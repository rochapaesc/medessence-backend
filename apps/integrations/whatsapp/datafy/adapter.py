"""
Adapter Datafy → DTOs do port (§5). A saída monta o corpo da Meta Cloud API;
a entrada (`parse_webhook`) delega ao parser do formato Meta.
"""

from apps.integrations.whatsapp.base import (
    MediaURL,
    SendResult,
    Template,
    WhatsAppEvent,
)
from apps.integrations.whatsapp.datafy.client import DatafyClient
from apps.integrations.whatsapp.events import parse_meta_webhook


def _first_message_id(response: dict) -> str:
    messages = response.get("messages") or []
    return messages[0].get("id", "") if messages else ""


class DatafyAdapter:
    def __init__(self, channel):
        self.channel = channel
        self.client = DatafyClient(channel)

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        response = self.client.post_messages(payload)
        return SendResult(provider_message_id=_first_message_id(response), raw=response)

    def send_template(
        self, to: str, name: str, language: str, components: list | None = None
    ) -> SendResult:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": name,
                "language": {"code": language},
                "components": components or [],
            },
        }
        response = self.client.post_messages(payload)
        return SendResult(provider_message_id=_first_message_id(response), raw=response)

    def send_media(
        self, to: str, kind: str, url_or_id: str, caption: str | None = None
    ) -> SendResult:
        media = {"link": url_or_id} if url_or_id.startswith("http") else {"id": url_or_id}
        if caption:
            media["caption"] = caption
        payload = {"messaging_product": "whatsapp", "to": to, "type": kind, kind: media}
        response = self.client.post_messages(payload)
        return SendResult(provider_message_id=_first_message_id(response), raw=response)

    def mark_read(self, provider_message_id: str) -> None:
        self.client.post_messages(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": provider_message_id,
            }
        )

    def resolve_media(self, media_id: str) -> MediaURL:
        # Endpoint simplificado da Datafy: URL da CDN válida por 30 dias e
        # baixável SEM Authorization. (O mirror `/v1/{id}` devolve URL Meta de
        # ~5 min que EXIGE `Bearer` no download — por isso não o usamos.)
        data = self.client.get(f"/media/{media_id}")
        return MediaURL(
            url=data.get("url", ""),
            mime_type=data.get("mime_type", ""),
            size_bytes=data.get("size"),
        )

    def list_templates(self) -> list[Template]:
        # Endpoint simplificado da Datafy (associa à WABA pelo token — dispensa
        # o waba_id na URL).
        data = self.client.get("/templates")
        return [
            Template(
                name=item.get("name", ""),
                language=item.get("language", "pt_BR"),
                category=item.get("category", ""),
                status=item.get("status", ""),
                components=item.get("components", []),
            )
            for item in data.get("data", []) or []
        ]

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        return parse_meta_webhook(payload)
