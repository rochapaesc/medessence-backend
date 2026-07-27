import secrets
import uuid

from django.db.models import CharField, Q, UniqueConstraint, UUIDField

from apps.core.fields import EncryptedJSONField
from apps.core.models import TenantScopedModel
from apps.inbox.choices import WhatsAppProviderKind


def default_webhook_secret() -> str:  # pragma: no cover
    """Viva SÓ porque a migration 0001 a referencia por caminho. O campo
    morreu com o webhook por URL (§7, Meta usa HMAC) - nenhum código chama."""
    return secrets.token_urlsafe(32)


class Channel(TenantScopedModel):
    """
    Número WhatsApp de UMA clínica (§9.5). Credenciais DO CANAL cifradas em
    `credentials` (`access_token`; `phone_number_id`/`waba_id` têm coluna
    própria); as do APP da plataforma (`app_secret`, `verify_token`) moram
    nos settings — dois níveis, §7.
    """

    uuid = UUIDField(
        verbose_name="UUID público",
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text="Identificador público estável do canal.",
    )
    provider = CharField(
        verbose_name="Provedor",
        max_length=20,
        choices=WhatsAppProviderKind.choices,
        default=WhatsAppProviderKind.META,
    )
    phone_number_id = CharField(verbose_name="Phone Number ID", max_length=32, blank=True)
    waba_id = CharField(verbose_name="WABA ID", max_length=32, blank=True)
    display_number = CharField(verbose_name="Número exibido", max_length=20, blank=True)
    credentials = EncryptedJSONField(verbose_name="Credenciais", default=dict)

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
