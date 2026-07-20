"""
Port WhatsApp (§5) - a fronteira entre o MedEssence e qualquer provedor de
mensageria (Datafy é proxy da Meta Cloud API).

Adapters normalizam o formato de terceiro na ENTRADA (`parse_webhook` →
`WhatsAppEvent`) e recebem valores já limpos na SAÍDA. O inbox NUNCA vê o
formato Meta cru fora do `raw`/`WebhookEvent` (preservado para replay).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


class WhatsAppEventKind:
    """Tipos de evento normalizados vindos do webhook (formato Meta, §7)."""

    INBOUND = "inbound"  # mensagem recebida do contato
    STATUS = "status"  # atualização de entrega de uma mensagem OUT
    ECHO = "echo"  # mensagem OUT enviada pelo app do celular (coexistência)


@dataclass(frozen=True)
class WhatsAppEvent:
    """Evento único e normalizado (uma mensagem, um status ou um echo)."""

    kind: str  # WhatsAppEventKind
    provider_message_id: str = ""  # wamid
    wa_id: str = ""  # E.164 sem "+" (contato do outro lado)
    message_kind: str = "text"  # já mapeado para MessageKind (choices do inbox)
    body: str = ""
    caption: str = ""
    media_id: str = ""
    mime_type: str = ""
    reply_to_provider_id: str = ""
    status: str = ""  # para kind=STATUS: sent/delivered/read/failed
    wa_timestamp: datetime | None = None  # aware
    contact_name: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    status: str = "sent"  # mapeado para MessageStatus
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MediaURL:
    url: str
    mime_type: str = ""
    size_bytes: int | None = None


@dataclass(frozen=True)
class Template:
    name: str
    language: str = "pt_BR"
    category: str = ""
    status: str = ""
    components: list = field(default_factory=list)


@runtime_checkable
class WhatsAppProvider(Protocol):
    """Interface da F2 - um adapter por provedor (resolvido por canal)."""

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        """Texto livre - só válido com a janela de 24h aberta (RF-INB-3)."""
        ...

    def send_template(
        self, to: str, name: str, language: str, components: list | None = None
    ) -> SendResult:
        """Template aprovado - único envio permitido fora da janela de 24h."""
        ...

    def send_media(
        self, to: str, kind: str, url_or_id: str, caption: str | None = None
    ) -> SendResult: ...

    def mark_read(self, provider_message_id: str) -> None:
        """`messages/read` no provedor (RF-INB-4)."""
        ...

    def resolve_media(self, media_id: str) -> MediaURL:
        """URL temporária (~30 dias) do ativo - para re-hospedar (RF-INB-6)."""
        ...

    def list_templates(self) -> list[Template]:
        """Cache de templates aprovados (beat 6h)."""
        ...

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        """Formato Meta (`entry[].changes[].value.*`) → eventos normalizados."""
        ...
