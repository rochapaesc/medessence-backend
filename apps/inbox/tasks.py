"""
Jobs Celery do inbox (§13).

  process_whatsapp_webhook  (webhooks) - parse + ingestão idempotente
  fetch_media_asset         (media)    - resolve URL temporária, baixa, re-hospeda
  send_whatsapp_message     (outbound) - envio com retry/backoff em 429
  refresh_wa_templates      (sync, beat 6h) - cache de templates aprovados
  wake_snoozed_conversations (sync, beat 1min) - adiada vence -> volta p/ fila

O provedor entra sempre pelo registry - as tasks não conhecem Datafy.
"""

import logging
import mimetypes
from contextlib import suppress

from celery import shared_task
from django.core.files.base import ContentFile
from django.utils import timezone

from apps.integrations.whatsapp.exceptions import WhatsAppRateLimitedError, WhatsAppUnavailableError

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
    """Baixa o ativo pelo provedor e re-hospeda (RF-INB-6).

    O download é do ADAPTER, não desta task: na Cloud API a URL de mídia
    expira em ~5 min e exige o token do canal - baixar "por fora" com uma
    URL solta era coisa do Datafy (30 dias públicos) e morreu com ele.

    A task termina SEMPRE com um estado: pronta, ou falhou com motivo. Antes
    ela saía calada por três caminhos diferentes e a mídia ficava "baixando…"
    para sempre na tela de quem estava esperando.
    """
    from apps.inbox.audio import ler_metadados
    from apps.inbox.choices import MediaState
    from apps.inbox.models import Channel, MediaAsset
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    media = MediaAsset.objects.filter(pk=media_asset_id).first()
    if media is None:
        return "skipped: mídia não existe mais"
    if media.stored_file:
        return "skipped: já baixada"

    channel = Channel.objects.filter(clinic=media.clinic).first()
    if channel is None:
        return _media_falhou(media, "Clínica sem canal de WhatsApp configurado")

    from apps.inbox.services import registrar_saude_do_canal

    try:
        downloaded = get_whatsapp_provider(channel).download_media(media.provider_media_id)
    except Exception as exc:  # captura larga de propósito: o motivo tem de chegar à tela
        logger.exception("Falha ao baixar mídia %s", media_asset_id)
        registrar_saude_do_canal(channel, erro=exc)
        return _media_falhou(media, _motivo_humano(exc))

    registrar_saude_do_canal(channel)

    if not downloaded.content:
        # A URL da Meta expira: mídia velha simplesmente não vem mais.
        return _media_falhou(media, "O provedor não devolveu o arquivo")

    content = downloaded.content
    mime = downloaded.mime_type or media.mime_type or ""
    media.mime_type = mime or media.mime_type
    media.size_bytes = len(content)
    media.duration_ms, media.waveform = ler_metadados(content, media.mime_type)
    media.state = MediaState.READY
    media.error = ""
    media.stored_file.save(_nome_do_arquivo(media), ContentFile(content), save=False)
    media.save()
    _avisar_a_tela(media)
    return {"media_id": media.pk, "bytes": len(content)}


def _motivo_humano(exc: Exception) -> str:
    """
    O erro como a recepção precisa lê-lo.

    `str(exc)` de um erro da Meta é um despejo com fbtrace_id e objeto de
    resposta dentro — texto de log, não de tela. Quem está atendendo precisa
    saber duas coisas: se adianta tentar de novo e o que fazer se não adianta.
    """
    from apps.integrations.whatsapp.exceptions import (
        WhatsAppAuthError,
        WhatsAppNotConfiguredError,
        WhatsAppRateLimitedError,
        WhatsAppUnavailableError,
    )

    if isinstance(exc, WhatsAppAuthError):
        return "Canal do WhatsApp desconectado. Avise o suporte para reconectar."
    if isinstance(exc, WhatsAppNotConfiguredError):
        return "Canal do WhatsApp sem credenciais configuradas."
    if isinstance(exc, WhatsAppRateLimitedError):
        return "Limite de downloads do WhatsApp atingido. Tente daqui a pouco."
    if isinstance(exc, WhatsAppUnavailableError):
        return "O WhatsApp não respondeu agora. Tente de novo."
    return "Não foi possível baixar o arquivo."


