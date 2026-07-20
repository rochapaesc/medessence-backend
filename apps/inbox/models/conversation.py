from datetime import timedelta

from django.db.models import (
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    PositiveIntegerField,
    Q,
    UniqueConstraint,
)
from django.utils import timezone

from apps.core.models import TenantScopedModel

# Janela de atendimento livre da Meta (§7): fora dela, só template aprovado.
WINDOW_HOURS = 24


class Conversation(TenantScopedModel):
    """
    Fio de conversa de um contato num canal (§9.5). Campos denormalizados
    (`last_message_*`, `unread_count`) são mantidos pelo signal de Message
    para a listagem por recência (RF-INB-1) não varrer a thread.

    `patient` é opcional: o número pode não estar vinculado, ou atender N
    pacientes (desambiguação manual via PatientContact - RF-INB-7).
    """

    channel = ForeignKey(
        "inbox.Channel",
        verbose_name="Canal",
        on_delete=RESTRICT,
        related_name="conversations",
    )
    contact = ForeignKey(
        "patients.Contact",
        verbose_name="Contato",
        on_delete=RESTRICT,
        related_name="conversations",
    )
    patient = ForeignKey(
        "patients.Patient",
        verbose_name="Paciente",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="conversations",
    )
    last_message_preview = CharField(verbose_name="Prévia", max_length=200, blank=True)
    last_message_at = DateTimeField(verbose_name="Última mensagem em", null=True, db_index=True)
    last_inbound_at = DateTimeField(verbose_name="Último inbound em", null=True)
    unread_count = PositiveIntegerField(verbose_name="Não lidas", default=0)
    needs_agent = BooleanField(verbose_name="Aguardando atendente", default=False)
    assigned_to = ForeignKey(
        "accounts.User",
        verbose_name="Atribuída a",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="assigned_conversations",
    )

    class Meta:
        verbose_name = "Conversa"
        verbose_name_plural = "Conversas"
        constraints = [
            UniqueConstraint(
                fields=["channel", "contact"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_conversation_channel_contact",
            ),
        ]

    def __str__(self):
        return f"{self.contact} @ {self.channel}"

    @property
    def window_open(self) -> bool:
        """Janela de 24h aberta (RF-INB-3): texto livre só com inbound recente."""
        if self.last_inbound_at is None:
            return False
        return timezone.now() - self.last_inbound_at < timedelta(hours=WINDOW_HOURS)
