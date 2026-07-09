from django.db.models import (
    CASCADE,
    SET_NULL,
    CharField,
    DateTimeField,
    ForeignKey,
    JSONField,
    Q,
    TextField,
    UniqueConstraint,
)

from apps.core.models import TenantScopedModel
from apps.inbox.choices import (
    SENDER_TO_DIRECTION,
    MessageDirection,
    MessageKind,
    MessageStatus,
    SenderKind,
)


class Message(TenantScopedModel):
    """
    Uma mensagem da thread (§9.5). Idempotência por `provider_message_id`
    (wamid) com unique por clínica — o mesmo evento reentregue pelo webhook
    não duplica (RNF-2).

    `direction` é SEMPRE derivado de `sender_kind` no `save()` (M8): a direção
    não pode divergir do autor.

    NOTA: `journey_stage` (FK para `automation.JourneyStage`) fica de fora na
    F2 e entra na F3 — mesmo adiamento de `Membership.practitioner` (M3).
    """

    conversation = ForeignKey(
        "inbox.Conversation",
        verbose_name="Conversa",
        on_delete=CASCADE,
        related_name="messages",
    )
    provider_message_id = CharField(verbose_name="wamid", max_length=128, blank=True)
    direction = CharField(
        verbose_name="Direção",
        max_length=3,
        choices=MessageDirection.choices,
        editable=False,
    )
    kind = CharField(
        verbose_name="Tipo",
        max_length=12,
        choices=MessageKind.choices,
        default=MessageKind.TEXT,
    )
    body = TextField(verbose_name="Texto", blank=True)
    caption = TextField(verbose_name="Legenda", blank=True)
    media = ForeignKey(
        "inbox.MediaAsset",
        verbose_name="Mídia",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="messages",
    )
    reply_to_provider_id = CharField(verbose_name="Responde a (wamid)", max_length=128, blank=True)
    status = CharField(
        verbose_name="Status",
        max_length=10,
        choices=MessageStatus.choices,
        blank=True,
    )
    sender_kind = CharField(
        verbose_name="Autor",
        max_length=8,
        choices=SenderKind.choices,
    )
    sent_by = ForeignKey(
        "accounts.User",
        verbose_name="Enviada por",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="sent_messages",
    )
    template_name = CharField(verbose_name="Template", max_length=120, blank=True)
    wa_timestamp = DateTimeField(verbose_name="Horário WhatsApp", db_index=True)
    raw_payload = JSONField(verbose_name="Payload cru", default=dict, blank=True)

    class Meta:
        verbose_name = "Mensagem"
        verbose_name_plural = "Mensagens"
        ordering = ["wa_timestamp"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "provider_message_id"],
                condition=~Q(provider_message_id=""),
                name="uniq_message_wamid",
            ),
        ]

    def save(self, *args, **kwargs):
        # A direção NUNCA vem do cliente — deriva do autor (M8).
        self.direction = SENDER_TO_DIRECTION.get(self.sender_kind, MessageDirection.OUT)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.get_direction_display()}] {self.body[:40] or self.get_kind_display()}"
