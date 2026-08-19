import secrets
import uuid

from django.db.models import (
    BooleanField,
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
from apps.inbox.choices import ChannelSource, WhatsAppProviderKind


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

    # Canal interno do MODO DE TESTE de fluxo (RF-FLW-25.1). Provider FAKE,
    # invisível para o Inbox e nunca escolhido para disparo: existe para a
    # conversa de teste ter onde viver sem alcançar ninguém.
    is_test = BooleanField(verbose_name="Canal de teste", default=False)

    # ------------- como o canal foi ligado (F2.7, §4.3.3) ------------------
    connection_source = CharField(
        verbose_name="Ligado por",
        max_length=20,
        choices=ChannelSource.choices,
        blank=True,
        help_text="Vazio nos canais anteriores ao cadastro incorporado.",
    )
    is_coexistence = BooleanField(
        verbose_name="Também no celular",
        default=False,
        help_text=(
            "RF-CON-3. O número segue no app do WhatsApp Business, então "
            "mensagem enviada de lá chega aqui como eco (RF-CON-5.1)."
        ),
    )
    connected_at = DateTimeField(verbose_name="Ligado em", null=True, blank=True)
    verified_name = CharField(
        verbose_name="Nome verificado",
        max_length=120,
        blank=True,
        help_text="O nome que o paciente vê no WhatsApp, aprovado na Meta.",
    )

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
            # Um canal DE VERDADE por clínica entre registros vivos (soft
            # delete não bloqueia recriação). O canal interno de teste
            # (RF-FLW-25.1) fica fora da conta: ele coexiste com o real.
            UniqueConstraint(
                fields=["clinic"],
                condition=Q(deleted_at__isnull=True, is_test=False),
                name="one_channel_per_clinic",
            ),
            # RF-CON-2.6 — o webhook resolve o tenant pelo `phone_number_id` do
            # payload (§7). Dois canais vivos com o mesmo número entregariam a
            # conversa de uma clínica DENTRO de outra, e em silêncio: o
            # `filter(...).first()` do webhook pegaria qualquer um dos dois.
            # Enquanto o canal era colado por nós isso era improvável; com o
            # gestor conectando sozinho, deixa de ser. É a lição da migration
            # 013 do wacrm, onde a duplicata derrubava todo inbound sem erro.
            # Vazio fica de fora: canal FAKE e canal recém-criado não têm.
            UniqueConstraint(
                fields=["phone_number_id"],
                condition=Q(deleted_at__isnull=True) & ~Q(phone_number_id=""),
                name="uniq_channel_phone_number_id",
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
