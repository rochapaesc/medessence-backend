"""
Emissão de eventos em tempo real (§12). Papel único: empurrar eventos enxutos
para o grupo da clínica. A fonte da verdade continua a API REST.

Contrato de eventos (servidor → cliente):
    message:new          · conversation_id, message{mínimo, com media e caption}
    message:status       · provider_message_id | message_id, status, error
    media:updated        · conversation_id, message_id, media{estado novo}
    channel:health       · disconnected, reason, display_number,
                           failed_messages (clínica inteira)
    message:reaction     · conversation_id, message_id, reactions[] (com dono)
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


def _reactions_min(message) -> list[dict]:
    """
    Os selos da mensagem, com DONO. Uma linha por ator (tabela copiada do
    wacrm): sem o ator, a tela mostrava um emoji sem dizer de quem era e a
    clínica reagindo pelo celular apagava a reação do paciente.
    """
    return [
        {
            "emoji": reacao.emoji,
            "actor_kind": reacao.actor_kind,
            "actor_name": (
                (reacao.actor_user.get_full_name() or reacao.actor_user.email)
                if reacao.actor_user_id
                else ""
            ),
        }
        for reacao in message.reactions.all()
    ]


def _citacao_min(message) -> dict | None:
    """A mensagem citada, como o balão precisa dela. `None` quando ela não está
    no nosso banco (resposta a algo anterior à integração)."""
    if not message.reply_to_provider_id:
        return None
    from apps.inbox.api.serializers.message import citacao_payload
    from apps.inbox.models import Message

    citada = Message.objects.filter(
        clinic_id=message.clinic_id, provider_message_id=message.reply_to_provider_id
    ).first()
    return citacao_payload(citada) if citada else None


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
        "reactions": _reactions_min(message),
        # A citação vai montada: o balão que chega pelo socket tem de nascer
        # com o chip, e o cliente não tem como resolver um wamid sozinho.
        "reply_to_provider_id": message.reply_to_provider_id,
        "reply_to": _citacao_min(message),
        "content_data": message.content_data,
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
        # Coexistência (RF-CON-5.1): o balão precisa nascer sabendo que a
        # resposta saiu do celular. Sem isto ele chega pelo socket sem a
        # assinatura e ganha assinatura só depois de um F5, que é a mesma
        # divergência que a janela de 24h já causou.
        "from_phone": message.from_phone,
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
            "reactions": _reactions_min(message),
        },
    )


def notify_channel_health(channel) -> None:
    """
    O canal caiu (ou voltou). Vai para a clínica inteira porque o problema é
    da clínica inteira: sem isto, a faixa só apareceria no próximo F5 — e
    quem está atendendo continuaria escrevendo mensagens que não saem.
    """
    from apps.inbox.services import mensagens_para_reenviar

    _broadcast(
        channel.clinic_id,
        {
            "event": "channel:health",
            "disconnected": channel.disconnected,
            "reason": channel.disconnect_reason,
            "display_number": channel.display_number,
            # Quantas ficaram presas na queda. Sem este número no MESMO
            # evento, a faixa verde de "reconectado" apareceria sem saber se
            # há algo a reenviar, e só descobriria no próximo F5 — que é
            # justamente quando a recepção já voltou a digitar.
            "failed_messages": mensagens_para_reenviar(channel.clinic_id).count(),
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


#: "não me passaram nada", que é diferente de "me passaram nada". Ver
#: `notify_conversation_updated`.
AUSENTE = object()


def notify_conversation_updated(conversation, *, sequencias_segurando=AUSENTE) -> None:
    """
    A fila muda para todo mundo, não só para quem agiu: status e posse vão no
    evento porque é por eles que a conversa SAI da lista de quem está olhando
    (resolvida, adiada) e é por eles que o composer trava (RF-ATD-14). Sem
    isso, quem não agiu continuaria escrevendo numa conversa que já é de
    outra pessoa até apertar F5.

    ⚠️ `sequencias_segurando` (RF-SEQ-5.5) só viaja quando QUEM CHAMA o passa, e
    quem passa é o motor de sequência, nos dois instantes em que o valor muda:
    ao segurar e ao soltar. Este evento dispara a cada mensagem; calcular a
    espera em todos eles seria uma consulta por mensagem para responder quase
    sempre a mesma coisa. Campo ausente faz o cliente PRESERVAR o que tem, que
    é o mesmo contrato já usado pela posse.
    """
    payload_espera = (
        {} if sequencias_segurando is AUSENTE else {"sequencias_segurando": sequencias_segurando}
    )
    _broadcast(
        conversation.clinic_id,
        {
            **payload_espera,
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
            # ⚠️ A janela de 24h TEM de vir aqui (11/08/2026). Ela é derivada
            # de `last_inbound_at`, então a mensagem do paciente ABRE a janela
            # no mesmo instante em que dispara este evento. Sem o campo, a
            # tela mantinha o valor velho e continuava mostrando o selo de
            # janela fechada com o paciente acabando de escrever - o composer
            # pedia template para uma conversa que aceitava texto livre.
            "window_open": conversation.window_open,
            "last_inbound_at": (
                conversation.last_inbound_at.isoformat()
                if conversation.last_inbound_at
                else None
            ),
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


def notify_conversation_updated_on_commit(conversation, *, sequencias_segurando=AUSENTE) -> None:
    """Como o `notify_message_new_on_commit`: quem cria mensagem dentro de
    transação emite só depois do commit."""
    from django.db import transaction

    transaction.on_commit(
        lambda: notify_conversation_updated(
            conversation, sequencias_segurando=sequencias_segurando
        )
    )


def notify_conversation_new(conversation) -> None:
    """
    Uma conversa NASCEU (RF-INB-1.2).

    ⚠️ Existe porque `conversation:updated` não bastava, e o defeito era mudo:
    o cliente só sabe atualizar conversa que já está na lista dele, então o
    aviso de uma conversa que ele nunca viu era descartado em silêncio. Quem
    disparava uma sequência via a mensagem sair, o paciente respondia, e a
    conversa **só aparecia depois de recarregar a página**.

    Manda a LINHA INTEIRA, e não só o id, de propósito: com o id o cliente
    teria de buscar cada uma, e uma campanha para mil pessoas viraria mil
    requisições em poucos minutos. É o mesmo desenho do `conversation.created`
    do Chatwoot, que também transmite o objeto.

    Quem decide se ela entra na lista é o CLIENTE, porque o filtro aberto (fila,
    etiqueta, busca) é estado de tela e o servidor não o conhece.
    """
    from apps.inbox.api.serializers import ConversationSerializer

    _broadcast(
        conversation.clinic_id,
        {
            "event": "conversation:new",
            "conversation_id": conversation.pk,
            "conversation": ConversationSerializer(conversation).data,
        },
    )


def notify_conversation_new_on_commit(conversation) -> None:
    """A conversa nasce dentro da transação do disparo; avisar antes do commit
    faria o cliente pedir uma linha que ainda não existe para quem lê."""
    from django.db import transaction

    transaction.on_commit(lambda: notify_conversation_new(conversation))


def notify_message_status(
    clinic_id: int,
    provider_message_id: str,
    status: str,
    conversation_id: int | None = None,
    *,
    message_id: int | None = None,
    error: str = "",
) -> None:
    """
    Tique de entrega. `conversation_id` vai junto porque o cliente precisa
    saber QUAL thread atualizar - sem ele, a tela teria de procurar o wamid
    em todas as conversas abertas (ou recarregar por um tique).

    `message_id` existe para o caso que o wamid NÃO cobre: a mensagem que
    falhou no envio nunca chegou a ter wamid. Sem ele, o balão ficava eterno
    em "enviando" e a recepção só descobria que não saiu porque o paciente
    não respondeu — foi exatamente o que aconteceu na queda de 29/07.
    """
    _broadcast(
        clinic_id,
        {
            "event": "message:status",
            "conversation_id": conversation_id,
            "provider_message_id": provider_message_id,
            "message_id": message_id,
            "status": status,
            "error": error,
        },
    )
