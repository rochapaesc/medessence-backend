"""
O semeador de sequência de teste, e a trava que é o motivo de ele existir.

Sequência dispara fluxo, fluxo manda mensagem: semear numa clínica de verdade
falaria com paciente de verdade sem ninguém ter pedido.
"""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.automation.choices import SequenceEnrollmentStatus
from apps.automation.models import Sequence, SequenceEnrollment
from apps.automation.tests.conftest import make_channel
from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel
from apps.patients.models import Contact

pytestmark = pytest.mark.django_db

NOME = "Pós-consulta (teste)"


def test_recusa_clinica_com_canal_de_verdade(clinic_a):
    """A trava: canal `meta` fala com o WhatsApp real."""
    Channel.objects.create(
        clinic=clinic_a,
        provider=WhatsAppProviderKind.META,
        display_number="5589959011077",
    )

    with pytest.raises(CommandError, match="só roda em canal FAKE"):
        call_command("seed_sequence_demo", clinic=clinic_a.pk)

    assert not Sequence.objects.filter(clinic=clinic_a, name=NOME).exists()


def test_recusa_clinica_sem_canal(clinic_a):
    with pytest.raises(CommandError, match="não tem canal"):
        call_command("seed_sequence_demo", clinic=clinic_a.pk)


def test_semeia_e_inscreve_em_canal_fake(clinic_a):
    make_channel(clinic_a)
    call_command("seed_sequence_demo", clinic=clinic_a.pk)

    trilha = Sequence.objects.get(clinic=clinic_a, name=NOME)
    assert trilha.steps.count() == 2
    # Operacional, não marketing: é aviso de retorno, não promoção.
    assert trilha.is_marketing is False

    inscricoes = SequenceEnrollment.objects.filter(sequence=trilha)
    assert inscricoes.count() == 2
    assert all(i.status == SequenceEnrollmentStatus.ACTIVE for i in inscricoes)
    # Números impossíveis de receber mensagem (DDD 00).
    assert all(i.contact.wa_id.startswith("5500") for i in inscricoes)


def test_semear_duas_vezes_nao_duplica(clinic_a):
    make_channel(clinic_a)
    call_command("seed_sequence_demo", clinic=clinic_a.pk)
    call_command("seed_sequence_demo", clinic=clinic_a.pk)

    trilha = Sequence.objects.get(clinic=clinic_a, name=NOME)
    assert trilha.steps.count() == 2
    assert SequenceEnrollment.objects.filter(sequence=trilha).count() == 2


def test_limpar_leva_tudo_o_que_semeou(clinic_a):
    make_channel(clinic_a)
    call_command("seed_sequence_demo", clinic=clinic_a.pk)
    call_command("seed_sequence_demo", clinic=clinic_a.pk, limpar=True)

    assert not Sequence.objects.filter(clinic=clinic_a, name=NOME).exists()
    assert not Contact.objects.filter(clinic=clinic_a, wa_id__startswith="5500").exists()


def test_consulta_percorre_o_arco_da_porta_automatica(clinic_a):
    """
    `--consulta` exercita os quatro arcos sem chamar o motor: quem trabalha é o
    `post_save` da consulta. São DUAS consultas de propósito, porque o pulo do
    último passo CONCLUI a trilha e aí não sobraria nada para o cancelamento
    cancelar - com uma só, a demonstração mentiria.
    """
    make_channel(clinic_a)
    call_command("seed_sequence_demo", clinic=clinic_a.pk, consulta=True)

    jornada = Sequence.objects.get(clinic=clinic_a, name="Jornada da consulta (teste)")
    assert jornada.enroll_on_appointment is True

    inscricoes = SequenceEnrollment.objects.filter(sequence=jornada)
    assert inscricoes.count() == 2

    # A que faltou: pulou o passo pós-consulta COM o motivo gravado.
    faltou = inscricoes.filter(dispatches__skip_reason="patient_no_show").first()
    assert faltou is not None
    assert faltou.status != SequenceEnrollmentStatus.CANCELED

    # A que foi cancelada: trilha encerrada com o motivo.
    cancelada = inscricoes.filter(status=SequenceEnrollmentStatus.CANCELED).first()
    assert cancelada is not None
    assert cancelada.end_reason == "appointment_canceled"
    assert cancelada.next_dispatch_at is None


def test_lote_usa_trilha_de_marketing_para_o_opt_out_valer(clinic_a):
    """
    ⚠️ A trilha do lote é PRÓPRIA e de marketing: resgate é `MARKETING` na
    Meta, e é isso que faz o opt-out valer. Com a operacional da demonstração,
    quem pediu silêncio entraria e a regra que é obrigação legal ficaria
    escondida.
    """
    make_channel(clinic_a)
    call_command("seed_sequence_demo", clinic=clinic_a.pk, lote=5)

    resgate = Sequence.objects.get(clinic=clinic_a, name="Resgate (teste)")
    assert resgate.is_marketing is True

    inscricoes = SequenceEnrollment.objects.filter(sequence=resgate)
    # Dos 5: um sem número e um com opt-out ficam de fora.
    assert inscricoes.count() == 3
    assert all(i.source == "batch" for i in inscricoes)


def test_disparar_resolve_o_passo_na_hora(clinic_a):
    make_channel(clinic_a)
    call_command("seed_sequence_demo", clinic=clinic_a.pk, disparar=True)

    trilha = Sequence.objects.get(clinic=clinic_a, name=NOME)
    # O primeiro passo abre com MODELO aprovado, então sai mesmo com a janela
    # de 24h fechada - que é a situação de toda sequência.
    for inscricao in SequenceEnrollment.objects.filter(sequence=trilha):
        assert inscricao.dispatches.count() == 1
        assert inscricao.dispatches.first().flow_run is not None
