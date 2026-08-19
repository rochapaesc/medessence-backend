"""
Mexer na trilha com GENTE DENTRO dela (RF-SEQ-6.1 e RF-SEQ-2.3).

Nasceu de um teste ao vivo em 18/08: o usuário ligou uma trilha de três
passos, recebeu o primeiro, trocou a ordem e os horários dos outros dois, e
uma das mensagens **nunca saiu e não deixou registro** - nem envio, nem pulo,
nem motivo no painel. A pergunta dele foi a certa: "foi porque troquei a ordem
e as horas?".

Aqui ficam as duas perguntas que isso levantou:
  1. desligar e religar retoma de onde parou, ou recomeça?
  2. editar o prazo de um passo pode ultrapassá-lo em silêncio?
"""

import zoneinfo
from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    FlowNodeType,
    DispatchSkipReason,
    EnrollmentSource,
    FlowStatus,
    HoldReason,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Sequence, SequenceDispatch, SequenceStep
from apps.automation.sequences import inscrever, resolver_disparo
from apps.automation.tests.conftest import make_flow, make_inbox

pytestmark = pytest.mark.django_db

#: Três horas atrás. Perto o bastante para nada vencer na validade padrão de
#: 24h, longe o bastante para os três passos já estarem no passado.
ANCORA = timezone.now() - timedelta(hours=3)


def _hora_local(clinic, quando):
    """
    A hora do passo é LOCAL da clínica, e a âncora aqui está em UTC.

    ⚠️ Gravar `quando.time()` direto parece funcionar e não funciona: o motor
    lê o `send_time` como hora da clínica, então três horas de fuso viram um
    passo no futuro e a validade nunca vence. Custou um teste verde mentindo.
    """
    fuso = zoneinfo.ZoneInfo(clinic.timezone or "America/Sao_Paulo")
    return timezone.localtime(quando, fuso).time()


@pytest.fixture
def trilha(clinic_a):
    sequence = Sequence.objects.create(
        clinic=clinic_a, name="Três passos", is_active=True, is_marketing=False
    )
    # Grafo de verdade e nó de TEMPLATE: sem nó de entrada o disparo é pulado
    # antes de qualquer regra, e com texto ele seria segurado pela janela de
    # 24h. Os dois esconderiam o que este arquivo quer medir.
    flow = make_flow(
        clinic_a,
        name="Aviso",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "config": {"template_name": "aviso"},
                }
            ],
            "edges": [],
        },
    )
    # Horários derivados da ÂNCORA e não do relógio de parede: assim os três
    # passos ficam no passado recente (vencidos para a varredura, vivos para a
    # validade de 24h) a qualquer hora em que a suíte rode, inclusive de
    # madrugada.
    for ordem in (1, 2, 3):
        SequenceStep.objects.create(
            sequence=sequence,
            order=ordem,
            name=f"Passo {ordem}",
            offset_days=0,
            send_time=_hora_local(clinic_a, ANCORA + timedelta(minutes=ordem)),
            flow=flow,
        )
    return sequence


@pytest.fixture
def dentro(clinic_a, trilha):
    inbox = make_inbox(clinic_a)
    return inscrever(
        trilha, inbox["contact"], source=EnrollmentSource.BATCH, anchor_at=ANCORA
    )


def _resolver(enrollment):
    """Força a varredura a olhar esta inscrição agora."""
    enrollment.next_dispatch_at = timezone.now() - timedelta(seconds=1)
    enrollment.save(update_fields=["next_dispatch_at"])
    return resolver_disparo(enrollment.pk)


# ---- desligar e religar ----


def test_desligar_segura_e_nao_consome_o_passo(trilha, dentro):
    passo_antes = dentro.current_step_id

    trilha.is_active = False
    trilha.save(update_fields=["is_active"])
    resultado = _resolver(dentro)

    dentro.refresh_from_db()
    assert resultado == f"segurado_{HoldReason.SEQUENCE_OFF}"
    assert dentro.hold_reason == HoldReason.SEQUENCE_OFF
    assert dentro.held_since is not None
    # O ponto: o passo NÃO foi consumido nem pulado.
    assert dentro.current_step_id == passo_antes
    assert SequenceDispatch.objects.filter(enrollment=dentro).count() == 0


def test_religar_retoma_de_onde_parou_e_nao_do_zero(trilha, dentro):
    """A pergunta do usuário: 'se a pessoa recebeu a primeira e eu desligo e
    ligo, vai de onde parou ou recebe do zero?'"""
    primeiro = dentro.current_step
    assert _resolver(dentro) == "disparado"
    dentro.refresh_from_db()
    segundo = dentro.current_step
    assert segundo != primeiro

    trilha.is_active = False
    trilha.save(update_fields=["is_active"])
    _resolver(dentro)
    dentro.refresh_from_db()
    assert dentro.current_step_id == segundo.pk, "desligada não anda"

    trilha.is_active = True
    trilha.save(update_fields=["is_active"])
    assert _resolver(dentro) == "disparado"

    dentro.refresh_from_db()
    saiu = list(
        SequenceDispatch.objects.filter(enrollment=dentro)
        .order_by("resolved_at")
        .values_list("step__name", flat=True)
    )
    # Continuou do segundo, e não repetiu o primeiro.
    assert saiu == ["Passo 1", "Passo 2"]