def _media_falhou(media, motivo: str):
    """Falha com MOTIVO e com aviso: mídia parada em silêncio vira um buraco
    mudo na conversa, e quem está olhando não sabe se espera ou desiste."""
    from apps.inbox.choices import MediaState

    media.state = MediaState.FAILED
    media.error = (motivo or "Não foi possível baixar o arquivo")[:200]
    media.save(update_fields=["state", "error"])
    _avisar_a_tela(media)
    return f"failed: {media.error}"


def _avisar_a_tela(media):
    """
    A tela está com um "Baixando…" girando desde que a mensagem chegou. O
    download é assíncrono; sem este aviso, quem já estava com a conversa aberta
    só veria a imagem no próximo F5.
    """
    from apps.inbox.models import Message
    from apps.inbox.realtime import notify_media_updated

    mensagem = Message.objects.filter(media_id=media.pk).order_by("id").first()
    if mensagem is not None:
        notify_media_updated(mensagem, media)


def _nome_do_arquivo(media) -> str:
    """
    Nome no disco: id do provedor + extensão do MIME.

    A extensão sai do `mimetypes` quando ele sabe, e de um mapa nosso quando
    não sabe. `audio/ogg` — que é EXATAMENTE o formato do áudio de voz do
    WhatsApp — devolve None no `guess_extension`, e o arquivo era salvo sem
    extensão nenhuma: o navegador recebia algo que ele não sabia tocar.
    """
    mime = (media.mime_type or "").split(";")[0].strip().lower()
    extensao = EXTENSOES_QUE_O_PYTHON_NAO_SABE.get(mime) or mimetypes.guess_extension(mime) or ""
    return f"{media.provider_media_id}{extensao}"


# MIMEs que a Meta manda e a biblioteca padrão do Python não conhece.
EXTENSOES_QUE_O_PYTHON_NAO_SABE = {
    "audio/ogg": ".ogg",  # áudio de voz do WhatsApp (opus)
    "audio/opus": ".opus",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
}


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


@shared_task(
    queue="outbound",
    autoretry_for=(WhatsAppRateLimitedError, WhatsAppUnavailableError),
    retry_backoff=30,
    max_retries=3,
)
def send_whatsapp_reaction(message_id: int, emoji: str):
    """
    Leva o selo da equipe para o WhatsApp do paciente.

    Em task e não no request porque o selo já apareceu na tela pelo evento de
    tempo real: fazer a recepção esperar a Meta para ver o próprio 👍 seria
    lento à toa. Se a Meta recusar, o selo continua nosso e a mensagem de erro
    não vale interromper quem atende — é reação, não resposta.
    """
    from apps.inbox.models import Message
    from apps.integrations.whatsapp.exceptions import WhatsAppError
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    message = (
        Message.objects.filter(pk=message_id)
        .select_related("conversation__channel", "conversation__contact")
        .first()
    )
    if message is None or not message.provider_message_id:
        return "skipped: mensagem sem wamid"

    conversation = message.conversation
    provider = get_whatsapp_provider(conversation.channel)
    try:
        provider.send_reaction(
            conversation.contact.wa_id, message.provider_message_id, emoji
        )
    except WhatsAppError as exc:
        logger.warning("reação recusada pela Meta: %s", exc)
        return "falhou"
    return {"message_id": message_id, "emoji": emoji}


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


@shared_task(queue="sync")
def wake_snoozed_conversations():
    """
    Varredura por minuto (beat): adiamento vencido volta para a fila
    (RF-ATD-1.2). Varredura, e não ETA por conversa no broker: "às 9h" tem
    precisão de minuto, e agendamento por mensagem se perde em restart de fila.

    Idempotente por construção - o `wake_snoozed` só transiciona quem AINDA
    está adiada, então varredura dupla e reabertura do paciente não duplicam
    nada.
    """
    from apps.inbox.attendance import wake_snoozed
    from apps.inbox.choices import ConversationStatus
    from apps.inbox.models import Conversation

    vencidas = Conversation.objects.filter(
        deleted_at__isnull=True,
        status=ConversationStatus.SNOOZED,
        snoozed_until__lte=timezone.now(),
    ).select_related("clinic", "assigned_to")

    acordadas = sum(1 for conversation in vencidas if wake_snoozed(conversation))
    return {"woken": acordadas}
