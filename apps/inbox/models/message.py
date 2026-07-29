from django.db.models import (
    CASCADE,
    SET_NULL,
    BooleanField,
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
    ActivityType,
    MessageDirection,
    MessageKind,
    MessageStatus,
    SenderKind,
)


class Message(TenantScopedModel):
    """
    Uma mensagem da thread (§9.5). Idempotência por `provider_message_id`
    (wamid) com unique por clínica - o mesmo evento reentregue pelo webhook
    não duplica (RNF-2).

    `direction` é SEMPRE derivado de `sender_kind` no `save()` (M8): a direção
    não pode divergir do autor.

    NOTA: `journey_stage` (FK para `automation.JourneyStage`) fica de fora na
    F2 e entra na F3 - mesmo adiamento de `Membership.practitioner` (M3).
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

    # Conteúdo ESTRUTURADO que não cabe em texto: cartão de contato
    # (`contacts`), coordenadas da localização (`location`) e o id da resposta
    # de botão (`interactive_id`, que o motor de jornadas da F3 usa para saber
    # QUAL caminho o paciente escolheu — o texto do botão muda a cada
    # template, o id não). Espelha o `content_attributes` do Chatwoot.
    content_data = JSONField(verbose_name="Dados do conteúdo", default=dict, blank=True)

    # Reação NÃO é campo daqui: virou tabela (`MessageReaction`), uma linha
    # por ator. Como campo único, a clínica reagindo pelo celular apagava a
    # reação do paciente e a tela não sabia de quem era o emoji.
    status = CharField(
        verbose_name="Status",
        max_length=10,
        choices=MessageStatus.choices,
        blank=True,
    )
    status_error = TextField(
        verbose_name="Motivo da falha",
        blank=True,
        help_text="Preenchido quando FAILED: errors[] do webhook ou erro de envio. "
        '"Falhou" sem motivo não ajuda ninguém a agir.',
    )

    # Nota interna reescrita. Marcado porque a equipe LÊ a nota como registro
    # do que se sabia: uma que mudou de texto sem avisar faria alguém confiar
    # numa versão que já não é a original. O antes/depois fica na auditoria.
    edited_at = DateTimeField(verbose_name="Editada em", null=True, blank=True)

    # ---------------- nota interna e atividade (F2.5, §4.3.1) --------------
    is_internal = BooleanField(
        verbose_name="Nota interna",
        default=False,
        help_text="Anotação da equipe: NUNCA é enviada ao paciente (RF-ATD-3).",
    )
    activity_type = CharField(
        verbose_name="Tipo de evento",
        max_length=16,
        choices=ActivityType.choices,
        blank=True,
        help_text="Preenchido só em evento de atividade (RF-ATD-4).",
    )
    activity_data = JSONField(
        verbose_name="Dados do evento",
        default=dict,
        blank=True,
        help_text="Quem/para quem/resumo. O front monta a frase - backend que "
        "concatena texto engessa tradução e formatação.",
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
        # A direção NUNCA vem do cliente - deriva do autor (M8).
        self.direction = SENDER_TO_DIRECTION.get(self.sender_kind, MessageDirection.OUT)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"[{self.get_direction_display()}] {self.body[:40] or self.get_kind_display()}"
