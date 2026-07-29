"""
Emissão de eventos em tempo real (§12). Papel único: empurrar eventos enxutos
para o grupo da clínica. A fonte da verdade continua a API REST.

Contrato de eventos (servidor → cliente):
    message:new          · conversation_id, message{mínimo, com media e caption}
    message:status       · provider_message_id, status
    media:updated        · conversation_id, message_id, media{estado novo}
    message:reaction     · conversation_id, message_id, reaction (vazio = removida)
    conversation:updated · conversation_id, unread_count, preview, status,
                           attended_by, assigned_to, assigned_to_name
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


def _media_min(message) -> dict | None:
    """
    A mídia do balão, no MESMO formato do REST (`media_payload`) — dois
    formatos para a mesma coisa fariam o balão mudar de forma quando o socket
    chegasse antes da resposta do REST.

    A URL sai RELATIVA aqui: o socket não tem request para saber o host
    público, e o cliente já sabe resolver contra a origem da API. No REST ela
    sai absoluta, e o cliente aceita as duas.
    """
    if not message.media_id:
        return None
    from apps.inbox.api.serializers.message import media_payload

    return media_payload(message.media)


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
        # Corpo e legenda viajam SEPARADOS. Iam fundidos num campo só
        # (`body or caption`) e a tela não tinha como saber se aquele texto
        # era a mensagem ou a legenda da foto — resultado: foto virava texto.
        "body": (message.body or "")[:200],
        "caption": (message.caption or "")[:200],
        # Mesmos NOMES do REST: `media` é o id, `media_asset` é o objeto. Já
        # foram a mesma chave com sentidos diferentes nos dois canais, e o
        # cliente quebrava ao receber um objeto onde esperava um número.
        "media": message.media_id,
        "media_asset": _media_min(message),
        "reaction": message.reaction,
        "sender_kind": message.sender_kind,
        "status": message.status,
        "status_error": message.status_error,
        # Nota interna e evento de atividade viajam MARCADOS: sem estes três
        # campos a tela de quem não agiu desenharia a nota como balão comum -
        # ou seja, uma anotação da equipe com cara de mensagem enviada - e o
        # evento como balão vazio.
        "is_internal": message.is_internal,
        "activity_type": message.activity_type,
        "activity_data": message.activity_data,
        "sent_by_name": (
            (message.sent_by.get_full_name() or message.sent_by.email)
            if message.sent_by_id
            else ""
        ),
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


def notify_message_reaction(message) -> None:
    """
    A reação colou numa mensagem que já está na tela. Vai como evento próprio
    (e não como conversa atualizada) porque a fila NÃO deve mexer: um joinha
    não é conversa nova nem pedido de resposta.
    """
    _broadcast(
        message.clinic_id,
        {
            "event": "message:reaction",
            "conversation_id": message.conversation_id,
            "message_id": message.pk,
            "reaction": message.reaction,
        },
    )


def notify_media_updated(message, media) -> None:
    """
    O download terminou (ou desistiu). Vai como evento próprio, e não como um
    `message:new` repetido, porque o balão JÁ ESTÁ na tela — o que mudou foi
    só o estado da mídia dentro dele. Reemitir a mensagem inteira faria a
    thread piscar e correria o risco de duplicar o balão.
    """
    _broadcast(
        message.clinic_id,
        {
            "event": "media:updated",
            "conversation_id": message.conversation_id,
            "message_id": message.pk,
            "media": _media_min(message) if message.media_id == media.pk else None,
        },
    )


def notify_message_new_on_commit(message) -> None:
    """
    Mesma emissão, adiada até o commit. Para quem cria a mensagem DENTRO de uma
    transação (nota interna, evento de atividade): quem recebe o evento consulta
    a API em seguida, e emitir antes do commit faria o cliente ler o estado
    anterior - ou, num rollback, reagir a algo que nunca existiu.
    """
    from django.db import transaction

    transaction.on_commit(lambda: notify_message_new(message))


def notify_conversation_updated(conversation) -> None:
    """
    A fila muda para todo mundo, não só para quem agiu: status e posse vão no
    evento porque é por eles que a conversa SAI da lista de quem está olhando
    (resolvida, adiada) e é por eles que o composer trava (RF-ATD-14). Sem
    isso, quem não agiu continuaria escrevendo numa conversa que já é de
    outra pessoa até apertar F5.
    """
    _broadcast(
        conversation.clinic_id,
        {
            "event": "conversation:updated",
            "conversation_id": conversation.pk,
            "unread_count": conversation.unread_count,
            "preview": conversation.last_message_preview,
            # A ORDEM da fila é recência dentro da prioridade: sem a data real,
            # o cliente usava o relógio local e a ordenação divergia do
            # servidor a cada evento.
            "last_message_at": (
                conversation.last_message_at.isoformat()
                if conversation.last_message_at
                else None
            ),
            "status": conversation.status,
            "attended_by": conversation.attended_by,
            "priority": conversation.priority,
            # Junto com o status: quando a conversa VOLTA para a fila (devolvida,
            # acordada do adiamento), o "aguardando há X" da tela de todo mundo
            # precisa do relógio novo - sem ele, mostraria o da fila anterior.
            "waiting_since": (
                conversation.waiting_since.isoformat() if conversation.waiting_since else None
            ),
            # Nome E id: o nome é para exibir ("Fulana está atendendo"), o id é
            # para o cliente decidir se o "agent" sou EU — sem ele, quem acabou
            # de assumir pelo próprio envio via a tela travar com o próprio
            # nome no banner (aconteceu ao vivo em 28/07).
            "assigned_to": conversation.assigned_to_id,
            "assigned_to_name": (
                conversation.assigned_to.get_full_name() if conversation.assigned_to_id else ""
            ),
            # Etiquetas NÃO vão aqui de propósito: este evento dispara a cada
            # mensagem, e uma lista de objetos por mensagem inflaria o canal
            # inteiro para refletir algo que muda uma ou duas vezes por
            # conversa. Elas chegam pelo REST na próxima carga - o socket
            # avisa, o REST confirma.
        },
    )


def notify_conversation_updated_on_commit(conversation) -> None:
    """Como o `notify_message_new_on_commit`: quem cria mensagem dentro de
    transação emite só depois do commit."""
    from django.db import transaction

    transaction.on_commit(lambda: notify_conversation_updated(conversation))


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
