"""
Ciclo de vida e posse do atendimento (§4.3.1, RF-ATD-1..17).

Vive fora do viewset de propósito: a ingestão do webhook (reabertura) e, na
frente, a IA (tomada, entrega) precisam das MESMAS regras. Regra que mora só
na API vira regra que o resto do sistema não obedece.
"""

from django.db import transaction
from django.utils import timezone

from apps.inbox.choices import (
    DORMANT_STATUSES,
    ActivityType,
    AttendedBy,
    ConversationStatus,
    MessageKind,
    SenderKind,
)


class ConversationBusy(Exception):
    """
    Alguém (ou a IA) tem a caneta e não é quem está pedindo (RF-ATD-14/15).

    Carrega quem é o responsável para a tela dizer "Ana assumiu esta conversa"
    em vez de um vermelho genérico.
    """

    def __init__(self, conversation):
        self.conversation = conversation
        self.attended_by = conversation.attended_by
        self.holder = (
            conversation.assigned_to.get_full_name() or conversation.assigned_to.email
            if conversation.assigned_to_id
            else ""
        )
        super().__init__("Conversa em atendimento por outra pessoa.")


# --------------------------------------------------------------------- #
# Eventos na linha do tempo (RF-ATD-4)
# --------------------------------------------------------------------- #


def log_activity(conversation, activity_type: str, *, user=None, data: dict | None = None):
    """
    Evento entra na MESMA tabela de mensagens (padrão Chatwoot) para a thread
    contar a história inteira numa consulta só. Sem corpo de texto: o tipo e
    os dados vão estruturados e o front monta a frase.
    """
    from apps.inbox.models import Message
    from apps.inbox.realtime import notify_message_new_on_commit

    evento = Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=MessageKind.ACTIVITY,
        sender_kind=SenderKind.SYSTEM,
        sent_by=user,
        activity_type=activity_type,
        activity_data=data or {},
        wa_timestamp=timezone.now(),
    )
    notify_message_new_on_commit(evento)
    return evento


# --------------------------------------------------------------------- #
# Posse (RF-ATD-12..15)
# --------------------------------------------------------------------- #


def can_write(conversation, user) -> bool:
    """
    Conversa SEM responsável não trava: escrever nela é o ato que a assume -
    senão a recepção daria dois cliques para responder a primeira mensagem do
    dia (RF-ATD-14).
    """
    if conversation.attended_by == AttendedBy.NONE:
        return True
    if conversation.attended_by == AttendedBy.BOT:
        return False
    return conversation.assigned_to_id == getattr(user, "pk", None)


def assert_can_write(conversation, user) -> None:
    if not can_write(conversation, user):
        raise ConversationBusy(conversation)


@transaction.atomic
def take_over(conversation, user, *, expected: str | None = None):
    """
    Toma o atendimento (RF-ATD-15). A troca é CONDICIONADA ao responsável
    esperado: dois atendentes clicando juntos, só um vence - o UPDATE do
    segundo não casa nenhuma linha e ele recebe aviso em vez de sobrescrever.

    `expected` vem do cliente (o que ele viu na tela). Sem ele, condiciona ao
    estado lido agora, que ainda protege contra a corrida entre requisições.
    """
    from apps.inbox.models import Conversation

    anterior = expected or conversation.attended_by
    era_bot = anterior == AttendedBy.BOT

    trocou = (
        Conversation.objects.filter(pk=conversation.pk, attended_by=anterior)
        .exclude(attended_by=AttendedBy.AGENT, assigned_to=user)
        .update(
            attended_by=AttendedBy.AGENT,
            assigned_to=user,
            attended_since=timezone.now(),
            status=ConversationStatus.OPEN,
            waiting_since=None,
            updated_at=timezone.now(),
        )
    )
    conversation.refresh_from_db()
    if not trocou:
        # Já era dele: idempotente. De outra pessoa: perdeu a corrida.
        if conversation.assigned_to_id == user.pk:
            return conversation
        raise ConversationBusy(conversation)

    log_activity(
        conversation,
        ActivityType.TAKEN_OVER if era_bot else ActivityType.ASSIGNED,
        user=user,
        data={"from": anterior},
    )
    return conversation


