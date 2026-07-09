"""
Denormalização da conversa a partir das mensagens (RF-INB-1).

A listagem por recência não pode varrer a thread a cada render, então
`Conversation` carrega `last_message_*`, `last_inbound_at` e `unread_count`
mantidos aqui — chamado pelo signal `post_save` de Message.
"""

from django.db.models import F

from apps.inbox.choices import MessageDirection

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

    if fields:
        conversation.save(update_fields=[*fields, "updated_at"])
