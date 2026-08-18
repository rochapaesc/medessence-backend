"""
Jobs Celery da automação (§13).

  sweep_flow_runs (automation, beat 1min) - acorda esperas vencidas, entrega
  ao humano o que ficou parado, e limpa execução cujo dono já mudou.

A varredura é UMA só de propósito: as três coisas olham a mesma tabela e o
mesmo relógio, e três tasks disputando as mesmas linhas dariam corrida sem
ganho nenhum.
"""

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(queue="automation")
def enviar_falas_do_fluxo(run_id: int, message_ids: list[int]):
    """
    Envia as falas de UM avanço, uma de cada vez e na ordem produzida.

    ⚠️ Uma task por mensagem NÃO serve: o worker roda com concorrência 4, e
    duas tasks disparadas juntas viram duas chamadas paralelas à Meta. Quem
    responder primeiro chega primeiro, e o paciente lê o menu antes da
    saudação. Aconteceu no primeiro teste ao vivo.

    A posse é conferida antes de CADA envio (RF-FLW-10), e não só ao montar a
    mensagem: entre a criação e a saída de fato, um atendente pode ter
    assumido. Quando isso acontece o resto do lote é descartado, porque a
    pessoa já está conduzindo a conversa.

    ⚠️ O que barra é a caneta na mão de um ATENDENTE, e não "o robô não está
    mais com a caneta" (corrigido 31/07/2026). A diferença derrubava a ÚLTIMA
    fala de todo fluxo que chegava ao fim: `_finish` devolve a conversa para a
    fila (posse vira `none`) e só então despacha, então a conferência antiga
    encontrava a conversa sem o bot e engolia justamente a mensagem de
    encerramento. O paciente respondia o nome e nunca recebia a confirmação.

    Posse `none` significa que ninguém pegou a conversa, e nesse caso a fala
    que o fluxo já produziu tem de sair.
    """
    from apps.inbox.choices import AttendedBy
    from apps.inbox.models import Conversation, Message
    from apps.inbox.services import send_message

    enviadas = 0
    for i, mid in enumerate(message_ids):
        message = Message.objects.filter(pk=mid, provider_message_id="").first()
        if message is None:
            continue  # já saiu, ou sumiu
        assumida = Conversation.objects.filter(
            pk=message.conversation_id, attended_by=AttendedBy.AGENT
        ).exists()
        if assumida:
            restantes = message_ids[i:]
            logger.info(
                "Fluxo %s: alguém assumiu a conversa, %s fala(s) não saíram",
                run_id,
                len(restantes),
            )
            _marcar_nao_enviadas(restantes)
            break
        send_message(message)
        enviadas += 1
    return {"run": run_id, "enviadas": enviadas, "de": len(message_ids)}


def _marcar_nao_enviadas(message_ids: list[int]) -> None:
    """
    Fala descartada não pode ficar na thread parecendo entregue.

    Sem isto a recepção lê no Inbox uma mensagem que o paciente nunca recebeu e
    responde em cima dela, o que é pior do que não ter mandado nada. `failed`
    é o mesmo estado que uma recusa da Meta produz, então a tela já sabe
    desenhar.
    """
    from apps.inbox.choices import MessageStatus
    from apps.inbox.models import Message
    from apps.inbox.realtime import notify_message_status

    motivo = "Atendente assumiu a conversa antes do envio"
    alvos = list(Message.objects.filter(pk__in=message_ids, provider_message_id=""))
    Message.objects.filter(pk__in=[m.pk for m in alvos]).update(
        status=MessageStatus.FAILED,
        status_error=motivo,
        updated_at=timezone.now(),
    )
    for message in alvos:
        notify_message_status(
            message.clinic_id,
            "",
            MessageStatus.FAILED,
            message.conversation_id,
            message_id=message.pk,
            error=motivo,
        )


@shared_task(queue="automation")
def advance_flow_run(run_id: int):
    """Avança UMA execução (RF-FLW-7). Usada pela varredura das esperas."""
    from apps.automation.choices import FlowRunStatus
    from apps.automation.engine import advance
    from apps.automation.models import FlowRun

    run = (
        FlowRun.objects.filter(pk=run_id, status=FlowRunStatus.ACTIVE)
        .select_related("flow", "version", "conversation", "clinic")
        .first()
    )
    if run is None:
        return "skipped: execução ausente ou encerrada"

    advance(run, from_outcome="default")
    return {"run": run.pk, "status": run.status}


