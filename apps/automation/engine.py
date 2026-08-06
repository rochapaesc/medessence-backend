"""
O motor de fluxos (§4.3.2, RF-FLW-7 a RF-FLW-12).

É a peça que o protótipo do cliente não tinha, e sem a qual o canvas é um
editor de desenho: aqui é onde o "oi" das 22h de sexta vira uma execução que
lembra, no sábado de manhã, em que nó aquele paciente parou.

Três invariantes que valem mais do que a elegância do código:

1. **Antes de CADA envio, confere de quem é a caneta** (RF-FLW-10). O motor é
   assíncrono: entre decidir a mensagem e entregá-la à Meta, uma recepcionista
   pode ter assumido. Sem essa releitura, o paciente recebe a fala do robô
   depois de a pessoa já ter respondido - que é justamente o que este módulo
   existe para impedir.
2. **Humano assumiu, o robô não volta sozinho** (RF-FLW-9), nem quando o
   paciente responde de novo.
3. **Um teto de passos por avanço.** O validador recusa laço infinito, mas
   fluxo semeado direto no banco não passou por ele, e o preço de um laço em
   produção é a Meta bloquear o número da clínica por spam.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.automation.choices import (
    EDGE_BUTTON_PREFIX,
    EDGE_DEFAULT,
    EDGE_FALSE,
    EDGE_ROW_PREFIX,
    EDGE_TRUE,
    ConditionOperator,
    ConditionSubject,
    FlowNodeType,
    FlowRunEventType,
    FlowRunStatus,
)
from apps.automation.graph import FlowGraph
from apps.automation.models import FlowRun, FlowRunEvent
from apps.inbox.choices import AttendedBy, ConversationStatus, MessageKind, SenderKind

logger = logging.getLogger(__name__)

# Teto de nós executados num único avanço. Ninguém desenha um fluxo com 25
# passos entre duas falas do paciente; um número maior só adiaria a
# descoberta de um laço que o validador deixou passar.
MAX_STEPS_PER_ADVANCE = 25

# Sentinela: o nó parou e espera o paciente (ou o relógio).
WAIT_FOR_CONTACT = object()

# O estado da execução, gravado junto a cada passo.
_ESTADO = ["current_node", "reprompt_count", "last_advanced_at", "wake_at", "vars", "updated_at"]


class _PosseTrocadaError(Exception):
    """Interrompe a cadeia porque a posse mudou no meio dela."""


# --------------------------------------------------------------------- #
# Variáveis
# --------------------------------------------------------------------- #


def interpolate(text: str, vars: dict) -> str:
    """
    Troca `{{chave}}` pelo que foi coletado.

    Chave que não existe vira string vazia, e não `{{chave}}` no texto: o
    paciente não pode ler chaves de programador porque o gestor errou o nome
    da variável.
    """
    if not text or "{{" not in text:
        return text or ""
    saida = text
    for chave, valor in (vars or {}).items():
        saida = saida.replace("{{" + str(chave) + "}}", str(valor))
    # Sobrou chave sem valor: some.
    while "{{" in saida and "}}" in saida:
        inicio = saida.index("{{")
        fim = saida.index("}}", inicio)
        saida = saida[:inicio] + saida[fim + 2 :]
    return saida


# --------------------------------------------------------------------- #
# Posse
# --------------------------------------------------------------------- #


def _bot_still_holds(conversation) -> bool:
    """
    RF-FLW-10. Relê do BANCO, não do objeto em memória: o objeto foi carregado
    antes de a colega clicar em "Assumir atendimento".
    """
    from apps.inbox.models import Conversation

    return Conversation.objects.filter(pk=conversation.pk, attended_by=AttendedBy.BOT).exists()


def _claim_for_bot(conversation) -> bool:
    """
    Toma a caneta para o robô, e só se ela estiver livre.

    UPDATE condicionado ao estado esperado, como o `take_over` humano: se uma
    atendente pegou a conversa entre a checagem e aqui, nenhuma linha casa e o
    fluxo não começa.
    """
    from apps.inbox.models import Conversation

    trocou = Conversation.objects.filter(pk=conversation.pk, attended_by=AttendedBy.NONE).update(
        attended_by=AttendedBy.BOT,
        attended_since=timezone.now(),
        status=ConversationStatus.OPEN,
        waiting_since=None,
        updated_at=timezone.now(),
    )
    if trocou:
        conversation.refresh_from_db()
    return bool(trocou)


def _release_to_queue(conversation, *, activity: str | None = None, data: dict | None = None):
    """
    Devolve a conversa para a fila humana: sem dono e Aguardando.

    Nunca deixa a conversa "com o robô" ao encerrar - conversa presa ao bot
    fica invisível para a recepção, que é o pior fim possível.
    """
    from apps.inbox.attendance import log_activity
    from apps.inbox.models import Conversation

    Conversation.objects.filter(pk=conversation.pk, attended_by=AttendedBy.BOT).update(
        attended_by=AttendedBy.NONE,
        attended_since=None,
        status=ConversationStatus.WAITING,
        waiting_since=timezone.now(),
        updated_at=timezone.now(),
    )
    conversation.refresh_from_db()
    if activity:
        log_activity(conversation, activity, data=data or {})


# --------------------------------------------------------------------- #
# Envio
# --------------------------------------------------------------------- #


def _send(run, conversation, *, body: str, kind: str = MessageKind.TEXT, content_data=None):
    """
    Cria a mensagem do robô e a põe na FILA DO AVANÇO, conferindo a posse.

    ⚠️ NÃO enfileira uma task por mensagem. O worker roda com concorrência 4:
    duas tasks disparadas juntas viram duas chamadas paralelas à Meta, e quem
    responder primeiro chega primeiro. Foi assim que a saudação chegou DEPOIS
    do menu no teste ao vivo. Quem envia é `_despachar`, no fim do avanço, uma
    de cada vez e na ordem em que o fluxo as produziu.
    """
    from apps.inbox.models import Message

    if not _bot_still_holds(conversation):
        raise _PosseTrocadaError

    message = Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=kind,
        sender_kind=SenderKind.BOT,
        body=body,
        content_data=content_data or {},
        wa_timestamp=timezone.now(),
    )
    _fila_do_avanco.setdefault(run.pk, []).append(message.pk)
    _log(run, FlowRunEventType.SENT, run.current_node, {"message_id": message.pk})
    return message


# As falas produzidas no avanço corrente, por execução. Vive só durante a
# chamada: `_despachar` esvazia no fim.
_fila_do_avanco: dict[int, list[int]] = {}


def _despachar(run) -> None:
    """
    Manda para a fila as falas deste avanço, EM ORDEM, numa task só.

    Uma task por avanço (e não por mensagem) é o que garante a ordem: dentro
    dela os envios são sequenciais.
    """
    from apps.automation.tasks import enviar_falas_do_fluxo

    ids = _fila_do_avanco.pop(run.pk, [])
    if not ids:
        return
    transaction.on_commit(lambda: enviar_falas_do_fluxo.delay(run.pk, ids))


def _log(run, event_type: str, node_key: str = "", data: dict | None = None):
    FlowRunEvent.objects.create(
        run=run, node_key=node_key or "", event_type=event_type, data=data or {}
    )


# --------------------------------------------------------------------- #
# Execução de um nó
# --------------------------------------------------------------------- #


def _clinic_is_open(clinic) -> bool:
    """
    A clínica está aberta AGORA, no fuso dela (RF-FLW-5.1).

    Dia sem faixa nenhuma = fechado. Comparar com o relógio do servidor poria
    a clínica de Fortaleza abrindo às 5h.

    ⚠️ Olha TODAS as faixas do dia, e não a primeira (corrigido em
    05/08/2026). Com `.first()`, a clínica que fecha das 12h às 14h contava
    como aberta no almoço inteiro, porque só a faixa da manhã era consultada e
    o fim dela virava o fim do expediente.
    """
    from zoneinfo import ZoneInfo

    agora = timezone.now().astimezone(ZoneInfo(clinic.timezone))
    hora = agora.time()
    return clinic.business_hours.filter(
        weekday=agora.weekday(), opens_at__lte=hora, closes_at__gte=hora
    ).exists()


def _eval_condition(node, run) -> str:
    """Devolve `true` ou `false` - as duas únicas saídas de um CONDITION."""
    cfg = node.config
    subject = cfg.get("subject")

    if subject == ConditionSubject.BUSINESS_HOURS:
        return EDGE_TRUE if _clinic_is_open(run.clinic) else EDGE_FALSE

    valor = str((run.vars or {}).get(cfg.get("subject_key") or "", "") or "")
    operator = cfg.get("operator")
    alvo = str(cfg.get("value") or "")

    if operator == ConditionOperator.PRESENT:
        ok = bool(valor.strip())
    elif operator == ConditionOperator.ABSENT:
        ok = not valor.strip()
    elif operator == ConditionOperator.CONTAINS:
        ok = alvo.casefold() in valor.casefold()
    else:  # EQUALS
        ok = valor.strip().casefold() == alvo.strip().casefold()
    return EDGE_TRUE if ok else EDGE_FALSE


def _execute(node, run, conversation):
    """
    Roda UM nó. Devolve a condição de saída, ou `WAIT_FOR_CONTACT` quando o nó
    para e espera, ou None quando o fluxo termina ali.
    """
    cfg = node.config
    vars_ = run.vars or {}

    if node.type == FlowNodeType.START:
        return EDGE_DEFAULT

    if node.type == FlowNodeType.SEND_MESSAGE:
        _send(run, conversation, body=interpolate(cfg.get("text") or "", vars_))
        return EDGE_DEFAULT

    if node.type == FlowNodeType.SEND_BUTTONS:
        _send(
            run,
            conversation,
            body=interpolate(cfg.get("text") or "", vars_),
            kind=MessageKind.INTERACTIVE,
            content_data={
                "buttons": [
                    {"id": b["id"], "title": interpolate(b.get("title") or "", vars_)}
                    for b in cfg.get("buttons") or []
                    if b.get("id")
                ]
            },
        )
        return WAIT_FOR_CONTACT

    if node.type == FlowNodeType.SEND_LIST:
        linhas = [r for r in cfg.get("rows") or [] if r.get("id")]
        _send(
            run,
            conversation,
            body=interpolate(cfg.get("text") or "", vars_),
            kind=MessageKind.INTERACTIVE,
            content_data={
                "list": {
                    "button_label": cfg.get("button_label") or "Ver opções",
                    "sections": [
                        {
                            "title": cfg.get("section_title") or "",
                            "rows": [
                                {"id": r["id"], "title": interpolate(r.get("title") or "", vars_)}
                                for r in linhas
                            ],
                        }
                    ],
                }
            },
        )
        return WAIT_FOR_CONTACT

    if node.type == FlowNodeType.SEND_MEDIA:
        # A mídia por URL ainda não tem caminho de envio próprio (o
        # `_enviar_anexo` parte de um MediaAsset nosso). Manda a legenda com o
        # endereço, que é degradação honesta, e segue.
        legenda = interpolate(cfg.get("caption") or "", vars_)
        _send(run, conversation, body=f"{legenda}\n{cfg.get('media_url') or ''}".strip())
        return EDGE_DEFAULT

    if node.type == FlowNodeType.SEND_TEMPLATE:
        _send(
            run,
            conversation,
            body=interpolate(cfg.get("text") or "", vars_),
            kind=MessageKind.TEMPLATE,
        )
        return EDGE_DEFAULT

    if node.type == FlowNodeType.COLLECT_INPUT:
        _send(run, conversation, body=interpolate(cfg.get("prompt_text") or "", vars_))
        return WAIT_FOR_CONTACT

    if node.type == FlowNodeType.CONDITION:
        return _eval_condition(node, run)

    if node.type == FlowNodeType.SET_LABEL:
        _apply_label(node, conversation)
        return EDGE_DEFAULT

    if node.type == FlowNodeType.WAIT:
        run.wake_at = _wake_at(cfg)
        return WAIT_FOR_CONTACT

    if node.type == FlowNodeType.HANDOFF:
        _finish(
            run,
            conversation,
            status=FlowRunStatus.HANDED_OFF,
            reason="handoff",
            note=interpolate(cfg.get("note") or "", vars_),
        )
        return None

    if node.type == FlowNodeType.END:
        _finish(run, conversation, status=FlowRunStatus.COMPLETED, reason="end")
        return None

    logger.warning("Nó de tipo desconhecido no fluxo %s: %s", run.flow_id, node.type)
    return None


def _wake_at(cfg):
    from datetime import timedelta

    unidade = cfg.get("unit") or "minutes"
    quantidade = int(cfg.get("amount") or 0)
    segundos = {
        "seconds": 1,
        "minutes": 60,
        "hours": 3600,
        "days": 86400,
    }.get(unidade, 60)
    return timezone.now() + timedelta(seconds=quantidade * segundos)


def _apply_label(node, conversation):
    """
    RF-FLW-13.1 - `ConversationLabel`, NUNCA `patients.Tag`.

    A Tag sincroniza com a vSaúde: um fluxo marcando "lead-quente" tentaria
    escrever no prontuário do paciente.
    """
    from apps.inbox.models import ConversationLabel

    label = ConversationLabel.objects.filter(
        pk=node.config.get("label_id"), clinic=conversation.clinic, is_active=True
    ).first()
    if label:
        conversation.labels.add(label)


def _finish(run, conversation, *, status: str, reason: str, note: str = ""):
    """Encerra a execução e SEMPRE devolve a conversa para a fila humana."""
    from apps.inbox.choices import ActivityType

    run.status = status
    run.ended_at = timezone.now()
    run.end_reason = reason
    run.save(update_fields=["status", "ended_at", "end_reason", "updated_at"])

    _release_to_queue(
        conversation,
        activity=ActivityType.BOT_HANDOFF,
        data={"flow": run.flow.name, "note": note, "reason": reason},
    )
    _log(run, FlowRunEventType.ENDED, run.current_node, {"reason": reason})
    # As falas deste avanço saem mesmo com a execução encerrada: a confirmação
    # antes do fim é justamente a última coisa que o paciente precisa ler.
    _despachar(run)


# --------------------------------------------------------------------- #
# O laço
# --------------------------------------------------------------------- #


def advance(run, *, from_outcome: str | None = None) -> None:
    """
    Avança a execução até parar num nó que espera, ou até o fluxo terminar.

    `from_outcome` é a resposta que o paciente acabou de dar; sem ela, começa
    executando o nó corrente (é o caso do início da execução).
    """
    if run.status != FlowRunStatus.ACTIVE:
        return

    conversation = run.conversation
    graph = FlowGraph(run.version.graph)
    node_id = run.current_node

    if from_outcome is not None:
        node_id = graph.resolve(run.current_node, from_outcome)
        if not node_id:
            # Resposta que não casa com saída nenhuma e o nó não tem
            # `default`: é o caso do reprompt, tratado por quem chamou.
            return

    try:
        for _ in range(MAX_STEPS_PER_ADVANCE):
            node = graph.node(node_id)
            if not node:
                _finish(run, conversation, status=FlowRunStatus.FAILED, reason="no_ausente")
                return

            run.current_node = node.id
            run.reprompt_count = 0
            run.last_advanced_at = timezone.now()
            run.wake_at = None
            # Grava ANTES de executar: se o processo morrer no meio do envio,
            # a retomada precisa saber em que nó a execução estava. Gravar só
            # ao parar perdia as variáveis coletadas quando o fluxo terminava
            # sem passar por um nó de espera.
            run.save(update_fields=_ESTADO)
            _log(run, FlowRunEventType.ENTERED, node.id)

            outcome = _execute(node, run, conversation)

            if outcome is None:  # terminou dentro do _execute
                return
            if outcome is WAIT_FOR_CONTACT:
                # De novo, porque o nó "Aguardar" define `wake_at` durante a
                # execução, depois da gravação acima.
                run.save(update_fields=_ESTADO)
                _despachar(run)
                return

            proximo = graph.resolve(node.id, outcome)
            if not proximo:
                _finish(run, conversation, status=FlowRunStatus.COMPLETED, reason="sem_saida")
                return
            node_id = proximo
        else:
            # Estourou o teto: laço que o validador não pegou.
            logger.error("Fluxo %s girou %s passos sem parar", run.flow_id, MAX_STEPS_PER_ADVANCE)
            _finish(run, conversation, status=FlowRunStatus.FAILED, reason="laco")
    except _PosseTrocadaError:
        # Alguém assumiu no meio da cadeia (RF-FLW-9/10). O que já tinha sido
        # montado é DESCARTADO: a pessoa está conduzindo agora.
        _fila_do_avanco.pop(run.pk, None)
        pause_for_agent(run)


# --------------------------------------------------------------------- #
# Entradas públicas
# --------------------------------------------------------------------- #


def start_run(flow, conversation) -> FlowRun | None:
    """
    Começa uma execução para o contato desta conversa.

    Devolve None quando não deu para começar: já havia execução ativa (a
    trava do banco recusa a segunda - RF-FLW-6) ou a conversa não estava
    livre. Nos dois casos é no-op de propósito, não erro.
    """
    version = flow.current_version
    if not version:
        return None

    entry = FlowGraph(version.graph).entry_node
    if not entry:
        return None

    if not _claim_for_bot(conversation):
        return None

    try:
        with transaction.atomic():
            run = FlowRun.objects.create(
                clinic=conversation.clinic,
                flow=flow,
                version=version,
                contact=conversation.contact,
                conversation=conversation,
                current_node=entry,
            )
    except IntegrityError:
        # A trava do banco (RF-FLW-6): outra entrega do mesmo webhook chegou
        # primeiro. Devolve a caneta, porque quem começou de verdade foi ela.
        _release_to_queue(conversation)
        return None

    from apps.inbox.attendance import log_activity
    from apps.inbox.choices import ActivityType

    log_activity(conversation, ActivityType.BOT_STARTED, data={"flow": flow.name})
    advance(run)
    return run


@transaction.atomic
def on_inbound(conversation, message) -> bool:
    """
    O paciente falou. Devolve True se um fluxo consumiu a mensagem.

    Chamado pela ingestão. A resposta vira a condição de saída: id do botão
    tocado, ou o texto digitado quando o nó é de coleta.

    ⚠️ **A execução é TRAVADA enquanto avança** (06/08/2026). O paciente manda
    três mensagens em rajada, o worker roda com concorrência 4, e as três liam
    o MESMO `current_node`: as três avançavam a partir dele e o paciente
    recebia a mesma fala duas ou três vezes. Com o lock, a segunda espera a
    primeira terminar e então lê o nó já atualizado, que é onde ela pertence.

    A reentrega do MESMO webhook não chega até aqui: a ingestão já a descarta
    pelo `provider_message_id` (`uniq_message_wamid`).
    """
    run = (
        FlowRun.objects.select_for_update(of=("self",))
        .filter(
            clinic=conversation.clinic,
            conversation=conversation,
            status=FlowRunStatus.ACTIVE,
        )
        .select_related("flow", "version")
        .first()
    )
    if not run:
        return False

    if not _bot_still_holds(conversation):
        # Um humano assumiu e o paciente respondeu para ELE (RF-FLW-9).
        pause_for_agent(run)
        return False

    run.conversation = conversation
    graph = FlowGraph(run.version.graph)
    node = graph.node(run.current_node)
    if not node:
        _finish(run, conversation, status=FlowRunStatus.FAILED, reason="no_ausente")
        return False

    _log(run, FlowRunEventType.REPLIED, node.id, {"message_id": message.pk})

    escolha = (message.content_data or {}).get("interactive_id") or ""
    texto = (message.body or "").strip()

    if node.type in (FlowNodeType.SEND_BUTTONS, FlowNodeType.SEND_LIST):
        botoes = node.type == FlowNodeType.SEND_BUTTONS
        chave_das_opcoes = "buttons" if botoes else "rows"
        prefixo = EDGE_BUTTON_PREFIX if botoes else EDGE_ROW_PREFIX

        if escolha:
            # ⚠️ O toque tem de ser numa opção DESTE nó (06/08/2026). O
            # paciente rola a conversa e toca num botão de três passos atrás;
            # sem esta conferência, um id repetido entre nós ("sim", "nao")
            # casaria a aresta errada e o fluxo pularia para outro lugar.
            if escolha not in _ids_das_opcoes(node, chave_das_opcoes):
                _reprompt(run, node, conversation)
                return True
            outcome = f"{prefixo}{escolha}"
        else:
            # Digitou em vez de tocar: só avança se o autor do fluxo previu
            # uma saída com esse texto; senão cai no reprompt logo abaixo.
            outcome = texto
        _guardar_escolha(run, node, escolha, texto, chave_das_opcoes)

    elif node.type == FlowNodeType.COLLECT_INPUT:
        # ⚠️ Toque em botão NÃO é resposta a uma pergunta aberta (06/08/2026).
        # O WhatsApp manda o id E o título; sem esta guarda, tocar num botão
        # velho enquanto o robô esperava o nome gravava `nome = "Marcar
        # consulta"` e seguia em frente sem reclamar. A recepção só descobria
        # ligando para o paciente.
        if escolha:
            _reprompt(run, node, conversation)
            return True

        chave = node.config.get("var_key")
        if chave:
            run.vars = {**(run.vars or {}), chave: texto}
            # Grava já: o que o paciente respondeu não pode depender de o
            # avanço seguinte chegar a um nó de espera para ser persistido.
            run.save(update_fields=["vars", "updated_at"])
        outcome = EDGE_DEFAULT
    else:
        outcome = EDGE_DEFAULT

    if graph.resolve(node.id, outcome) is None:
        _reprompt(run, node, conversation)
        return True

    advance(run, from_outcome=outcome)
    return True


def _ids_das_opcoes(node, chave_das_opcoes: str) -> set[str]:
    return {
        str(o.get("id"))
        for o in (node.config.get(chave_das_opcoes) or [])
        if o.get("id")
    }


def _guardar_escolha(run, node, escolha: str, texto: str, chave_das_opcoes: str) -> None:
    """
    Guarda o que o paciente ESCOLHEU numa variável (RF-FLW-14).

    Nasceu em 06/08/2026, no teste ao vivo. Sem isto, a única forma de saber o
    que a pessoa respondeu era perguntar por texto livre, e aí a resposta vinha
    como ela quisesse: à pergunta "você tem convênio?" o paciente respondeu
    "Tenho", e a recepção abriu a conversa com `Pagamento: Tenho`. Pior, o
    TIPO DE ATENDIMENTO escolhido na lista não chegava na nota de jeito nenhum,
    que é justamente o dado de que quem agenda mais precisa.

    Guarda o TÍTULO e não o id: a variável existe para ser lida por gente, e
    "Consulta presencial" diz o que `presencial` não diz. Quem precisa do id
    para desviar o fluxo usa a ARESTA (`button:x`), que não passa por aqui.
    """
    chave = (node.config.get("var_key") or "").strip()
    if not chave:
        return

    titulo = ""
    if escolha:
        for opcao in node.config.get(chave_das_opcoes) or []:
            if str(opcao.get("id")) == escolha:
                titulo = str(opcao.get("title") or "")
                break
    # Sem toque em botão (a pessoa digitou), fica o que ela escreveu: melhor
    # do que variável vazia numa nota que alguém vai ler.
    run.vars = {**(run.vars or {}), chave: titulo or texto}
    run.save(update_fields=["vars", "updated_at"])


def _reprompt(run, node, conversation):
    """
    A resposta não casou com saída nenhuma (RF-FLW-11).

    Repete a pergunta até o teto da política; esgotado, entrega ao humano.
    Insistir para sempre é o que faz o paciente desistir da clínica.
    """
    politica = run.flow.fallback or {}
    teto = int(politica.get("max_reprompts") or 0)

    if run.reprompt_count >= teto:
        _finish(run, conversation, status=FlowRunStatus.HANDED_OFF, reason="reprompt_esgotado")
        return

    run.reprompt_count += 1
    run.last_advanced_at = timezone.now()
    run.save(update_fields=["reprompt_count", "last_advanced_at", "updated_at"])
    _log(run, FlowRunEventType.REPROMPT, node.id, {"tentativa": run.reprompt_count})

    try:
        _execute(node, run, conversation)
        _despachar(run)
    except _PosseTrocadaError:
        pause_for_agent(run)


def pause_for_agent(run) -> None:
    """
    Um humano assumiu (RF-FLW-9). A execução para e NÃO volta sozinha, nem
    quando o paciente responde de novo. Devolver à máquina é ato explícito.
    """
    if run.status != FlowRunStatus.ACTIVE:
        return
    run.status = FlowRunStatus.PAUSED_BY_AGENT
    run.ended_at = timezone.now()
    run.end_reason = "assumido"
    run.save(update_fields=["status", "ended_at", "end_reason", "updated_at"])
    _log(run, FlowRunEventType.HANDOFF, run.current_node, {"motivo": "assumido"})
