"""
Modo de teste do fluxo (RF-FLW-25): o gestor conversa com o RASCUNHO.

O desenho inteiro está no §4.3.2. A frase que o resume: **o teste roda o motor
de verdade**, numa conversa que não alcança ninguém. A conversa vive num canal
interno FAKE (`Channel.is_test`), o envio curto-circuita onde o canal de
demonstração já curto-circuita, e o resto é o mesmo código que atende paciente.

A alternativa estudada e rejeitada foi a do whatomate: simular o fluxo no
navegador. Vira um segundo motor, e dois motores divergem na primeira mudança.
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.automation.choices import FlowRunEventType, FlowRunStatus, FlowTrigger
from apps.automation.graph import validate_graph
from apps.automation.models import FlowRun, FlowRunEvent
from apps.automation.triggers import _is_first_inbound, _matches_keyword
from apps.inbox.choices import (
    AttendedBy,
    ConversationStatus,
    MessageKind,
    SenderKind,
    WhatsAppProviderKind,
)
from apps.inbox.models import Channel, Conversation, Message
from apps.patients.models import Contact

logger = logging.getLogger(__name__)

# O contato de teste usa este prefixo: número impossível (DDD 00 não existe),
# o mesmo raciocínio dos 5500 do seed. O sufixo é o id do fluxo.
PREFIXO_DO_CONTATO = "0000"


def _canal_de_teste(clinic):
    canal, _ = Channel.objects.get_or_create(
        clinic=clinic,
        is_test=True,
        defaults={
            "provider": WhatsAppProviderKind.FAKE,
            "display_number": "teste interno",
        },
    )
    return canal


def _contato_de_teste(flow):
    contato, _ = Contact.objects.get_or_create(
        clinic=flow.clinic,
        wa_id=f"{PREFIXO_DO_CONTATO}{flow.pk:011d}",
        defaults={"display_name": "Paciente de teste"},
    )
    return contato


def _conversa_de_teste(flow, *, criar=True):
    canal = _canal_de_teste(flow.clinic)
    contato = _contato_de_teste(flow)
    if not criar:
        return Conversation.objects.filter(
            clinic=flow.clinic, channel=canal, contact=contato
        ).first()
    conversa, _ = Conversation.objects.get_or_create(
        clinic=flow.clinic,
        channel=canal,
        contact=contato,
        defaults={
            "status": ConversationStatus.WAITING,
            "attended_by": AttendedBy.NONE,
            "waiting_since": timezone.now(),
        },
    )
    return conversa


def _problemas(flow):
    """
    Estruturais barram, aprovação de template só avisa (RF-FLW-25.4).

    O truque é o que o seeder já descobriu: `validate_graph` SEM clínica é só
    estrutura; COM clínica cobra também template aprovado. A diferença entre
    as duas listas é exatamente o conjunto dos avisos.
    """
    graph = (flow.current_version.graph or {}) if flow.current_version else {}
    estruturais = validate_graph(graph, None)
    completos = validate_graph(graph, flow.clinic)
    avisos = [p for p in completos if p not in estruturais]
    return estruturais, avisos


@transaction.atomic
def iniciar_teste(flow):
    """
    Abre (ou zera) a sessão de teste do fluxo.

    Zerar é apagar as mensagens da conversa de teste e encerrar a execução
    anterior: o teste recomeça sempre do silêncio, como uma conversa nova.
    """
    conversa = _conversa_de_teste(flow)

    # As execuções de teste antigas saem JUNTO com as mensagens: deixá-las
    # faria o retrato achar a encerrada e dizer "terminou" numa conversa
    # zerada. Eventos caem em cascata.
    FlowRun.objects.filter(conversation=conversa, is_test=True).delete()

    apagar = Message.all_objects if hasattr(Message, "all_objects") else Message.objects
    fila = apagar.filter(conversation=conversa)
    fila.hard_delete() if hasattr(fila, "hard_delete") else fila.delete()

    Conversation.objects.filter(pk=conversa.pk).update(
        status=ConversationStatus.WAITING,
        attended_by=AttendedBy.NONE,
        waiting_since=timezone.now(),
        last_message_at=None,
        unread_count=0,
    )
    conversa.refresh_from_db()
    return retrato(flow)


@transaction.atomic
def falar_no_teste(flow, *, texto="", interactive_id=""):
    """
    O gestor falou como o paciente. Mesma mecânica da ingestão, na ordem dela:
    primeiro continuar a execução em andamento, depois cogitar começar.
    """
    from apps.automation.engine import on_inbound

    conversa = _conversa_de_teste(flow, criar=False)
    if conversa is None:
        return retrato(flow, notas=["O teste ainda não foi aberto."])

    mensagem = Message.objects.create(
        clinic=flow.clinic,
        conversation=conversa,
        sender_kind=SenderKind.CONTACT,
        kind=MessageKind.INTERACTIVE if interactive_id else MessageKind.TEXT,
        body=texto,
        content_data={"interactive_id": interactive_id} if interactive_id else {},
        wa_timestamp=timezone.now(),
    )
    Conversation.objects.filter(pk=conversa.pk).update(last_message_at=timezone.now())
    conversa.refresh_from_db()

    if on_inbound(conversa, mensagem):
        return retrato(flow)

    # Nenhuma execução em voo: vale o GATILHO deste fluxo (RF-FLW-25.2).
    notas = []
    if flow.trigger == FlowTrigger.MANUAL:
        notas.append(
            "Este fluxo é de disparo manual. Use o botão Começar como a recepção."
        )
        return retrato(flow, notas=notas)

    if interactive_id:
        # Toque em botão nunca começa fluxo, igual à produção.
        return retrato(flow, notas=["Toque em botão não começa fluxo. Digite uma mensagem."])

    if flow.trigger == FlowTrigger.KEYWORD and not _matches_keyword(
        texto, flow.trigger_config or {}
    ):
        palavras = ", ".join((flow.trigger_config or {}).get("keywords") or [])
        notas.append(
            f"Essa mensagem não tem nenhuma das palavras do gatilho ({palavras}). "
            "O fluxo não começaria."
        )
        return retrato(flow, notas=notas)

    if flow.trigger == FlowTrigger.FIRST_INBOUND and not _is_first_inbound(conversa):
        notas.append(
            "Não é a primeira mensagem da conversa, então este gatilho não vale "
            "mais. Recomece o teste para tentar de novo."
        )
        return retrato(flow, notas=notas)

    if flow.only_outside_hours:
        # No teste o expediente não trava (RF-FLW-25.2), mas a diferença é dita.
        notas.append(
            "Fora do teste, este fluxo só começa com a clínica fechada."
        )

    if _comecar(flow, conversa) is None:
        notas.append("O fluxo não conseguiu começar. Confira se o desenho tem início.")
    return retrato(flow, notas=notas)


@transaction.atomic
def comecar_manual(flow):
    """O botão "Começar como a recepção" dos fluxos de disparo manual."""
    conversa = _conversa_de_teste(flow)
    if _comecar(flow, conversa) is None:
        return retrato(flow, notas=["O fluxo não conseguiu começar. Confira o desenho."])
    return retrato(flow)


def _comecar(flow, conversa):
    from apps.automation.engine import start_run

    return start_run(flow, conversa, is_test=True)


@transaction.atomic
def pular_espera(flow):
    """Adianta o relógio do nó Aguardar (RF-FLW-25.3)."""
    from apps.automation.engine import advance

    conversa = _conversa_de_teste(flow, criar=False)
    run = (
        FlowRun.objects.filter(
            conversation=conversa, is_test=True, status=FlowRunStatus.ACTIVE
        )
        .select_related("flow", "version", "conversation")
        .first()
        if conversa
        else None
    )
    if run is None or not run.wake_at:
        return retrato(flow, notas=["Não há espera para pular agora."])

    # O mesmo caminho da varredura (`advance_flow_run`): quem sabe sair do nó
    # de espera é o próprio avanço.
    advance(run, from_outcome="default")
    return retrato(flow)


@transaction.atomic
def encerrar_teste(flow):
    """Apaga o rastro: mensagens fora, execução encerrada (RF-FLW-25.5)."""
    conversa = _conversa_de_teste(flow, criar=False)
    if conversa is None:
        return
    FlowRun.objects.filter(
        conversation=conversa, is_test=True, status=FlowRunStatus.ACTIVE
    ).update(status=FlowRunStatus.COMPLETED, updated_at=timezone.now())
    apagar = Message.all_objects if hasattr(Message, "all_objects") else Message.objects
    fila = apagar.filter(conversation=conversa)
    fila.hard_delete() if hasattr(fila, "hard_delete") else fila.delete()


# ------------------------------------------------------------------ #
# O retrato: tudo que o painel precisa, numa resposta só.
# ------------------------------------------------------------------ #


def retrato(flow, *, notas=None):
    """
    O estado inteiro da sessão: pendências, gatilho, linhas da conversa,
    variáveis, espera e fim. O painel redesenha a partir dele, sem delta.
    """
    estruturais, avisos = _problemas(flow)
    conversa = _conversa_de_teste(flow, criar=False)
    run = (
        FlowRun.objects.filter(conversation=conversa, is_test=True)
        .order_by("-pk")
        .first()
        if conversa
        else None
    )

    linhas = _linhas(conversa, run) if conversa else []

    situacao = "sem_conversa"
    espera = None
    fim = None
    if estruturais:
        situacao = "bloqueado"
    elif run is None:
        situacao = "esperando_comecar"
    elif run.status == FlowRunStatus.ACTIVE and run.wake_at:
        situacao = "esperando_o_relogio"
        espera = {"ate": run.wake_at}
    elif run.status == FlowRunStatus.ACTIVE:
        situacao = "esperando_voce"
    else:
        situacao = "terminou"
        ultimo = (
            FlowRunEvent.objects.filter(
                run=run, event_type__in=[FlowRunEventType.ENDED, FlowRunEventType.HANDOFF]
            )
            .order_by("-pk")
            .first()
        )
        fim = {
            "status": run.status,
            "motivo": (ultimo.data or {}).get("reason", "") if ultimo else "",
        }

    return {
        "situacao": situacao,
        "problemas": estruturais,
        "avisos": avisos,
        "notas": notas or [],
        "gatilho": {
            "tipo": flow.trigger,
            "palavras": (flow.trigger_config or {}).get("keywords") or [],
        },
        "no_atual": run.current_node if run and run.status == FlowRunStatus.ACTIVE else "",
        "variaveis": (run.vars or {}) if run else {},
        "espera": espera,
        "fim": fim,
        "linhas": linhas,
    }


def _linhas(conversa, run):
    """
    Mensagens e eventos INTERCALADOS por ordem de criação.

    Os eventos que viram linha são os que o mockup promete: guardou, marcou,
    repetiu, mexeu em sequência, transferiu, terminou. ENTERED fica de fora
    (move a luz do canvas pelo `no_atual`, não polui o chat) e SENT/REPLIED
    também, porque a própria mensagem já está na lista.
    """
    linhas = []
    for m in Message.objects.filter(conversation=conversa).order_by("pk"):
        linhas.append(
            (
                m.created_at,
                0,
                {
                    "tipo": "mensagem",
                    "quem": "paciente" if m.sender_kind == SenderKind.CONTACT else "robo",
                    "kind": m.kind,
                    "texto": m.body or "",
                    "template": m.template_name or "",
                    "opcoes": _opcoes_da_mensagem(m),
                },
            )
        )

    if run is not None:
        visiveis = {
            FlowRunEventType.VAR_SAVED,
            FlowRunEventType.LABEL_APPLIED,
            FlowRunEventType.SEQUENCE_APPLIED,
            FlowRunEventType.REPROMPT,
            FlowRunEventType.HANDOFF,
            FlowRunEventType.ENDED,
        }
        for e in FlowRunEvent.objects.filter(run=run, event_type__in=visiveis).order_by("pk"):
            linhas.append(
                (
                    e.created_at,
                    1,
                    {"tipo": "evento", "evento": e.event_type, "dados": e.data or {}},
                )
            )

    linhas.sort(key=lambda item: (item[0], item[1]))
    return [conteudo for _, _, conteudo in linhas]


def _opcoes_da_mensagem(m):
    """Os botões/itens que a mensagem interativa carrega, para o painel clicar."""
    dados = m.content_data or {}
    brutos = dados.get("buttons") or []
    if not brutos and dados.get("list"):
        # A lista guarda as opções dentro de sections; para o painel tanto
        # faz, os dois viram a mesma coisa clicável.
        for secao in dados["list"].get("sections") or []:
            brutos.extend(secao.get("rows") or [])
    return [
        {"id": str(o.get("id") or ""), "titulo": str(o.get("title") or "")}
        for o in brutos
        if isinstance(o, dict)
    ]
