"""
O motor de sequências (RF-SEQ-5): a ordem das checagens e o efeito de cada uma.

Segurar, pular e cancelar são coisas DIFERENTES, e é isso que estes testes
protegem: só o pulo consome o passo e anda o calendário.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    DispatchSkipReason,
    EnrollmentEndReason,
    EnrollmentSource,
    FlowNodeType,
    FlowStatus,
    HoldReason,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import (
    Sequence,
    SequenceDispatch,
    SequenceEnrollment,
    SequenceStep,
)
from apps.automation.sequences import horario_do_passo, inscrever, resolver_disparo
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.inbox.choices import AttendedBy
from apps.inbox.models import Conversation

pytestmark = pytest.mark.django_db


# ---- montagem ----


def fluxo_que_abre_com_texto(clinic, name="Aviso"):
    return make_flow(
        clinic,
        name=name,
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {"id": "n1", "type": FlowNodeType.SEND_MESSAGE, "config": {"text": "Oi"}},
            ],
            "edges": [],
        },
    )


def fluxo_que_abre_com_template(clinic, name="Aviso com modelo"):
    return make_flow(
        clinic,
        name=name,
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "config": {"template_name": "lembrete"},
                },
            ],
            "edges": [],
        },
    )


def fluxo_que_abre_e_espera(clinic, name="Confirmação"):
    """Modelo aprovado e depois espera a resposta: o formato do D-1 de verdade."""
    return make_flow(
        clinic,
        name=name,
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "config": {"template_name": "lembrete"},
                },
                {
                    "id": "n2",
                    "type": FlowNodeType.COLLECT_INPUT,
                    "config": {"prompt_text": "Confirma?", "var_key": "confirma"},
                },
            ],
            "edges": [{"from": "n1", "to": "n2", "condition": "default"}],
        },
    )


def montar(clinic, *, flow=None, offset=0, expire_hours=24, is_active=True, marketing=True):
    sequence = Sequence.objects.create(
        clinic=clinic, name=f"Trilha {timezone.now().timestamp()}", is_active=is_active,
        is_marketing=marketing,
    )
    step = SequenceStep.objects.create(
        sequence=sequence,
        order=1,
        name="Primeiro",
        offset_days=offset,
        send_time=time(8, 0),
        flow=flow or fluxo_que_abre_com_texto(clinic),
        expire_hours=expire_hours,
    )
    return sequence, step


def vencer(enrollment, *, ha=timedelta(minutes=1)):
    """Põe o disparo no passado, que é o que a varredura procura."""
    SequenceEnrollment.objects.filter(pk=enrollment.pk).update(
        next_dispatch_at=timezone.now() - ha
    )
    enrollment.refresh_from_db()
    return enrollment


def conversa_aberta(clinic, contact, *, atendida_por=AttendedBy.NONE):
    """Conversa com a janela de 24h ABERTA (paciente falou agora)."""
    return Conversation.objects.create(
        clinic=clinic,
        channel=make_channel(clinic),
        contact=contact,
        attended_by=atendida_por,
        last_inbound_at=timezone.now(),
    )


# ---- o caminho feliz ----


def test_disparo_comeca_o_fluxo_e_grava_o_historico(clinic_a):
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, step = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == "disparado"

    disparo = SequenceDispatch.objects.get(enrollment=enrollment)
    assert disparo.status == SequenceDispatchStatus.STARTED
    assert disparo.flow_run is not None
    assert disparo.step == step

    # Passo único: a trilha acabou e o relógio para.
    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.COMPLETED
    assert enrollment.end_reason == EnrollmentEndReason.FINISHED
    assert enrollment.next_dispatch_at is None


def test_a_conversa_do_disparo_nasce_livre_para_o_robo_tomar(clinic_a):
    """
    Sem conversa nenhuma, a sequência cria uma, e ela nasce LIVRE para o robô
    tomar a caneta. Nascesse atribuída, como a do `iniciar_conversa`, todo
    disparo falharia em silêncio.

    ⚠️ O fluxo aqui ESPERA o paciente de propósito: um fluxo de nó único
    termina no mesmo avanço, e o fim devolve a conversa para a fila (posse
    volta a `none`) - a caneta na mão do robô só é observável enquanto a
    execução está viva.
    """
    contact = make_contact(clinic_a)
    make_channel(clinic_a)
    sequence, _ = montar(clinic_a, flow=fluxo_que_abre_e_espera(clinic_a))
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == "disparado"

    conversa = Conversation.objects.get(contact=contact)
    assert conversa.attended_by == AttendedBy.BOT


# ---- segurar (não grava linha) ----


def test_atendente_com_a_conversa_segura_o_disparo_sem_consumir_o_passo(clinic_a):
    """
    A escolha do usuário em 13/08: atendente SEGURA, não remove a inscrição.
    O `start_run` recusa sozinho, porque a caneta não está livre.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact, atendida_por=AttendedBy.AGENT)
    sequence, step = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == f"segurado_{HoldReason.BUSY}"

    assert not SequenceDispatch.objects.filter(enrollment=enrollment).exists()
    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE
    assert enrollment.current_step == step
    # O relógio foi empurrado para frente: volta a tentar sozinho.
    assert enrollment.next_dispatch_at > timezone.now()
    # E o painel tem o que mostrar ao lado da hora que já passou.
    assert enrollment.hold_reason == HoldReason.BUSY
    assert enrollment.held_since is not None