@shared_task(queue="automation")
def sweep_flow_runs():
    """
    Varredura de minuto das execuções vivas (RF-FLW-11).

    Três coisas, nesta ordem:

    1. **Dono mudou** - um atendente assumiu e o paciente nunca mais escreveu.
       Sem isto a execução ficaria ACTIVE para sempre, e pior: ocupando a
       trava de "uma execução por contato", o que impediria qualquer fluxo
       futuro para aquela pessoa.
    2. **Espera vencida** - o nó "Aguardar" marcou a hora de voltar.
    3. **Silêncio longo** - o paciente parou de responder no meio; entrega ao
       humano com o que já foi coletado, em vez de sumir com a conversa.
    """
    from datetime import timedelta

    from apps.automation.choices import FlowRunStatus
    from apps.automation.engine import _finish, pause_for_agent
    from apps.automation.models import FlowRun
    from apps.inbox.choices import AttendedBy

    agora = timezone.now()
    stats = {"assumidas": 0, "acordadas": 0, "expiradas": 0}

    vivas = FlowRun.objects.filter(status=FlowRunStatus.ACTIVE).select_related(
        "flow", "version", "conversation", "clinic"
    )

    for run in vivas:
        conversation = run.conversation

        # 1. alguém assumiu enquanto o robô esperava
        if conversation is None or conversation.attended_by != AttendedBy.BOT:
            pause_for_agent(run)
            stats["assumidas"] += 1
            continue

        # 2. o nó "Aguardar" pediu para voltar
        if run.wake_at and run.wake_at <= agora:
            advance_flow_run.delay(run.pk)
            stats["acordadas"] += 1
            continue

        # 3. o paciente calou. `wake_at` preenchido não conta: ali quem manda
        # é o relógio do fluxo, não o silêncio do paciente.
        if run.wake_at:
            continue

        horas = int((run.flow.fallback or {}).get("on_timeout_hours") or 0)
        if horas and run.last_advanced_at <= agora - timedelta(hours=horas):
            _finish(
                run,
                conversation,
                status=FlowRunStatus.TIMED_OUT,
                reason="inatividade",
            )
            stats["expiradas"] += 1

    if any(stats.values()):
        logger.info("sweep_flow_runs: %s", stats)
    return stats


# Teto de inscrições enfileiradas por varredura. Existe por causa do lote
# (RF-SEQ-9): inscrever 1.891 pacientes de uma vez põe muita gente com o mesmo
# `next_dispatch_at`, e despejar tudo num tick só afogaria a fila de saída. O
# que sobra volta no minuto seguinte, e o corte é BARULHENTO de propósito -
# teto que corta calado faz o gestor achar que a sequência morreu.
MAX_DISPAROS_POR_VARREDURA = 300


@shared_task(queue="automation")
def sweep_sequences():
    """
    Varredura de minuto das sequências (§4.4, RF-SEQ-5).

    Só seleciona e enfileira: quem resolve é uma task por inscrição, porque
    disparar envolve falar com a Meta e uma lenta não pode segurar as outras.
    A trava contra varreduras sobrepostas mora no `resolver_disparo`, num
    UPDATE condicionado - aqui não há estado para proteger.

    É a MESMA varredura de minuto do `sweep_flow_runs` no sentido do RF-FLW-20
    (um relógio para os dois motores), mas task própria: elas olham tabelas
    diferentes e nada ganhariam disputando as mesmas linhas.
    """
    from apps.automation.choices import SequenceEnrollmentStatus
    from apps.automation.models import SequenceEnrollment

    agora = timezone.now()
    vencidas = list(
        SequenceEnrollment.objects.filter(
            status=SequenceEnrollmentStatus.ACTIVE,
            next_dispatch_at__lte=agora,
            sequence__is_active=True,
            # ⚠️ O `delete()` do projeto é SOFT, e uma sequência apagada
            # continua com `is_active=True`. Sem esta linha, apagar a trilha
            # deixava as inscrições disparando. O gerenciador padrão filtra o
            # `deleted_at` da INSCRIÇÃO, não o do que ela referencia.
            sequence__deleted_at__isnull=True,
        )
        .order_by("next_dispatch_at")
        .values_list("pk", flat=True)[: MAX_DISPAROS_POR_VARREDURA + 1]
    )

    truncou = len(vencidas) > MAX_DISPAROS_POR_VARREDURA
    if truncou:
        vencidas = vencidas[:MAX_DISPAROS_POR_VARREDURA]
        logger.warning(
            "sweep_sequences: mais de %s disparos vencidos neste tick; o teto foi "
            "aplicado e o resto sai no próximo minuto",
            MAX_DISPAROS_POR_VARREDURA,
        )

    for pk in vencidas:
        resolver_disparo_da_sequencia.delay(pk)

    if vencidas:
        logger.info("sweep_sequences: %s enfileirados", len(vencidas))
    return {"enfileirados": len(vencidas), "truncou": truncou}


@shared_task(queue="automation")
def resolver_disparo_da_sequencia(enrollment_id: int):
    """
    Resolve UM passo vencido (RF-SEQ-5). Uma task por inscrição.

    ⚠️ Sem retry automático de propósito: o relógio da própria inscrição já é
    o retry. A reserva empurra `next_dispatch_at` cinco minutos para frente
    antes de qualquer coisa, então um worker que morra no meio faz o passo
    voltar sozinho na varredura seguinte, sem risco de disparo duplo.
    """
    from apps.automation.sequences import resolver_disparo

    resultado = resolver_disparo(enrollment_id)
    logger.info("resolver_disparo(%s): %s", enrollment_id, resultado)
    return resultado
