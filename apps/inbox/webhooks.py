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


def _segredos_possiveis(payload: dict) -> list[str]:
    """
    Os app secrets que podem ter assinado esta chamada, na ordem de tentativa.

    Primeiro o do CANAL (em `credentials["app_secret"]`), depois o global do
    ambiente. É o desenho do `meta_app_secrets` do Chatwoot
    (`webhooks/whatsapp_controller.rb`), e ele existe para o caso que temos
    aqui: um WABA compartilhado entre dois produtos, em que o secret do
    ambiente é de OUTRO app e o número desta clínica pertence a um app
    próprio. Sem isto, uma das duas pontas fica sem receber para sempre.
    """
    segredos: list[str] = []
    for canal in _canais_do_payload(payload):
        do_canal = (canal.credentials or {}).get("app_secret") or ""
        if do_canal and do_canal not in segredos:
            segredos.append(do_canal)
    global_ = settings.WHATSAPP_APP_SECRET
    if global_ and global_ not in segredos:
        segredos.append(global_)
    return segredos


def _signature_ok(request, payload: dict | None = None) -> bool:
    """
    A assinatura da Meta bate?

    ⚠️ Recusa que NÃO deixa rastro é recusa impossível de depurar (18/08, em
    produção: "coloquei o webhook e não recebo nada", sem uma linha em lugar
    nenhum). O 403 continua igual para quem chama; o que muda é o log daqui,
    que diz QUAL das três coisas aconteceu. Nada de segredo no log: só se o
    cabeçalho veio e o tamanho dele.
    """
    segredos = _segredos_possiveis(payload or {})
    if not segredos:
        logger.error(
            "Webhook RECUSADO: nenhum app secret disponível. Configure o "
            "WHATSAPP_APP_SECRET do ambiente, ou o `app_secret` nas "
            "credenciais do canal desta clínica. Sem ele nada entra."
        )
        return False  # fail closed: sem app secret não há webhook confiável

    header = request.headers.get("X-Hub-Signature-256", "")
    if not header:
        logger.warning(
            "Webhook RECUSADO: chamada sem X-Hub-Signature-256. Ou não é a "
            "Meta, ou algum proxy está removendo o cabeçalho."
        )
        return False

    for segredo in segredos:
        esperado = (
            "sha256="
            + hmac.new(segredo.encode(), request.body, hashlib.sha256).hexdigest()
        )
        if hmac.compare_digest(header, esperado):
            return True

    logger.warning(
        "Webhook RECUSADO: assinatura não confere com nenhum dos %d app "
        "secret(s) conhecidos (corpo de %d bytes). O secret configurado é de "
        "OUTRO app da Meta, ou não é o app dono deste número.",
        len(segredos),
        len(request.body or b""),
    )
    return False


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


def _waba_ids(payload: dict) -> list[str]:
    """
    As contas citadas no payload (o `entry[].id` é o WABA).

    ⚠️ Existe porque **nem todo evento fala de um número**: o `account_update`
    que avisa que a clínica removeu a integração (RF-CON-5.4) é da CONTA, e vem
    sem `metadata.phone_number_id`. Sem este caminho ele cairia no ramo
    "assinado pela Meta, mas sem número" e ninguém o leria.
    """
    ids: list[str] = []
    for entry in payload.get("entry", []):
        waba = str(entry.get("id") or "")
        if waba and waba not in ids:
            ids.append(waba)
    return ids


def _canais_do_payload(payload: dict) -> list:
    """
    Os canais que este payload alcança: pelo número, e só na falta dele, pela
    conta. Serve à conferência da assinatura E ao roteamento, para os dois não
    divergirem — secret achado por um caminho e canal achado por outro seria
    webhook aceito e jogado fora.
    """
    canais = []
    vistos: set[int] = set()
    for phone_id in _phone_number_ids(payload):
        canal = Channel.objects.filter(phone_number_id=phone_id).first()
        if canal is not None and canal.pk not in vistos:
            vistos.add(canal.pk)
            canais.append(canal)
    if canais:
        return canais
    for waba_id in _waba_ids(payload):
        canal = Channel.objects.filter(waba_id=waba_id).first()
        if canal is not None and canal.pk not in vistos:
            vistos.add(canal.pk)
            canais.append(canal)
    return canais


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
    # ⚠️ O corpo é lido ANTES da conferência porque o app secret pode ser o do
    # CANAL, e achar o canal exige o `phone_number_id` de dentro do payload
    # (é o que o Chatwoot faz). O payload não verificado serve SÓ para essa
    # busca: nada é gravado nem processado antes da assinatura bater.
    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        payload = {"_invalid_json": request.body.decode("utf-8", "replace")[:2000]}

    if not _signature_ok(request, payload):
        return HttpResponseForbidden()

    phone_ids = _phone_number_ids(payload)
    if not phone_ids:
        # Sem número no payload, o evento ainda pode ser da CONTA de uma
        # clínica (RF-CON-5.4): aí ele é processado como qualquer outro, com o
        # canal achado pelo WABA. Sem dono conhecido, fica o rastro global.
        canal = next(iter(_canais_do_payload(payload)), None)
        event = WebhookEvent.objects.create(
            source=WebhookSource.META,
            clinic=canal.clinic if canal else None,
            payload=payload,
        )
        if canal is not None:
            process_whatsapp_webhook.delay(event.pk, canal.pk)
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
