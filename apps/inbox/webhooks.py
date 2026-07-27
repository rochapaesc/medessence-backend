"""
Webhook Meta Cloud API (§7): endpoint ÚNICO da plataforma.

GET = verificação da assinatura do app (a Meta manda `hub.challenge` ao
configurar o webhook); POST = eventos, autenticados por `X-Hub-Signature-256`
(HMAC-SHA256 do corpo com o app secret, comparação em tempo constante). O
canal sai de `value.metadata.phone_number_id` — multi-tenant resolvido POR
PAYLOAD, nunca por URL.

Responde 200 imediato e joga o processamento na fila (RNF-4); o payload cru
fica em WebhookEvent para replay. Não-2xx faz a Meta reenviar com backoff —
por isso número desconhecido também recebe 200 (com rastro de erro), senão
um canal desativado viraria retry infinito do lado deles.
"""

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.inbox.choices import WebhookSource
from apps.inbox.models import Channel, WebhookEvent
from apps.inbox.tasks import process_whatsapp_webhook

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    if request.method == "GET":
        return _verify_subscription(request)
    return _receive_events(request)


def _verify_subscription(request):
    """Devolve `hub.challenge` se o verify token bate. Sem token configurado,
    recusa tudo — fail closed, nunca um webhook aberto por engano."""
    token = settings.WHATSAPP_VERIFY_TOKEN
    provided = request.GET.get("hub.verify_token", "")
    if (
        request.GET.get("hub.mode") == "subscribe"
        and token
        and hmac.compare_digest(provided, token)
    ):
        return HttpResponse(request.GET.get("hub.challenge", ""))
    return HttpResponseForbidden()


def _signature_ok(request) -> bool:
    secret = settings.WHATSAPP_APP_SECRET
    if not secret:
        return False  # fail closed: sem app secret não há webhook confiável
    header = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def _phone_number_ids(payload: dict) -> list[str]:
    """Números distintos citados no payload, na ordem em que aparecem."""
    ids: list[str] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            metadata = change.get("value", {}).get("metadata") or {}
            pid = metadata.get("phone_number_id", "")
            if pid and pid not in ids:
                ids.append(pid)
    return ids


def _slice_for(payload: dict, phone_number_id: str) -> dict:
    """
    Recorta o payload para UM número. Payload multi-número é teórico (hoje
    1 clínica = 1 número), mas fronteira de tenant não trabalha com
    "provavelmente": cada clínica processa e ARQUIVA só o que é dela.
    """
    entries = []
    for entry in payload.get("entry", []):
        changes = [
            change
            for change in entry.get("changes", [])
            if (change.get("value", {}).get("metadata") or {}).get("phone_number_id")
            == phone_number_id
        ]
        if changes:
            entries.append({**entry, "changes": changes})
    return {**payload, "entry": entries}


def _receive_events(request):
    if not _signature_ok(request):
        return HttpResponseForbidden()

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        payload = {"_invalid_json": request.body.decode("utf-8", "replace")[:2000]}

    phone_ids = _phone_number_ids(payload)
    if not phone_ids:
        # Assinado pela Meta, mas sem número — update de conta/template etc.
        # Fica o rastro global; tipos novos são assunto da F3+.
        WebhookEvent.objects.create(source=WebhookSource.META, clinic=None, payload=payload)
        return HttpResponse(status=200)

    single = len(phone_ids) == 1
    for phone_id in phone_ids:
        sliced = payload if single else _slice_for(payload, phone_id)
        channel = Channel.objects.filter(phone_number_id=phone_id).first()
        if channel is None:
            WebhookEvent.objects.create(
                source=WebhookSource.META,
                clinic=None,
                payload=sliced,
                error=f"phone_number_id sem canal: {phone_id}",
            )
            logger.warning("Webhook Meta para número desconhecido %s", phone_id)
            continue
        event = WebhookEvent.objects.create(
            source=WebhookSource.META, clinic=channel.clinic, payload=sliced
        )
        process_whatsapp_webhook.delay(event.pk, channel.pk)
    return HttpResponse(status=200)
