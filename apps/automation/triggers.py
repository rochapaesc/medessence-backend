"""
O que faz um fluxo começar (§4.3.2, RF-FLW-5).

O protótipo do cliente não tinha esta parte: o nó de início não dizia o que o
dispara. É a decisão de produto mais pesada do módulo, porque um fluxo com
gatilho `first_inbound` põe o robô na frente da recepção em TODA conversa
nova - e é por isso que o `only_outside_hours` existe.
"""

import logging

from apps.automation.choices import FlowStatus, FlowTrigger
from apps.automation.models import Flow
from apps.inbox.choices import SenderKind

logger = logging.getLogger(__name__)


def _matches_keyword(texto: str, config: dict) -> bool:
    """
    Casamento de palavra-chave. Sem regex de propósito: quem monta o fluxo é
    o gestor da clínica, e uma expressão mal escrita passaria a valer para
    toda mensagem que chega.
    """
    alvo = (texto or "").strip().casefold()
    if not alvo:
        return False

    modo = (config.get("match") or "contains").lower()
    for palavra in config.get("keywords") or []:
        chave = str(palavra).strip().casefold()
        if not chave:
            continue
        if modo == "exact" and alvo == chave:
            return True
        if modo == "starts_with" and alvo.startswith(chave):
            return True
        if modo == "contains" and chave in alvo:
            return True
    return False


def _is_first_inbound(conversation) -> bool:
    """
    Primeira mensagem DO CONTATO nesta conversa.

    Conta a que acabou de chegar, então "primeira" é exatamente uma. Contar
    todas as mensagens não serviria: a clínica pode ter iniciado a conversa.
    """
    from apps.inbox.models import Message

    return (
        Message.objects.filter(
            conversation=conversation, sender_kind=SenderKind.CONTACT, is_internal=False
        ).count()
        <= 1
    )


def pick_flow(conversation, message) -> Flow | None:
    """
    Qual fluxo atende esta mensagem, ou None.

    RF-FLW-5.2: mais de um pode casar; desempata por prioridade (menor vence)
    e, no empate, o ativado mais recentemente. Nunca dois ao mesmo tempo - a
    garantia é aqui, e não numa trava do banco, porque ter dois fluxos de
    primeira mensagem (um para fora do horário, outro para dentro) é desenho
    legítimo.
    """
    from apps.automation.engine import _clinic_is_open

    candidatos = (
        Flow.objects.filter(clinic=conversation.clinic, status=FlowStatus.ACTIVE)
        .exclude(current_version__isnull=True)
        .exclude(trigger=FlowTrigger.MANUAL)
        .select_related("current_version")
        .order_by("priority", "-activated_at", "-pk")
    )
    if not candidatos:
        return None

    # ⚠️ Toque em botão NUNCA começa fluxo (06/08/2026). Ele é resposta a uma
    # pergunta que já foi feita, e o WhatsApp manda junto o TÍTULO do botão:
    # com casamento por `contains`, tocar num botão velho chamado "Marcar
    # consulta" dispararia um fluxo cuja palavra é "consulta". Quem continua
    # execução em andamento é o `on_inbound`, que roda antes deste.
    if (message.content_data or {}).get("interactive_id"):
        return None

    aberta = _clinic_is_open(conversation.clinic)
    primeira = None  # calculado sob demanda: a contagem é uma query

    for flow in candidatos:
        if flow.only_outside_hours and aberta:
            continue

        if flow.trigger == FlowTrigger.KEYWORD:
            if _matches_keyword(message.body, flow.trigger_config or {}):
                return flow
            continue

        if flow.trigger == FlowTrigger.FIRST_INBOUND:
            if primeira is None:
                primeira = _is_first_inbound(conversation)
            if primeira:
                return flow

    return None


def handle_inbound(conversation, message) -> bool:
    """
    Ponto de entrada da ingestão. Devolve True se um fluxo tratou a mensagem.

    A ordem importa: PRIMEIRO tenta continuar uma execução em andamento, e só
    então cogita começar uma nova. Invertido, a palavra-chave "orçamento" dita
    no meio de um agendamento reiniciaria a conversa do zero.
    """
    from apps.automation.engine import on_inbound, start_run

    if on_inbound(conversation, message):
        return True

    flow = pick_flow(conversation, message)
    if not flow:
        return False

    return start_run(flow, conversation) is not None