def test_o_que_venceu_no_escuro_pula_pela_validade(trilha, dentro):
    """Religar não faz sair mensagem atrasada: o que venceu pula, com motivo."""
    for passo in trilha.steps.all():
        passo.expire_hours = 1
        passo.save(update_fields=["expire_hours"])

    trilha.is_active = False
    trilha.save(update_fields=["is_active"])
    _resolver(dentro)

    # A âncora é de ontem, então com validade de 1h o passo já venceu.
    trilha.is_active = True
    trilha.save(update_fields=["is_active"])
    _resolver(dentro)

    disparo = SequenceDispatch.objects.filter(enrollment=dentro).first()
    assert disparo.status == SequenceDispatchStatus.SKIPPED
    assert disparo.skip_reason == DispatchSkipReason.EXPIRED


# ---- mexer nos horários com gente dentro ----


def test_atrasar_o_proximo_passo_nao_perde_o_passo(trilha, dentro):
    """Mudar o prazo do passo que AINDA VAI sair é seguro: ele só anda."""
    assert _resolver(dentro) == "disparado"
    dentro.refresh_from_db()
    segundo = dentro.current_step

    segundo.send_time = time(23, 0)
    segundo.save(update_fields=["send_time"])

    dentro.refresh_from_db()
    assert dentro.current_step_id == segundo.pk


def test_mover_o_passo_atual_para_tras_nao_o_perde(trilha, dentro):
    """Mexer no passo que o ponteiro JÁ aponta é seguro: ele continua sendo o próximo."""
    assert _resolver(dentro) == "disparado"
    dentro.refresh_from_db()
    segundo = dentro.current_step
    assert segundo.name == "Passo 2"

    segundo.send_time = time(0, 5)
    segundo.save(update_fields=["send_time"])

    assert _resolver(dentro) == "disparado"
    saiu = list(
        SequenceDispatch.objects.filter(enrollment=dentro)
        .order_by("resolved_at")
        .values_list("step__name", flat=True)
    )
    assert saiu == ["Passo 1", "Passo 2"]


def test_passo_ultrapassado_por_edicao_nao_some_calado(trilha, dentro):
    """
    ⚠️ O defeito que apareceu ao vivo em 18/08, e o motivo de ele existir.

    A pessoa recebeu o passo 1 e o ponteiro foi para o 2. Aí o gestor mexeu no
    prazo do passo 3 e o jogou para ANTES de tudo. Como o motor anda pelo
    relógio, o 3 ficou atrás do ponteiro e nunca mais seria visitado: sem
    envio, sem pulo, sem motivo. Quem pergunta "por que ele não recebeu a
    última?" não achava resposta em lugar nenhum.
    """
    assert _resolver(dentro) == "disparado"
    dentro.refresh_from_db()
    assert dentro.current_step.name == "Passo 2"

    terceiro = trilha.steps.get(name="Passo 3")
    terceiro.send_time = time(0, 5)
    terceiro.save(update_fields=["send_time"])

    assert _resolver(dentro) == "disparado"  # sai o passo 2
    dentro.refresh_from_db()

    saiu = {
        d.step.name: (d.status, d.skip_reason)
        for d in SequenceDispatch.objects.filter(enrollment=dentro).select_related("step")
    }
    assert "Passo 3" in saiu, f"o passo 3 sumiu sem registro. Só há {list(saiu)}."
    assert saiu["Passo 3"] == (
        SequenceDispatchStatus.SKIPPED,
        DispatchSkipReason.REORDERED,
    )
    # E a trilha termina, em vez de ficar esperando um passo que não vem.
    assert dentro.status == SequenceEnrollmentStatus.COMPLETED


# ---- fluxo em RASCUNHO não fala com paciente (18/08) ----


def test_passo_com_fluxo_em_rascunho_e_pulado_com_motivo(trilha, dentro):
    """
    ⚠️ O erro que apareceu ao vivo em 18/08.

    Trilha criada a partir de um modelo (RF-SEQ-12) nasce com um fluxo em
    RASCUNHO por passo, e o nó de template SEM TEMPLATE ESCOLHIDO - a pendência
    honesta do RF-SEQ-5.4. A guarda antiga só perguntava se existia versão, não
    se ela estava publicada, então os três passos dispararam, o nó tentou
    mandar um modelo vazio e a Meta recusou os três com "The parameter
    text.body is required".

    Pior que a recusa: o painel marcou os três como DISPARADOS, porque disparar
    o fluxo deu certo. Quem lê o painel conclui que o paciente foi avisado.
    """
    flow = trilha.steps.first().flow
    flow.status = FlowStatus.DRAFT
    flow.save(update_fields=["status"])

    assert _resolver(dentro) == "pulado_sem_fluxo"

    disparo = SequenceDispatch.objects.filter(enrollment=dentro).first()
    assert disparo.status == SequenceDispatchStatus.SKIPPED
    assert disparo.skip_reason == DispatchSkipReason.FLOW_UNPUBLISHED
