"""
Port WhatsApp (§5) - a fronteira entre o MedEssence e o transporte de
mensageria. É o "gateway" do §7: campanha, segmentação e follow-up ficam no
domínio e só falam com o WhatsApp por esta interface.

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
    filename: str = ""  # só documento traz: é o nome que o paciente vê
    # Conteúdo estruturado que não cabe em texto: cartão de contato,
    # coordenadas da localização, id da resposta de botão.
    content_data: dict = field(default_factory=dict)
    # Reação (👍 numa mensagem): NÃO é mensagem nova — é um selo colado numa
    # mensagem que já existe. `reaction_to` é o wamid do alvo; emoji vazio
    # significa que a pessoa REMOVEU a reação.
    reaction_emoji: str = ""
    reaction_to: str = ""
    reply_to_provider_id: str = ""
    status: str = ""  # para kind=STATUS: sent/delivered/read/failed
    status_error: str = ""  # para status=failed: motivo legível (errors[] da Meta)
    wa_timestamp: datetime | None = None  # aware
    contact_name: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str
    status: str = "sent"  # mapeado para MessageStatus
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadedMedia:
    """Bytes do ativo, já baixados pelo adapter.

    O port entrega CONTEÚDO, não URL: na Cloud API a URL de mídia expira em
    ~5 minutos e o download exige o token — devolver a URL crua convidaria o
    caller a baixar sem auth (e foi assim com o Datafy, que dava 30 dias
    públicos). Content vazio = provedor sem bytes (FAKE) → caller pula.
    """

    content: bytes = b""
    mime_type: str = ""


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

    def download_media(self, media_id: str) -> DownloadedMedia:
        """Baixa o ativo no provedor (autenticado) - para re-hospedar (RF-INB-6)."""
        ...

    def list_templates(self) -> list[Template]:
        """Cache de templates aprovados (beat 6h)."""
        ...

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        """Formato Meta (`entry[].changes[].value.*`) → eventos normalizados."""
        ...
