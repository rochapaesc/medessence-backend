import secrets
import uuid

from django.db.models import (
    CharField,
    DateTimeField,
    PositiveIntegerField,
    Q,
    UniqueConstraint,
    UUIDField,
)
from django.utils import timezone

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

    # ------------------- saúde do canal (item 2 do fechamento) -------------
    # Desenho do `Reauthorizable` do Chatwoot: erro de credencial CONTA, e só
    # ao bater o limiar o canal é dado como morto. Um 401 isolado é blip da
    # Meta — gritar lobo na primeira falha treina a equipe a ignorar o aviso.
    auth_error_count = PositiveIntegerField(verbose_name="Falhas de credencial", default=0)
    disconnected_at = DateTimeField(
        verbose_name="Desconectado desde",
        null=True,
        blank=True,
        help_text="Null = canal vivo. Preenchido, a clínica inteira parou de responder.",
    )
    disconnect_reason = CharField(verbose_name="Motivo", max_length=200, blank=True)

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

    # Uma falha pode ser blip; duas seguidas é credencial morta. É o
    # `AUTHORIZATION_ERROR_THRESHOLD` do Chatwoot (padrão deles: 2).
    LIMIAR_DE_FALHAS = 2

    @property
    def disconnected(self) -> bool:
        return self.disconnected_at is not None

    def registrar_falha_de_auth(self, motivo: str) -> bool:
        """
        Conta uma falha de credencial. Devolve True quando ESTA falha derrubou
        o canal (para o chamador avisar a equipe uma vez só, e não a cada
        mensagem que tentar sair depois).
        """
        self.auth_error_count += 1
        campos = ["auth_error_count", "updated_at"]
        caiu_agora = False
        if self.auth_error_count >= self.LIMIAR_DE_FALHAS and not self.disconnected:
            self.disconnected_at = timezone.now()
            self.disconnect_reason = motivo[:200]
            campos += ["disconnected_at", "disconnect_reason"]
            caiu_agora = True
        self.save(update_fields=campos)
        return caiu_agora

    def reconectado(self) -> bool:
        """
        Qualquer chamada bem-sucedida cura o canal (o `reauthorized!` do
        Chatwoot). Devolve True quando ele ESTAVA morto — ninguém precisa
        clicar em "já arrumei": se voltou a funcionar, a tela para de reclamar.
        """
        if not self.auth_error_count and not self.disconnected:
            return False
        estava_morto = self.disconnected
        self.auth_error_count = 0
        self.disconnected_at = None
        self.disconnect_reason = ""
        self.save(
            update_fields=[
                "auth_error_count",
                "disconnected_at",
                "disconnect_reason",
                "updated_at",
            ]
        )
        return estava_morto

    def __str__(self):
        return self.display_number or f"Canal {self.clinic_id}"
