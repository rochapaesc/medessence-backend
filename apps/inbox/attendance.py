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

    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=MessageKind.ACTIVITY,
        sender_kind=SenderKind.SYSTEM,
        sent_by=user,
        activity_type=activity_type,
        activity_data=data or {},
        wa_timestamp=timezone.now(),
    )


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
