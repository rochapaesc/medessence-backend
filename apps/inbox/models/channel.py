import secrets
import uuid

from django.db.models import (
    CharField,
    Q,
    UniqueConstraint,
    UUIDField,
)

from apps.core.fields import EncryptedJSONField
from apps.core.models import TenantScopedModel
from apps.inbox.choices import WhatsAppProviderKind


def default_webhook_secret() -> str:
    """Segredo da URL de webhook por canal (§7) — sem verify token da Meta,
    a URL é a credencial: `/webhooks/whatsapp/{uuid}/{secret}/`."""
    return secrets.token_urlsafe(32)


class Channel(TenantScopedModel):
    """
    Número WhatsApp de UMA clínica (§9.5). As credenciais do provedor ficam
    cifradas (`EncryptedJSONField`); `uuid` + `webhook_secret` compõem a URL
    não-adivinhável do webhook, já que a Datafy não usa verify token.
    """

    uuid = UUIDField(
        verbose_name="UUID público",
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text="Identificador do canal na URL do webhook.",
    )
    provider = CharField(
        verbose_name="Provedor",
        max_length=20,
        choices=WhatsAppProviderKind.choices,
        default=WhatsAppProviderKind.DATAFY,
    )
    phone_number_id = CharField(verbose_name="Phone Number ID", max_length=32, blank=True)
    waba_id = CharField(verbose_name="WABA ID", max_length=32, blank=True)
    display_number = CharField(verbose_name="Número exibido", max_length=20, blank=True)
    credentials = EncryptedJSONField(verbose_name="Credenciais", default=dict)
    webhook_secret = CharField(
        verbose_name="Segredo do webhook",
        max_length=64,
        default=default_webhook_secret,
    )

    class Meta:
        verbose_name = "Canal"
        verbose_name_plural = "Canais"
        constraints = [
            # Um canal por clínica entre registros vivos (soft delete não bloqueia recriação).
            UniqueConstraint(
                fields=["clinic"],
                condition=Q(deleted_at__isnull=True),
                name="one_channel_per_clinic",
            ),
        ]

    def __str__(self):
        return self.display_number or f"Canal {self.clinic_id}"
