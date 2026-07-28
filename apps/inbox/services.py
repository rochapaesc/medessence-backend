"""
Serviços do inbox:
  1. Denormalização da conversa a partir das mensagens (RF-INB-1) - signal.
  2. Ingestão de eventos do webhook (§7) - idempotente por wamid.
  3. Envio de mensagens OUT via provider (§7).

O provedor entra sempre pelo registry (`get_whatsapp_provider(channel)`) -
este módulo não conhece Datafy.
"""

import logging

from django.db.models import F
from django.utils import timezone

from apps.inbox.choices import MessageDirection, MessageKind, MessageStatus, SenderKind
from apps.integrations.whatsapp.base import WhatsAppEventKind

logger = logging.getLogger(__name__)

PREVIEW_MAX = 200


def _preview(message) -> str:
    text = message.body or message.caption or message.get_kind_display()
    return text[:PREVIEW_MAX]


def apply_message_to_conversation(message, *, created: bool) -> None:
    """Atualiza os campos denormalizados da conversa após salvar uma mensagem.

    Só reage à CRIAÇÃO: atualizações posteriores (ex.: status de entrega)
    não mexem em prévia nem em contador, evitando dupla contagem.
    """
    if not created:
        return

    conversation = message.conversation
    fields = []

    # Prévia/recência só avançam para frente no tempo (mensagens fora de ordem
    # do webhook não retrocedem a listagem).
    if conversation.last_message_at is None or message.wa_timestamp >= conversation.last_message_at:
        conversation.last_message_at = message.wa_timestamp
        conversation.last_message_preview = _preview(message)
        fields += ["last_message_at", "last_message_preview"]

    if message.direction == MessageDirection.IN:
        conversation.last_inbound_at = message.wa_timestamp
        conversation.unread_count = F("unread_count") + 1
        fields += ["last_inbound_at", "unread_count"]

    # Primeira resposta HUMANA depois de um inbound (RF-ATD-11). Nota interna
    # não conta: o paciente não a recebeu.
    elif (
        conversation.first_response_at is None
        and message.sender_kind == SenderKind.AGENT
        and not message.is_internal
        and conversation.last_inbound_at is not None
    ):
        conversation.first_response_at = message.wa_timestamp
        fields.append("first_response_at")

    if fields:
        conversation.save(update_fields=[*fields, "updated_at"])

    # Reabertura (RF-ATD-2): mensagem do paciente ressuscita conversa dormente.
    # Depois do save para não brigar pelos mesmos campos.
    if message.direction == MessageDirection.IN:
        from apps.inbox.attendance import reopen

        reopen(conversation, by_contact=True)


# --------------------------------------------------------------------- #
# Ingestão de eventos do webhook (§7) - idempotente por wamid
# --------------------------------------------------------------------- #


def ingest_events(channel, events) -> dict:
    """Aplica uma lista de `WhatsAppEvent` normalizados. Idempotente: reentrega
    do webhook não duplica (unique clinic+wamid) nem recontagem."""
    stats = {"inbound": 0, "echo": 0, "status": 0, "ignored": 0}
    for event in events:
        if event.kind == WhatsAppEventKind.INBOUND:
            stats["inbound"] += bool(_ingest_message(channel, event, SenderKind.CONTACT))
        elif event.kind == WhatsAppEventKind.ECHO:
            stats["echo"] += bool(_ingest_message(channel, event, SenderKind.AGENT))
        elif event.kind == WhatsAppEventKind.STATUS:
            stats["status"] += bool(_apply_status(channel, event))
        else:
            stats["ignored"] += 1
    return stats


def _get_or_create_conversation(channel, event):
    from apps.patients.models import Contact, PatientContact

    contact, _ = Contact.objects.get_or_create(
        clinic=channel.clinic,
        wa_id=event.wa_id,
        defaults={"display_name": event.contact_name[:160]},
    )
    conversation, created = channel.conversations.get_or_create(
        clinic=channel.clinic,
        channel=channel,
        contact=contact,
    )
    if created:
        # Vínculo automático quando o número já tem paciente principal (RF-INB-7);
        # ambíguo/sem vínculo fica para a desambiguação manual.
        link = (
            PatientContact.objects.filter(contact=contact)
            .order_by("-is_primary", "pk")
            .select_related("patient")
            .first()
        )
        if link is not None:
            conversation.patient = link.patient
            conversation.save(update_fields=["patient", "updated_at"])
    return conversation


def _ingest_message(channel, event, sender_kind) -> bool:
    """Upsert idempotente de uma mensagem IN (contato) ou echo (OUT do celular)."""
    from apps.inbox.models import MediaAsset, Message

    if not event.provider_message_id:
        return False

    existing = Message.objects.filter(
        clinic=channel.clinic, provider_message_id=event.provider_message_id
    ).first()
    if existing is not None:
        return False  # reentrega - idempotente

    conversation = _get_or_create_conversation(channel, event)

    media = None
    if event.media_id:
        media = MediaAsset.objects.create(
            clinic=channel.clinic,
            provider_media_id=event.media_id,
            mime_type=event.mime_type,
        )

    message = Message.objects.create(
        clinic=channel.clinic,
        conversation=conversation,
        provider_message_id=event.provider_message_id,
        sender_kind=sender_kind,
        kind=event.message_kind or MessageKind.TEXT,
        body=event.body,
        caption=event.caption,
        media=media,
        reply_to_provider_id=event.reply_to_provider_id,
        wa_timestamp=event.wa_timestamp or timezone.now(),
        raw_payload=event.raw,
    )

    if media is not None:
        from apps.inbox.tasks import fetch_media_asset

        fetch_media_asset.delay(media.pk)

    # Realtime (§12): a conversa vem atualizada pelo signal de Message.
    from apps.inbox.realtime import notify_conversation_updated, notify_message_new

    conversation.refresh_from_db()
    notify_message_new(message)
    notify_conversation_updated(conversation)
    return message is not None