def test_segurando_desde_conta_do_primeiro_impedimento_e_nao_da_ultima_tentativa(clinic_a):
    """
    O painel diz "segurando há 2h", e é isso que a recepção lê para decidir se
    intervém. Reiniciar o relógio a cada tentativa faria o número dizer sempre
    "há 5 minutos", que é a hora da última varredura e não do problema.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact, atendida_por=AttendedBy.AGENT)
    sequence, _ = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    resolver_disparo(enrollment.pk)
    enrollment.refresh_from_db()
    primeira_vez = enrollment.held_since

    vencer(enrollment)
    resolver_disparo(enrollment.pk)
    enrollment.refresh_from_db()

    assert enrollment.held_since == primeira_vez


def test_disparar_com_sucesso_solta_o_que_estava_segurando(clinic_a):
    """A anotação some quando deixa de valer, senão o painel mente para sempre."""
    contact = make_contact(clinic_a)
    conversa = conversa_aberta(clinic_a, contact, atendida_por=AttendedBy.AGENT)
    sequence, _ = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    resolver_disparo(enrollment.pk)
    enrollment.refresh_from_db()
    assert enrollment.hold_reason == HoldReason.BUSY

    # A recepção soltou a conversa.
    conversa.attended_by = AttendedBy.NONE
    conversa.save(update_fields=["attended_by"])
    vencer(enrollment)

    assert resolver_disparo(enrollment.pk) == "disparado"
    enrollment.refresh_from_db()
    assert enrollment.held_since is None
    assert enrollment.hold_reason == ""


def test_disparo_adiado_nao_deixa_conversa_vazia_na_fila(clinic_a):
    """
    O rollback do adiamento leva junto a conversa recém-criada. Sem isto, cada
    tentativa frustrada deixaria uma conversa sem mensagem nenhuma na fila da
    recepção.
    """
    contact = make_contact(clinic_a)
    make_channel(clinic_a)
    # Fluxo que abre com TEXTO e contato sem inbound: a janela está fechada.
    sequence, _ = montar(clinic_a, flow=fluxo_que_abre_com_texto(clinic_a))
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == f"segurado_{HoldReason.NO_WINDOW}"
    assert not Conversation.objects.filter(contact=contact).exists()


def test_sequencia_apagada_encerra_a_inscricao_em_vez_de_disparar(clinic_a):
    """
    ⚠️ Defeito achado ao limpar os dados de teste em 13/08/2026: o `delete()`
    do projeto é SOFT, e uma sequência apagada continua com `is_active=True`.
    As inscrições ficavam VIVAS com disparo agendado, e o paciente voltaria a
    receber mensagem de uma trilha que a clínica já tinha aposentado.

    A guarda tem de estar aqui, e não só no viewset: quem apaga pelo admin,
    por comando ou por cascata não passa por ele.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    sequence.delete()  # soft: `is_active` continua True no banco

    assert resolver_disparo(enrollment.pk) == "cancelado_sequencia_apagada"
    assert not SequenceDispatch.objects.filter(enrollment=enrollment).exists()

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.SEQUENCE_RETIRED


def test_varredura_ignora_inscricao_de_sequencia_apagada(clinic_a):
    """A mesma proteção um nível antes: a varredura nem enfileira."""
    from apps.automation.tasks import sweep_sequences

    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a)
    vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert sweep_sequences()["enfileirados"] == 1
    sequence.delete()
    assert sweep_sequences()["enfileirados"] == 0


def test_sequencia_desligada_segura_e_religar_retoma(clinic_a):
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a, is_active=False)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == f"segurado_{HoldReason.SEQUENCE_OFF}"
    assert not SequenceDispatch.objects.filter(enrollment=enrollment).exists()
    enrollment.refresh_from_db()
    assert enrollment.hold_reason == HoldReason.SEQUENCE_OFF

    sequence.is_active = True
    sequence.save(update_fields=["is_active"])
    vencer(enrollment)
    assert resolver_disparo(enrollment.pk) == "disparado"


# ---- pular (consome o passo, com motivo) ----


