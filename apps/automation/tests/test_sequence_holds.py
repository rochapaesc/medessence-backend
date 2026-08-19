"""
RF-SEQ-5.5: a conversa conta que está segurando uma sequência.

O RF-SEQ-5 decidiu que o atendente com a conversa SEGURA o disparo, e a
consequência ficava invisível para ele: a campanha parava e ninguém no balcão
sabia. Aqui o Inbox passa a dizer.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentSource,
    FlowStatus,
    HoldReason,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.inbox.models import Channel, Conversation

pytestmark = pytest.mark.django_db

URL = "/api/v1/conversations/"


def _trilha(clinic, nome="Pré-consulta", **extra):
    sequence = Sequence.objects.create(clinic=clinic, name=nome, is_active=True, **extra)
    SequenceStep.objects.create(
        sequence=sequence,
        order=1,
        offset_days=1,
        send_time=time(8, 0),
        flow=make_flow(clinic, name=f"Aviso {nome}", status=FlowStatus.ACTIVE),
    )
    return sequence


def _conversa(clinic, wa_id="5585900000123"):
    # Um canal por clínica é regra do banco: reusar o que existir, senão a
    # segunda conversa do teste estoura na unicidade em vez de testar algo.
    channel = Channel.objects.filter(clinic=clinic).first() or make_channel(clinic)
    contact = make_contact(clinic, wa_id)
    return Conversation.objects.create(clinic=clinic, channel=channel, contact=contact)


def _inscricao(sequence, conversation, *, motivo=HoldReason.BUSY, ha_horas=2, **extra):
    return SequenceEnrollment.objects.create(
        clinic=sequence.clinic,
        sequence=sequence,
        contact=conversation.contact,
        status=extra.pop("status", SequenceEnrollmentStatus.ACTIVE),
        source=EnrollmentSource.PATIENT_RECORD,
        anchor_at=timezone.now(),
        hold_reason=motivo,
        held_since=timezone.now() - timedelta(hours=ha_horas),
        **extra,
    )


def _linha(api_client, user, conversation):
    api_client.force_authenticate(user)
    resposta = api_client.get(URL)
    assert resposta.status_code == 200
    return next(c for c in resposta.data["results"] if c["id"] == conversation.pk)


def test_conversa_que_segura_uma_trilha_conta_qual_e_desde_quando(
    api_client, manager_single_clinic, clinic_a
):
    conversa = _conversa(clinic_a)
    _inscricao(_trilha(clinic_a), conversa, ha_horas=2)

    espera = _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"]

    assert espera["quantas"] == 1
    assert espera["nome"] == "Pré-consulta"
    assert espera["desde"] is not None


def test_conversa_sem_nada_esperando_nao_inventa_faixa(
    api_client, manager_single_clinic, clinic_a
):
    conversa = _conversa(clinic_a)

    assert _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"] is None


def test_duas_esperando_contam_duas_e_trazem_a_mais_antiga(
    api_client, manager_single_clinic, clinic_a
):
    """O nome sai da frase na tela, mas a espera contada é a da primeira."""
    conversa = _conversa(clinic_a)
    _inscricao(_trilha(clinic_a, "Recente"), conversa, ha_horas=1)
    _inscricao(_trilha(clinic_a, "Antiga"), conversa, ha_horas=72)

    espera = _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"]

    assert espera["quantas"] == 2
    assert espera["nome"] == "Antiga"


def test_espera_por_outro_motivo_nao_vira_faixa(api_client, manager_single_clinic, clinic_a):
    """
    Só `busy` aparece. Fora da janela e trilha desligada não são causados por
    quem atende nem resolvidos por ele: seriam aviso sem porta de saída.
    """
    conversa = _conversa(clinic_a)
    _inscricao(_trilha(clinic_a), conversa, motivo=HoldReason.NO_WINDOW)

    assert _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"] is None


def test_sequencia_apagada_nao_segura_mais_ninguem(
    api_client, manager_single_clinic, clinic_a
):
    """
    ⚠️ O gerenciador padrão filtra o `deleted_at` da INSCRIÇÃO, não o da
    SEQUÊNCIA. Sem o filtro explícito, a faixa acusaria uma trilha que já não
    existe, e a recepção encerraria o atendimento para destravar o nada.
    """
    conversa = _conversa(clinic_a)
    trilha = _trilha(clinic_a)
    _inscricao(trilha, conversa)
    trilha.delete()

    assert _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"] is None


def test_trilha_desligada_nao_aparece(api_client, manager_single_clinic, clinic_a):
    """Desligada segura por OUTRO motivo, e o `hold_reason` só muda na próxima
    varredura: até lá a faixa mentiria sobre o que destrava."""
    conversa = _conversa(clinic_a)
    trilha = _trilha(clinic_a)
    _inscricao(trilha, conversa)
    trilha.is_active = False
    trilha.save(update_fields=["is_active"])

    assert _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"] is None


def test_inscricao_encerrada_nao_aparece(api_client, manager_single_clinic, clinic_a):
    conversa = _conversa(clinic_a)
    _inscricao(_trilha(clinic_a), conversa, status=SequenceEnrollmentStatus.COMPLETED)

    assert _linha(api_client, manager_single_clinic, conversa)["sequencias_segurando"] is None


def test_a_espera_e_do_contato_daquela_conversa(api_client, manager_single_clinic, clinic_a):
    """A conversa do vizinho não herda a faixa: o disparo resolve a conversa
    pelo contato da inscrição."""
    segurando = _conversa(clinic_a, "5585900000123")
    outra = _conversa(clinic_a, "5585900000999")
    _inscricao(_trilha(clinic_a), segurando)

    api_client.force_authenticate(manager_single_clinic)
    linhas = {c["id"]: c["sequencias_segurando"] for c in api_client.get(URL).data["results"]}

    assert linhas[segurando.pk] is not None
    assert linhas[outra.pk] is None


def test_ao_segurar_a_tela_aberta_recebe_a_faixa_sem_recarregar(
    clinic_a, django_capture_on_commit_callbacks
):
    """
    A espera nasce na varredura de MINUTO: sem o evento, quem já estava com a
    conversa aberta só veria a faixa na próxima carga. O campo viaja só neste
    instante e no de soltar, porque pô-lo em todo `conversation:updated` daria
    uma consulta por mensagem.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    from apps.automation.choices import EnrollmentSource
    from apps.automation.sequences import inscrever, resolver_disparo
    from apps.automation.tests.test_sequences_engine import conversa_aberta, montar, vencer
    from apps.inbox.choices import AttendedBy

    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact, atendida_por=AttendedBy.AGENT)
    sequence, _ = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    layer = get_channel_layer()
    canal = async_to_sync(layer.new_channel)()
    async_to_sync(layer.group_add)(f"inbox_clinic_{clinic_a.id}", canal)

    # O aviso sai no commit, para não anunciar espera que o rollback desfez.
    with django_capture_on_commit_callbacks(execute=True):
        assert resolver_disparo(enrollment.pk) == f"segurado_{HoldReason.BUSY}"

    eventos = []
    while True:
        import asyncio

        async def um():
            try:
                return await asyncio.wait_for(layer.receive(canal), timeout=0.3)
            except TimeoutError:
                return None

        recebido = async_to_sync(um)()
        if recebido is None:
            break
        eventos.append(recebido["data"])

    avisos = [e for e in eventos if "sequencias_segurando" in e]
    assert avisos, f"nenhum evento com a espera em {[e.get('event') for e in eventos]}"
    assert avisos[-1]["sequencias_segurando"]["quantas"] == 1


def test_a_fila_nao_pergunta_por_linha(
    api_client, manager_single_clinic, clinic_a, django_assert_max_num_queries
):
    """
    A anotação existe para isto: trinta conversas na página não podem virar
    trinta consultas. Com mais conversas, o número de consultas não muda.
    """
    for i in range(6):
        conversa = _conversa(clinic_a, f"558590000{i:04d}")
        _inscricao(_trilha(clinic_a, f"Trilha {i}"), conversa)

    api_client.force_authenticate(manager_single_clinic)
    with django_assert_max_num_queries(12):
        assert api_client.get(URL).status_code == 200