# Escala de progresso do status (guarda de ordem): a Meta entrega webhooks
# fora de ordem e duplicados - sem isto, um `delivered` atrasado REGRIDE um
# `read` na tela. FAILED fica entre SENT e DELIVERED de propósito: sobrescreve
# um envio (a entrega falhou), mas nunca uma entrega confirmada - e um
# delivered posterior o supera (se entregou, entregou). Mesmo padrão
# anti-regressão do in_progress no EHR (§10.2).
_STATUS_RANK = {
    "": 0,
    MessageStatus.SENT: 1,
    MessageStatus.FAILED: 2,
    MessageStatus.DELIVERED: 3,
    MessageStatus.READ: 4,
}


def _apply_status(channel, event) -> bool:
    """Atualiza o status de entrega de uma mensagem OUT (sent→delivered→read)."""
    from apps.inbox.models import Message

    if not event.provider_message_id or not event.status:
        return False

    rank = _STATUS_RANK.get(event.status, 0)
    # Só status ATRÁS na escala podem ser sobrescritos - filtro no UPDATE, e
    # não em Python, para a corrida entre dois webhooks paralelos não anular
    # a guarda.
    can_overwrite = [s for s, r in _STATUS_RANK.items() if r < rank]
    alvo = Message.objects.filter(
        clinic=channel.clinic,
        provider_message_id=event.provider_message_id,
        status__in=can_overwrite,
    )
    # A conversa é lida ANTES do update: o evento de realtime precisa dela
    # para o cliente saber que thread atualizar, e depois do UPDATE o
    # queryset não casa mais (o status já mudou).
    conversation_id = alvo.values_list("conversation_id", flat=True).first()
    updated = alvo.update(
        status=event.status,
        # FAILED chega com o motivo; um status de sucesso posterior limpa o
        # motivo de um FAILED fora de ordem que ele acabou de superar.
        status_error=event.status_error if event.status == MessageStatus.FAILED else "",
        updated_at=timezone.now(),
    )
    if updated:
        from apps.inbox.realtime import notify_message_status

        notify_message_status(
            channel.clinic_id,
            event.provider_message_id,
            event.status,
            conversation_id=conversation_id,
        )
    return bool(updated)


# --------------------------------------------------------------------- #
# Envio de mensagens OUT (§7)
# --------------------------------------------------------------------- #


def create_internal_note(conversation, user, body: str):
    """
    Nota da equipe (RF-ATD-3): existe na thread e NUNCA sai para o paciente.

    Nasce sem `provider_message_id` e com `is_internal=True`; quem impede o
    envio é a guarda em `send_message` — aqui só se cria o registro.
    """
    from apps.inbox.models import Message

    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=MessageKind.TEXT,
        sender_kind=SenderKind.AGENT,
        sent_by=user,
        body=body,
        is_internal=True,
        wa_timestamp=timezone.now(),
    )


def send_message(message) -> None:
    """Envia uma mensagem OUT pendente pelo provider do canal e grava o wamid
    e o status. Chamado pela task `send_whatsapp_message`."""
    # Última barreira da nota interna (RF-ATD-3). A task não deveria ser
    # enfileirada para ela, mas o custo do erro é o paciente ler comentário da
    # equipe — a guarda fica também aqui, no ponto onde a mensagem SAI.
    if message.is_internal:
        return
    from apps.integrations.whatsapp.exceptions import (
        WhatsAppError,
        WhatsAppRateLimitedError,
        WhatsAppUnavailableError,
    )
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    conversation = message.conversation
    provider = get_whatsapp_provider(conversation.channel)
    to = conversation.contact.wa_id

    try:
        if message.kind == MessageKind.TEMPLATE and message.template_name:
            result = provider.send_template(to, message.template_name, "pt_BR")
        else:
            result = provider.send_text(
                to, message.body, message.reply_to_provider_id or None
            )
    except (WhatsAppRateLimitedError, WhatsAppUnavailableError):
        # Transitórios: sobem para o autoretry da task - a mensagem continua
        # pendente e a fila tenta de novo.
        raise
    except WhatsAppError as exc:
        # Erro de negócio (janela fechada, número inválido...): retry não
        # resolve. A mensagem morre FAILED **com o motivo** - antes desta
        # captura ela ficava pendente para sempre, sem explicação nenhuma.
        message.status = MessageStatus.FAILED
        message.status_error = str(exc)
        message.save(update_fields=["status", "status_error", "updated_at"])
        # Sem realtime aqui: o evento message:status é endereçado por wamid,
        # que uma falha de envio não tem. A resposta REST do composer e o
        # refetch da thread mostram o estado.
        return

    message.provider_message_id = result.provider_message_id
    message.status = result.status or MessageStatus.SENT
    message.save(update_fields=["provider_message_id", "status", "updated_at"])

    # Realtime (§12): telas dos demais atendentes veem a mensagem enviada.
    from apps.inbox.realtime import notify_message_new

    notify_message_new(message)