# --------------------------------------------------------------------- #
# Ciclo de vida (RF-ATD-1/2)
# --------------------------------------------------------------------- #


@transaction.atomic
def resolve(conversation, user, *, note: str = ""):
    """Encerra. Nada é obrigatório (RF-ATD-1.3) - encerrar é o ato mais
    repetido do dia, e campo obrigatório aí trava a recepção ou gera lixo."""
    from apps.inbox.services import create_internal_note

    conversation.status = ConversationStatus.RESOLVED
    conversation.resolved_at = timezone.now()
    conversation.snoozed_until = None
    conversation.waiting_since = None
    conversation.save(
        update_fields=["status", "resolved_at", "snoozed_until", "waiting_since", "updated_at"]
    )
    if note.strip():
        create_internal_note(conversation, user, note.strip())
    log_activity(conversation, ActivityType.RESOLVED, user=user)
    return conversation


@transaction.atomic
def snooze(conversation, user, *, until, note: str = ""):
    """Adia até data e hora escolhidas (RF-ATD-1.2)."""
    from apps.inbox.services import create_internal_note

    conversation.status = ConversationStatus.SNOOZED
    conversation.snoozed_until = until
    conversation.waiting_since = None
    conversation.save(
        update_fields=["status", "snoozed_until", "waiting_since", "updated_at"]
    )
    if note.strip():
        create_internal_note(conversation, user, note.strip())
    log_activity(conversation, ActivityType.SNOOZED, user=user, data={"until": until.isoformat()})
    return conversation


@transaction.atomic
def reopen(conversation, *, user=None, by_contact: bool = False):
    """
    Reabre (RF-ATD-2). Chamado pela ingestão quando chega inbound em conversa
    dormente, e pela API quando alguém reabre à mão.

    Volta para ABERTA se ainda houver responsável - a conversa não perde o
    dono só porque ficou parada; senão volta para a fila.
    """
    if conversation.status not in DORMANT_STATUSES:
        return conversation

    tem_dono = conversation.attended_by == AttendedBy.AGENT and conversation.assigned_to_id
    conversation.status = ConversationStatus.OPEN if tem_dono else ConversationStatus.WAITING
    conversation.snoozed_until = None
    conversation.resolved_at = None
    if conversation.status == ConversationStatus.WAITING:
        conversation.waiting_since = timezone.now()
    conversation.save(
        update_fields=[
            "status",
            "snoozed_until",
            "resolved_at",
            "waiting_since",
            "updated_at",
        ]
    )
    log_activity(
        conversation,
        ActivityType.REOPENED,
        user=user,
        data={"by": "contact" if by_contact else "agent"},
    )
    return conversation


def wake_snoozed(conversation) -> bool:
    """
    Cumpre a promessa do adiamento (RF-ATD-1.2): "volta sozinha para
    Aguardando na hora marcada". Chamada pela varredura por minuto
    (`wake_snoozed_conversations`).

    Acorda para AGUARDANDO **sem responsável** — o invariante do status. A
    alternativa (voltar Aberta para quem adiou) esconderia o lembrete no meio
    das abertas da pessoa: o valor do adiamento é ser VISTO quando vence.

    O UPDATE é condicionado ao status: se o paciente reabriu antes da hora
    (RF-ATD-2) ou duas varreduras se sobrepõem, só uma transição acontece -
    acordar duas vezes duplicaria o evento na thread.
    """
    from apps.inbox.models import Conversation
    from apps.inbox.realtime import notify_conversation_updated

    agora = timezone.now()
    dono_anterior = _nome(conversation.assigned_to)
    trocou = Conversation.objects.filter(
        pk=conversation.pk, status=ConversationStatus.SNOOZED
    ).update(
        status=ConversationStatus.WAITING,
        attended_by=AttendedBy.NONE,
        assigned_to=None,
        attended_since=None,
        snoozed_until=None,
        waiting_since=agora,
        updated_at=agora,
    )
    if not trocou:
        return False

    conversation.refresh_from_db()
    log_activity(
        conversation,
        ActivityType.REOPENED,
        # `was_with` preserva na thread de quem a conversa era antes de voltar
        # para a fila - o dono saiu do registro vivo, não da história.
        data={"by": "snooze", "was_with": dono_anterior},
    )
    # A fila muda para todo mundo: sem isto, a conversa "volta" só para quem
    # apertar F5.
    notify_conversation_updated(conversation)
    return True


