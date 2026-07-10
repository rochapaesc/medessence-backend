"""
Jobs Celery do inbox (§13).

  process_whatsapp_webhook  (webhooks) — parse + ingestão idempotente
  fetch_media_asset         (media)    — resolve URL temporária, baixa, re-hospeda
  send_whatsapp_message     (outbound) — envio com retry/backoff em 429
  refresh_wa_templates      (sync, beat 6h) — cache de templates aprovados

O provedor entra sempre pelo registry — as tasks não conhecem Datafy.
"""

import logging
import mimetypes
from contextlib import suppress

import requests
from celery import shared_task
from django.core.files.base import ContentFile
from django.utils import timezone

from apps.integrations.whatsapp.exceptions import (
    WhatsAppRateLimitedError,
    WhatsAppUnavailableError,
)

logger = logging.getLogger(__name__)


@shared_task(queue="webhooks")
def process_whatsapp_webhook(webhook_event_id: int, channel_id: int):
    """Processa um WebhookEvent cru: parse via adapter + ingestão idempotente."""
    from apps.inbox.models import Channel, WebhookEvent
    from apps.inbox.services import ingest_events
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    event = WebhookEvent.objects.filter(pk=webhook_event_id).first()
    channel = Channel.objects.filter(pk=channel_id).first()
    if event is None or channel is None:
        return "skipped: evento/canal ausente"

    try:
        provider = get_whatsapp_provider(channel)
        stats = ingest_events(channel, provider.parse_webhook(event.payload))
        event.processed_at = timezone.now()
        event.error = ""
        event.save(update_fields=["processed_at", "error"])
        return stats
    except Exception as exc:
        event.error = str(exc)[:2000]
        event.save(update_fields=["error"])
        logger.exception("Falha ao processar webhook %s", webhook_event_id)
        raise


@shared_task(queue="media")
def fetch_media_asset(media_asset_id: int):
    """Resolve a URL temporária do provedor, baixa e re-hospeda (RF-INB-6)."""
    from apps.inbox.models import Channel, MediaAsset
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    media = MediaAsset.objects.filter(pk=media_asset_id).first()
    if media is None or media.stored_file:
        return "skipped: sem mídia ou já baixada"

    channel = Channel.objects.filter(clinic=media.clinic).first()
    if channel is None:
        return "skipped: clínica sem canal"

    provider = get_whatsapp_provider(channel)
    resolved = provider.resolve_media(media.provider_media_id)
    if not resolved.url:
        return "skipped: sem URL"

    response = requests.get(resolved.url, timeout=60)
    response.raise_for_status()
    content = response.content

    mime = resolved.mime_type or media.mime_type or ""
    extension = mimetypes.guess_extension(mime.split(";")[0]) or ""
    media.mime_type = mime or media.mime_type
    media.size_bytes = len(content)
    media.stored_file.save(f"{media.provider_media_id}{extension}", ContentFile(content), save=True)
    return {"media_id": media.pk, "bytes": len(content)}


@shared_task(
    queue="outbound",
    autoretry_for=(WhatsAppRateLimitedError, WhatsAppUnavailableError),
    retry_backoff=30,
    retry_backoff_max=60 * 10,
    max_retries=5,
)
def send_whatsapp_message(message_id: int):
    """Envia uma mensagem OUT pendente pelo provedor do canal."""
    from apps.inbox.models import Message
    from apps.inbox.services import send_message

    message = Message.objects.filter(pk=message_id).first()
    if message is None:
        return "skipped: mensagem ausente"
    if message.provider_message_id:
        return "skipped: já enviada"

    send_message(message)
    return {"message_id": message.pk, "wamid": message.provider_message_id}


@shared_task(
    queue="outbound",
    autoretry_for=(WhatsAppRateLimitedError, WhatsAppUnavailableError),
    retry_backoff=30,
    max_retries=3,
)
def mark_whatsapp_read(conversation_id: int):
    """Envia `messages/read` do último inbound da conversa ao provedor (RF-INB-4)."""
    from apps.inbox.choices import MessageDirection
    from apps.inbox.models import Conversation
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    conversation = Conversation.objects.filter(pk=conversation_id).select_related("channel").first()
    if conversation is None:
        return "skipped: conversa ausente"

    last_inbound = (
        conversation.messages.filter(direction=MessageDirection.IN)
        .exclude(provider_message_id="")
        .order_by("-wa_timestamp")
        .first()
    )
    if last_inbound is None:
        return "skipped: sem inbound com wamid"

    provider = get_whatsapp_provider(conversation.channel)
    provider.mark_read(last_inbound.provider_message_id)
    return {"conversation_id": conversation_id, "wamid": last_inbound.provider_message_id}


@shared_task(queue="sync")
def refresh_wa_templates():
    """Beat (6h): fan-out do refresh de templates por canal."""
    from apps.inbox.models import Channel

    for channel_id in Channel.objects.values_list("pk", flat=True):
        refresh_channel_templates.delay(channel_id)


@shared_task(queue="sync")
def refresh_channel_templates(channel_id: int):
    """Upsert dos templates aprovados de UM canal (M9)."""
    from apps.inbox.models import Channel, WhatsAppTemplate
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    channel = Channel.objects.filter(pk=channel_id).first()
    if channel is None:
        return "skipped: canal ausente"

    provider = get_whatsapp_provider(channel)
    count = 0
    with suppress(WhatsAppUnavailableError, WhatsAppRateLimitedError):
        for template in provider.list_templates():
            WhatsAppTemplate.objects.update_or_create(
                clinic=channel.clinic,
                name=template.name,
                language=template.language,
                defaults={
                    "category": template.category,
                    "status": template.status,
                    "components": template.components,
                },
            )
            count += 1
    return {"channel_id": channel_id, "templates": count}
