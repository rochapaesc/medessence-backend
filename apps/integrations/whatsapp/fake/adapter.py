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


def build_inbound_payload(
    *,
    wa_id: str,
    body: str,
    name: str = "Contato Fake",
    phone_number_id: str = "",
    reply_id: str = "",
) -> dict:
    """Monta um payload Meta de mensagem recebida - usado pelo `wa_simulate`.

    Timestamp = agora, para o inbound abrir a janela de 24h (dev pode responder
    com texto livre). `phone_number_id` preenche o metadata - é por ele que o
    webhook único roteia o canal (§7); quem injeta direto na ingestão não
    precisa dele.

    `reply_id` monta o payload de TOQUE EM BOTÃO (F2.6): é o formato
    `interactive.button_reply` que a Meta manda, e é o id dele que o motor de
    fluxos usa para saber qual caminho o paciente escolheu. Sem isso não dá
    para exercitar um fluxo de ponta a ponta sem número real."""
    now_ts = str(int(timezone.now().timestamp()))
    if reply_id:
        message = {
            "from": wa_id,
            "id": fake_wamid(),
            "timestamp": now_ts,
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": reply_id, "title": body},
            },
        }
    else:
        message = {
            "from": wa_id,
            "id": fake_wamid(),
            "timestamp": now_ts,
            "type": "text",
            "text": {"body": body},
        }
    value = {
        "messaging_product": "whatsapp",
        "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
        "messages": [message],
    }
    if phone_number_id:
        value["metadata"] = {
            "phone_number_id": phone_number_id,
            "display_phone_number": phone_number_id,
        }
    return {"entry": [{"changes": [{"value": value}]}]}


class FakeWhatsAppAdapter:
    def __init__(self, channel):
        self.channel = channel

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "to": to})

    def send_template(
        self, to: str, name: str, language: str, components: list | None = None
    ) -> SendResult:
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "template": name})

    def send_buttons(self, to: str, body: str, buttons: list[dict]) -> SendResult:
        # Guarda o que recebeu para o teste conferir os ids: é por eles que o
        # motor de fluxos resolve a aresta quando o paciente toca o botão.
        return SendResult(
            provider_message_id=fake_wamid(),
            raw={"fake": True, "to": to, "body": body, "buttons": buttons},
        )

    def send_list(self, to: str, body: str, button_label: str, sections: list[dict]) -> SendResult:
        return SendResult(
            provider_message_id=fake_wamid(),
            raw={
                "fake": True,
                "to": to,
                "body": body,
                "button_label": button_label,
                "sections": sections,
            },
        )

    def send_media(
        self,
        to: str,
        kind: str,
        url_or_id: str,
        caption: str | None = None,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        reply_to: str | None = None,
        is_voice: bool = False,
    ) -> SendResult:
        # O dublê aceita a MESMA assinatura do adapter real de propósito: um
        # fake mais permissivo esconderia justamente o erro de parâmetro que
        # só a Meta reprova (foi assim que o idioma do template passou).
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "kind": kind})

    def send_reaction(self, to: str, provider_message_id: str, emoji: str) -> SendResult:
        return SendResult(provider_message_id=fake_wamid(), raw={"fake": True, "emoji": emoji})

    def mark_read(self, provider_message_id: str) -> None:
        return None

    def download_media(self, media_id: str) -> DownloadedMedia:
        # FAKE não tem bytes reais - content vazio faz o fetch pular.
        return DownloadedMedia(content=b"", mime_type="image/jpeg")

    def verify_credentials(self) -> dict:
        # O dublê sempre aprova. Quem testa a RECUSA injeta um provedor que
        # levanta — dublê que decide sozinho quando falhar esconde o caminho.
        return {
            "display_phone_number": "+55 85 99999-0000",
            "verified_name": "Clínica de teste",
            "quality_rating": "GREEN",
        }

    def list_templates(self) -> list[Template]:
        return [
            Template(name="confirmacao_consulta", category="UTILITY", status="APPROVED"),
            Template(name="lembrete_retorno", category="MARKETING", status="APPROVED"),
        ]

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        return parse_meta_webhook(payload)
