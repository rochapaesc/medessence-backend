"""
O que faz um fluxo começar (§4.3.2, RF-FLW-5).

O protótipo do cliente não tinha esta parte: o nó de início não dizia o que o
dispara. É a decisão de produto mais pesada do módulo, porque um fluxo com
gatilho `first_inbound` põe o robô na frente da recepção em TODA conversa
nova - e é por isso que o `only_outside_hours` existe.
"""

import logging
from datetime import timedelta

from apps.automation.choices import FlowStatus, FlowTrigger
from apps.automation.models import Flow
from apps.inbox.choices import ActivityType, MessageKind, SenderKind

logger = logging.getLogger(__name__)

# Quantas execuções o MESMO contato pode começar numa hora (RF-FLW-23.2).
MAX_RUNS_POR_HORA = 3


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


def _is_new_conversation(conversation) -> bool:
    """
    Esta mensagem começou um ATENDIMENTO novo? (RF-FLW-5.2)

    Conta as falas do contato desde o último ENCERRAMENTO: exatamente uma
    significa que a que acabou de chegar abriu o atendimento. Sem encerramento
    nenhum, é a conversa nova, e a conta dá o mesmo resultado do
    `_is_first_inbound` — ele é o caso particular deste.

    ⚠️ Por que não olhar o status: quando isto roda, a ingestão JÁ reabriu a
    conversa (o signal da mensagem chama `reopen` antes do sinal que chega
    aqui), então ela nunca está mais em Resolvida. O rastro que sobrevive é o
    evento de encerramento na linha do tempo.
    """
    from apps.inbox.models import Message

    encerramento = (
        Message.objects.filter(
            conversation=conversation,
            kind=MessageKind.ACTIVITY,
            activity_type=ActivityType.RESOLVED,
        )
        .order_by("-wa_timestamp")
        .values_list("wa_timestamp", flat=True)
        .first()
    )
    falas = Message.objects.filter(
        conversation=conversation, sender_kind=SenderKind.CONTACT, is_internal=False
    )
    if encerramento is not None:
        falas = falas.filter(wa_timestamp__gt=encerramento)
    return falas.count() <= 1


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


def _repicou_demais(conversation) -> bool:
    """
    Trava do redisparo em série (RF-FLW-23.2).

    Ao entregar, a conversa volta para a fila com posse `none` - e aí a mesma
    palavra-chave dispara um fluxo NOVO. A trava do banco (RF-FLW-6) só impede
    duas execuções ATIVAS ao mesmo tempo, não uma fila infinita em sequência,
    que é o que um robô do outro lado produziria.

    Três por hora deixa passar o paciente que errou e tentou de novo.

    ⚠️ **Disparo de SEQUÊNCIA não entra na conta** (18/08/2026, decisão do
    usuário depois de bater nisto ao vivo). Cada passo de trilha é uma execução
    de fluxo, então uma campanha de três passos consumia a cota inteira do
    contato e a pessoa ficava sem conseguir usar palavra-chave nenhuma pela
    hora seguinte. Aconteceu exatamente assim: seis passos de sequência em
    vinte minutos e o "agendar teste" recusado duas vezes em seguida.

    A trava é contra o repique do PACIENTE em conversa; passo agendado pela
    clínica é calendário, e calendário não repica.
    """
    from django.utils import timezone

    from apps.automation.models import FlowRun, SequenceDispatch

    if conversation.contact_id is None:
        return False
    desde = timezone.now() - timedelta(hours=1)

    # O vínculo já existe e é o do painel (RF-SEQ-11.3): o disparo guarda a
    # execução que gerou. Recortado pela mesma janela para a subconsulta não
    # crescer com o histórico da clínica.
    de_sequencia = SequenceDispatch.objects.filter(
        flow_run__isnull=False, resolved_at__gte=desde
    ).values("flow_run_id")

    quantas = (
        FlowRun.objects.filter(
            clinic=conversation.clinic,
            contact_id=conversation.contact_id,
            created_at__gte=desde,
            deleted_at__isnull=True,
        )
        .exclude(pk__in=de_sequencia)
        .count()
    )
    if quantas < MAX_RUNS_POR_HORA:
        return False
    logger.warning(
        "Contato %s já teve %s execuções na última hora: fluxo não dispara "
        "(a conversa fica para a recepção)",
        conversation.contact_id,
        quantas,
    )
    _avisar_que_o_robo_se_conteve(conversation, quantas)
    return True


def _avisar_que_o_robo_se_conteve(conversation, quantas: int) -> None:
    """
    Deixa uma NOTA INTERNA quando a trava recusa (18/08/2026).

    Sem isto a recusa vivia só no log do servidor: quem está com a conversa
    aberta via o paciente escrever e o robô não responder, e concluía que o
    sistema tinha engolido a mensagem. Nota interna não sai para o paciente
    (RF-ATD-3) e aparece na thread de quem atende, que é exatamente onde a
    dúvida nasce.

    ⚠️ UMA por hora, no máximo. O paciente que repica manda várias mensagens
    seguidas, e uma nota por mensagem entulharia a conversa com o aviso de que
    a conversa está entulhada.
    """
    from django.utils import timezone

    from apps.inbox.choices import MessageKind, SenderKind
    from apps.inbox.models import Message

    marca = "[robô contido]"
    desde = timezone.now() - timedelta(hours=1)
    if Message.objects.filter(
        conversation=conversation,
        is_internal=True,
        body__startswith=marca,
        created_at__gte=desde,
    ).exists():
        return

    Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=MessageKind.TEXT,
        sender_kind=SenderKind.BOT,
        is_internal=True,
        body=(
            f"{marca} O atendimento automático não respondeu esta mensagem "
            f"porque já houve {quantas} conversas automáticas com este contato "
            "na última hora. É uma trava contra repetição. A conversa fica com "
            "a recepção."
        ),
        wa_timestamp=timezone.now(),
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

    # A trava do redisparo em série vem DEPOIS do descarte do botão e ANTES de
    # escolher o fluxo: contar execução para decidir e depois não usar o
    # resultado seria uma query à toa em toda mensagem de menu (RF-FLW-23.2).
    if _repicou_demais(conversation):
        return None

    aberta = _clinic_is_open(conversation.clinic)
    primeira = None  # calculado sob demanda: a contagem é uma query
    atendimento_novo = None

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
            continue

        if flow.trigger == FlowTrigger.NEW_CONVERSATION:
            # Calculado sob demanda, como o de cima: são duas consultas, e
            # cobrá-las em toda mensagem de menu seria pagar por nada.
            if atendimento_novo is None:
                atendimento_novo = _is_new_conversation(conversation)
            if atendimento_novo:
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
