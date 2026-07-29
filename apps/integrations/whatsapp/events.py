"""
Parser do formato Meta Cloud API (§7) → `WhatsAppEvent` normalizado.

A Datafy é proxy da Meta, então o payload do webhook segue o formato
`entry[].changes[].value.{messages, message_echoes, statuses}`. Este módulo
é o ÚNICO lugar que conhece esse formato - o inbox recebe só DTOs.
"""

from datetime import UTC, datetime

from apps.inbox.choices import MessageKind, MessageStatus
from apps.integrations.whatsapp.base import WhatsAppEvent, WhatsAppEventKind

# type do Meta → MessageKind do inbox.
KIND_MAP = {
    "text": MessageKind.TEXT,
    "image": MessageKind.IMAGE,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
    "location": MessageKind.LOCATION,
    "interactive": MessageKind.INTERACTIVE,
    "template": MessageKind.TEMPLATE,
}
MEDIA_KINDS = {"image", "audio", "video", "document", "sticker"}

STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


def _ts(value) -> datetime | None:
    """Timestamp Meta (unix segundos, string) → datetime aware (UTC)."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, TypeError, OSError):
        return None


def _names_by_wa_id(value: dict) -> dict:
    names = {}
    for contact in value.get("contacts", []) or []:
        wa_id = contact.get("wa_id", "")
        name = (contact.get("profile") or {}).get("name", "")
        if wa_id:
            names[wa_id] = name
    return names


def _parse_message(message: dict, *, kind: str, wa_id: str, names: dict) -> WhatsAppEvent:
    meta_type = message.get("type", "")
    message_kind = KIND_MAP.get(meta_type, MessageKind.UNSUPPORTED)

    body = caption = media_id = mime_type = filename = ""
    if meta_type == "text":
        body = (message.get("text") or {}).get("body", "")
    elif meta_type in MEDIA_KINDS:
        payload = message.get(meta_type) or {}
        media_id = payload.get("id", "")
        mime_type = payload.get("mime_type", "")
        caption = payload.get("caption", "")
        # Só documento traz nome, e é o nome que o paciente vê no celular
        # dele. Jogá-lo fora fazia o exame chegar na recepção como
        # "1037387288883307.pdf".
        filename = payload.get("filename", "")

    return WhatsAppEvent(
        kind=kind,
        provider_message_id=message.get("id", ""),
        wa_id=wa_id,
        message_kind=message_kind,
        body=body,
        caption=caption,
        media_id=media_id,
        mime_type=mime_type,
        filename=filename,
        reply_to_provider_id=(message.get("context") or {}).get("id", ""),
        wa_timestamp=_ts(message.get("timestamp")),
        contact_name=names.get(wa_id, ""),
        raw=message,
    )


def parse_meta_webhook(payload: dict) -> list[WhatsAppEvent]:
    events: list[WhatsAppEvent] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = _names_by_wa_id(value)

            for message in value.get("messages", []) or []:
                events.append(
                    _parse_message(
                        message,
                        kind=WhatsAppEventKind.INBOUND,
                        wa_id=message.get("from", ""),
                        names=names,
                    )
                )

            for echo in value.get("message_echoes", []) or []:
                events.append(
                    _parse_message(
                        echo,
                        kind=WhatsAppEventKind.ECHO,
                        wa_id=echo.get("to", ""),
                        names=names,
                    )
                )

            for status in value.get("statuses", []) or []:
                events.append(
                    WhatsAppEvent(
                        kind=WhatsAppEventKind.STATUS,
                        provider_message_id=status.get("id", ""),
                        wa_id=status.get("recipient_id", ""),
                        status=STATUS_MAP.get(status.get("status", ""), ""),
                        status_error=_status_error(status),
                        wa_timestamp=_ts(status.get("timestamp")),
                        raw=status,
                    )
                )
    return events


def _status_error(status: dict) -> str:
    """
    `errors[]` do status FAILED em uma linha legível. O código importa (é o
    que se busca na documentação da Meta) e `error_data.details` costuma ser
    a única parte que explica de verdade.
    """
    parts = []
    for error in status.get("errors", []) or []:
        code = error.get("code", "")
        title = error.get("title", "")
        details = (error.get("error_data") or {}).get("details", "")
        text = " ".join(str(p) for p in (code, title) if p)
        if details and details != title:
            text = f"{text}: {details}" if text else details
        if text:
            parts.append(text)
    return "; ".join(parts)
