"""
Provider FAKE do WhatsApp - dev sem número real (mesmo papel do FakeAdapter
do EHR). Envios devolvem um wamid sintético; `parse_webhook` usa o mesmo
parser Meta, então payloads simulados exercitam o pipeline completo.
"""

import uuid

from django.utils import timezone

from apps.integrations.whatsapp.base import (
    DownloadedMedia,
    SendResult,
    Template,
    WhatsAppEvent,
)
from apps.integrations.whatsapp.events import parse_meta_webhook


def fake_wamid() -> str:
    return f"wamid.FAKE-{uuid.uuid4().hex[:20]}"


def build_inbound_payload(*, wa_id: str, body: str, name: str = "Contato Fake") -> dict:
    """Monta um payload Meta de mensagem recebida - usado pelo `wa_simulate`.

    Timestamp = agora, para o inbound abrir a janela de 24h (dev pode responder
    com texto livre)."""
    now_ts = str(int(timezone.now().timestamp()))
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": fake_wamid(),
                                    "timestamp": now_ts,
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


class FakeWhatsAppAdapter:
    def __init__(self, channel):
        self.channel = channel

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "to": to})

    def send_template(
        self, to: str, name: str, language: str, components: list | None = None
    ) -> SendResult:
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "template": name})

    def send_media(
        self, to: str, kind: str, url_or_id: str, caption: str | None = None
    ) -> SendResult:
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "kind": kind})

    def mark_read(self, provider_message_id: str) -> None:
        return None

    def download_media(self, media_id: str) -> DownloadedMedia:
        # FAKE não tem bytes reais - content vazio faz o fetch pular.
        return DownloadedMedia(content=b"", mime_type="image/jpeg")

    def list_templates(self) -> list[Template]:
        return [
            Template(name="confirmacao_consulta", category="UTILITY", status="APPROVED"),
            Template(name="lembrete_retorno", category="MARKETING", status="APPROVED"),
        ]

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        return parse_meta_webhook(payload)