def test_passo_vencido_e_pulado_com_motivo(clinic_a):
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a, expire_hours=2)
    enrollment = inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE)
    # ⚠️ A ÂNCORA vai para o passado junto, não só o next_dispatch_at: o
    # `previsto` sai da âncora (send_time 08:00), e com validade de 2h o teste
    # só expirava depois das 10:00 locais. Era uma bomba de horário, prima da
    # do dublê que fixava o mês: passava à noite e quebrava toda manhã.
    SequenceEnrollment.objects.filter(pk=enrollment.pk).update(
        anchor_at=timezone.now() - timedelta(days=3)
    )
    enrollment.refresh_from_db()
    vencer(enrollment, ha=timedelta(days=3))

    assert resolver_disparo(enrollment.pk) == "pulado_vencido"

    disparo = SequenceDispatch.objects.get(enrollment=enrollment)
    assert disparo.status == SequenceDispatchStatus.SKIPPED
    assert disparo.skip_reason == DispatchSkipReason.EXPIRED


def test_passo_negativo_expira_na_ancora_mesmo_com_validade_longa(clinic_a):
    """
    RF-SEQ-5.2: confirmação de véspera que não saiu até a hora da consulta não
    sai mais. Depois dela seria atrasada ou absurda.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a, offset=-1, expire_hours=240)

    # Âncora (a consulta) ficou 2h no passado; a validade de 10 dias ainda
    # estaria de pé, e mesmo assim o passo não deve sair.
    enrollment = inscrever(
        sequence,
        contact,
        source=EnrollmentSource.APPOINTMENT,
        anchor_at=timezone.now() - timedelta(hours=2),
    )
    vencer(enrollment)

    assert resolver_disparo(enrollment.pk) == "pulado_vencido"


def test_fluxo_sem_versao_publicada_pula_com_motivo(clinic_a):
    from apps.automation.models import Flow

    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    rascunho = Flow.objects.create(clinic=clinic_a, name="Só rascunho")
    sequence, _ = montar(clinic_a, flow=rascunho)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == "pulado_sem_fluxo"
    assert (
        SequenceDispatch.objects.get(enrollment=enrollment).skip_reason
        == DispatchSkipReason.FLOW_UNPUBLISHED
    )


def test_fluxo_publicado_com_grafo_vazio_pula_em_vez_de_adiar_para_sempre(clinic_a):
    """
    ⚠️ Achado ao escrever os testes da falta: fluxo com versão publicada mas
    SEM nó de entrada faz o `start_run` devolver `None` igualzinho a "a caneta
    está ocupada". Sem esta guarda o disparo adiava a cada cinco minutos até a
    validade vencer, anotando "ocupado" o tempo todo, e o painel diria a coisa
    errada sobre por que o paciente não recebeu.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    # `make_flow` publica com o grafo vazio quando não se passa nenhum.
    vazio = make_flow(clinic_a, name="Publicado e vazio", status=FlowStatus.ACTIVE)
    sequence, _ = montar(clinic_a, flow=vazio)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == "pulado_sem_fluxo"
    assert (
        SequenceDispatch.objects.get(enrollment=enrollment).skip_reason
        == DispatchSkipReason.FLOW_UNPUBLISHED
    )


def test_fora_da_janela_o_fluxo_de_template_passa(clinic_a):
    """O nó de modelo existe justamente para falar fora da janela de 24h."""
    contact = make_contact(clinic_a)
    make_channel(clinic_a)
    sequence, _ = montar(clinic_a, flow=fluxo_que_abre_com_template(clinic_a))
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == "disparado"


# ---- cancelar ----


def test_opt_out_cancela_a_inscricao_de_marketing_no_disparo(clinic_a):
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    assert resolver_disparo(enrollment.pk) == "cancelado_opt_out"
    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.OPTED_OUT


def test_sequencia_operacional_ignora_o_opt_out(clinic_a):
    """Parar promoção não é parar de confirmar consulta (RF-SEQ-8.2)."""
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    sequence, _ = montar(clinic_a, marketing=False)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    assert resolver_disparo(enrollment.pk) == "disparado"


# ---- corrida e idempotência ----


