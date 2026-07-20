"""
Endpoint de webhook do WhatsApp (§7).

Sem verify token da Meta: a URL por canal é a credencial
(`/webhooks/whatsapp/{uuid}/{secret}/`). Responde 200 IMEDIATO e joga o
processamento na fila (RNF-4) - o payload cru fica em WebhookEvent para replay.
"""

import hmac
import json
import logging

from django.http import HttpResponse, HttpResponseNotFound
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.inbox.choices import WebhookSource
from apps.inbox.models import Channel, WebhookEvent
from apps.inbox.tasks import process_whatsapp_webhook

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["POST"])
def whatsapp_webhook(request, channel_uuid, secret):
    channel = Channel.objects.filter(uuid=channel_uuid).first()
    # Segredo errado / canal inexistente → 404 (não vaza qual dos dois falhou),
    # comparação em tempo constante.
    if channel is None or not hmac.compare_digest(channel.webhook_secret, secret):
        return HttpResponseNotFound()

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        payload = {"_invalid_json": request.body.decode("utf-8", "replace")[:2000]}

    event = WebhookEvent.objects.create(
        source=WebhookSource.DATAFY,
        clinic=channel.clinic,
        payload=payload,
    )
    process_whatsapp_webhook.delay(event.pk, channel.pk)
    return HttpResponse(status=200)