# --------------------------------------------------------------------- #
# Classificação e passagem adiante (Bloco B1, RF-ATD-6/8/9)
# --------------------------------------------------------------------- #


@transaction.atomic
def transfer(conversation, user, *, to_user, note: str = ""):
    """
    Passa o atendimento para outra pessoa (RF-ATD-6).

    Quem transfere precisa TER a posse: passar adiante conversa que não é sua é
    a disputa do RF-ATD-15 por outro caminho. Quem recebe vira responsável na
    hora - sem devolução por timeout (B2), transferir É atribuir, e deixar a
    conversa "oferecida" e sem dono seria inventar um estado que ninguém
    resolve.
    """
    from apps.inbox.services import create_internal_note

    assert_can_write(conversation, user)
    if to_user.pk == conversation.assigned_to_id:
        return conversation

    de = conversation.assigned_to
    conversation.assigned_to = to_user
    conversation.attended_by = AttendedBy.AGENT
    conversation.attended_since = timezone.now()
    conversation.status = ConversationStatus.OPEN
    conversation.waiting_since = None
    conversation.save(
        update_fields=[
            "assigned_to",
            "attended_by",
            "attended_since",
            "status",
            "waiting_since",
            "updated_at",
        ]
    )
    # A nota de passagem entra ANTES do evento para a thread ler na ordem em
    # que a coisa aconteceu: o porquê, e então o registro de quem passou.
    if note.strip():
        create_internal_note(conversation, user, note.strip())
    log_activity(
        conversation,
        ActivityType.TRANSFERRED,
        user=user,
        data={
            "from": _nome(de),
            "to": _nome(to_user),
            # O texto vai junto porque quem recebe abre a conversa pelo evento,
            # e ter de procurar a nota logo acima é atrito no pior momento.
            "note": note.strip(),
        },
    )
    return conversation


def set_priority(conversation, user, *, priority: str):
    """Urgência da fila (RF-ATD-8). Sem evento quando nada mudou - repetir a
    mesma prioridade não é um fato da conversa."""
    if conversation.priority == priority:
        return conversation

    anterior = conversation.priority
    conversation.priority = priority
    conversation.save(update_fields=["priority", "updated_at"])
    log_activity(
        conversation,
        ActivityType.PRIORITY_CHANGED,
        user=user,
        data={"from": anterior, "to": priority},
    )
    return conversation


def add_label(conversation, user, *, label):
    """Marca assunto (RF-ATD-9). Idempotente: marcar duas vezes não gera dois
    eventos."""
    if conversation.labels.filter(pk=label.pk).exists():
        return conversation
    conversation.labels.add(label)
    log_activity(
        conversation,
        ActivityType.LABEL_ADDED,
        user=user,
        data={"label": label.name, "label_id": label.pk},
    )
    return conversation


def remove_label(conversation, user, *, label):
    if not conversation.labels.filter(pk=label.pk).exists():
        return conversation
    conversation.labels.remove(label)
    log_activity(
        conversation,
        ActivityType.LABEL_REMOVED,
        user=user,
        data={"label": label.name, "label_id": label.pk},
    )
    return conversation


def _nome(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.email


def mark_waiting(conversation, user=None):
    """Devolve para a fila: perde o responsável (RF-ATD-1)."""
    conversation.status = ConversationStatus.WAITING
    conversation.attended_by = AttendedBy.NONE
    conversation.assigned_to = None
    conversation.attended_since = None
    conversation.waiting_since = timezone.now()
    conversation.save(
        update_fields=[
            "status",
            "attended_by",
            "assigned_to",
            "attended_since",
            "waiting_since",
            "updated_at",
        ]
    )
    return conversation
