"""
Emissão de eventos em tempo real (§12). Papel único: empurrar eventos enxutos
para o grupo da clínica. A fonte da verdade continua a API REST.

Contrato de eventos (servidor → cliente):
    message:new          · conversation_id, message{mínimo}
    message:status       · provider_message_id, status
    conversation:updated · conversation_id, unread_count, preview
    handoff:new          · conversation_id, patient_name
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def _broadcast(clinic_id: int, data: dict) -> None:
    layer = get_channel_layer()
    if layer is None:  # ambiente sem channel layer (ex.: shell simples) - no-op
        return
    try:
        async_to_sync(layer.group_send)(
            f"inbox_clinic_{clinic_id}", {"type": "inbox.event", "data": data}
        )
    except Exception:
        logger.exception("Falha ao emitir evento realtime para a clínica %s", clinic_id)


def _message_min(message) -> dict:
    return {
        "id": message.pk,
        # O wamid vai junto porque é a CHAVE dos eventos de status seguintes
        # (sent→delivered→read): sem ele o cliente recebe o tique e não sabe
        # em qual balão aplicá-lo. A resposta do POST nasce com ele vazio -
        # o envio é assíncrono e só termina depois.
        "provider_message_id": message.provider_message_id,
        "direction": message.direction,
        "kind": message.kind,
        "body": (message.body or message.caption)[:200],
        "sender_kind": message.sender_kind,
        "status": message.status,
        "status_error": message.status_error,
        "wa_timestamp": message.wa_timestamp.isoformat() if message.wa_timestamp else None,
    }


def notify_message_new(message) -> None:
    _broadcast(
        message.clinic_id,
        {
            "event": "message:new",
            "conversation_id": message.conversation_id,
            "message": _message_min(message),
        },
    )


def notify_conversation_updated(conversation) -> None:
    _broadcast(
        conversation.clinic_id,
        {
            "event": "conversation:updated",
            "conversation_id": conversation.pk,
            "unread_count": conversation.unread_count,
            "preview": conversation.last_message_preview,
        },
    )


def notify_message_status(
    clinic_id: int, provider_message_id: str, status: str, conversation_id: int | None = None
) -> None:
    """
    Tique de entrega. `conversation_id` vai junto porque o cliente precisa
    saber QUAL thread atualizar - sem ele, a tela teria de procurar o wamid
    em todas as conversas abertas (ou recarregar por um tique).
    """
    _broadcast(
        clinic_id,
        {
            "event": "message:status",
            "conversation_id": conversation_id,
            "provider_message_id": provider_message_id,
            "status": status,
        },
    )


def notify_handoff(conversation) -> None:
    _broadcast(
        conversation.clinic_id,
        {
            "event": "handoff:new",
            "conversation_id": conversation.pk,
            "patient_name": conversation.patient.name if conversation.patient_id else "",
        },
    )