def test_duas_varreduras_sobrepostas_disparam_uma_vez_so(clinic_a):
    """
    A trava é a MESMA escrita do adiamento: quem reserva empurra o relógio, e a
    segunda tentativa não encontra mais a linha vencida.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, _ = montar(clinic_a)
    enrollment = vencer(inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE))

    primeiro = resolver_disparo(enrollment.pk)
    segundo = resolver_disparo(enrollment.pk)

    assert primeiro == "disparado"
    assert segundo == "corrida"
    assert SequenceDispatch.objects.filter(enrollment=enrollment).count() == 1


def test_passo_ja_resolvido_nao_dispara_de_novo(clinic_a):
    """Worker morto entre o disparo e a gravação não faz o passo sair duas vezes."""
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)
    sequence, step = montar(clinic_a)
    enrollment = inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE)

    SequenceDispatch.objects.create(
        enrollment=enrollment,
        step=step,
        scheduled_for=timezone.now(),
        resolved_at=timezone.now(),
        status=SequenceDispatchStatus.STARTED,
    )
    vencer(enrollment)

    assert resolver_disparo(enrollment.pk) == "ja_resolvido"
    assert SequenceDispatch.objects.filter(enrollment=enrollment).count() == 1


# ---- a ordem sai do relógio (RF-SEQ-2.2) ----


def test_o_motor_avanca_pelo_relogio_e_nao_pela_posicao(clinic_a):
    """
    ⚠️ Defeito reproduzido em 13/08/2026, antes da correção: o motor avançava
    por `order` e calculava a hora por `offset_days`. Numa trilha com o passo 1
    em D+10 e o passo 2 em D+5, ele disparava o primeiro e agendava o segundo
    para uma data no PASSADO, que era pulada como vencida. O paciente nunca
    recebia o segundo, e o painel dizia "passou da validade", verdade que
    esconde a causa.
    """
    contact = make_contact(clinic_a)
    conversa_aberta(clinic_a, contact)

    sequence = Sequence.objects.create(clinic=clinic_a, name="Fora de ordem", is_active=True)
    flow = fluxo_que_abre_com_texto(clinic_a)
    tarde = SequenceStep.objects.create(
        sequence=sequence, order=1, name="Tarde", offset_days=10,
        send_time=time(8, 0), flow=flow,
    )
    cedo = SequenceStep.objects.create(
        sequence=sequence, order=2, name="Cedo", offset_days=5,
        send_time=time(8, 0), flow=flow,
    )

    # Âncora 10 dias atrás: o de D+5 já passou e o de D+10 vence agora.
    enrollment = inscrever(
        sequence,
        contact,
        source=EnrollmentSource.FLOW_NODE,
        anchor_at=timezone.now() - timedelta(days=10),
    )

    # O primeiro passo é o do relógio, e não o de `order` menor.
    assert enrollment.current_step == cedo

    vencer(enrollment)
    assert resolver_disparo(enrollment.pk) in ("disparado", "pulado_vencido")

    enrollment.refresh_from_db()
    # E o seguinte é o de DEPOIS no relógio, nunca um com data no passado.
    assert enrollment.current_step == tarde
    assert enrollment.next_dispatch_at > enrollment.anchor_at


def test_normalizar_ordem_reescreve_a_posicao_a_partir_do_prazo(clinic_a):
    """`order` é exposto na API, e campo exposto que discorda do motor engana."""
    from apps.automation.sequences import normalizar_ordem

    sequence = Sequence.objects.create(clinic=clinic_a, name="Bagunçada", is_active=True)
    flow = fluxo_que_abre_com_texto(clinic_a)
    passos = [
        SequenceStep.objects.create(
            sequence=sequence, order=i, name=f"P{offset}", offset_days=offset,
            send_time=time(8, 0), flow=flow,
        )
        for i, offset in enumerate([30, -1, 7], start=1)
    ]

    # Os três trocam de lugar: 30 vai do 1 ao 3, -1 do 2 ao 1 e 7 do 3 ao 2.
    assert normalizar_ordem(sequence) == 3

    for passo in passos:
        passo.refresh_from_db()
    ordens = {p.name: p.order for p in passos}
    assert ordens == {"P-1": 1, "P7": 2, "P30": 3}


def test_normalizar_e_idempotente(clinic_a):
    from apps.automation.sequences import normalizar_ordem

    sequence, _ = montar(clinic_a)
    assert normalizar_ordem(sequence) == 0


# ---- calendário ----


def test_horario_do_passo_usa_o_fuso_da_clinica(clinic_a):
    """
    O `send_time` promete 08:00 para quem está na clínica, não no servidor. Já
    houve adiamento do Inbox virando 6h da manhã na tela por causa disto.
    """
    import zoneinfo

    clinic_a.timezone = "America/Sao_Paulo"
    clinic_a.save(update_fields=["timezone"])
    _, step = montar(clinic_a, offset=1)

    ancora = timezone.now()
    quando = horario_do_passo(step, ancora, clinic_a)
    local = timezone.localtime(quando, zoneinfo.ZoneInfo("America/Sao_Paulo"))

    assert (local.hour, local.minute) == (8, 0)
    assert local.date() == (
        timezone.localtime(ancora, zoneinfo.ZoneInfo("America/Sao_Paulo"))
        + timedelta(days=1)
    ).date()
