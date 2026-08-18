"""
Parser do formato Meta Cloud API (§7) → `WhatsAppEvent` normalizado.

A Datafy é proxy da Meta, então o payload do webhook segue o formato
`entry[].changes[].value.{messages, message_echoes, statuses}`. Este módulo
é o ÚNICO lugar que conhece esse formato - o inbox recebe só DTOs.
"""

from datetime import UTC, datetime

from apps.inbox.choices import MessageKind, MessageStatus
from apps.integrations.whatsapp.base import WhatsAppEvent, WhatsAppEventKind

# type do Meta → MessageKind do inbox.
KIND_MAP = {
    "text": MessageKind.TEXT,
    "image": MessageKind.IMAGE,
    "audio": MessageKind.AUDIO,
    "video": MessageKind.VIDEO,
    "document": MessageKind.DOCUMENT,
    "sticker": MessageKind.STICKER,
    "location": MessageKind.LOCATION,
    "contacts": MessageKind.CONTACT,
    "interactive": MessageKind.INTERACTIVE,
    # Botão de template (resposta rápida): é uma resposta de botão como a
    # `interactive`, só que de um template — a tela trata igual.
    "button": MessageKind.INTERACTIVE,
    "template": MessageKind.TEMPLATE,
}
MEDIA_KINDS = {"image", "audio", "video", "document", "sticker"}

# Tipos que NÃO viram balão (regra do Chatwoot, `unprocessable_message_type?`).
# `ephemeral` é mensagem temporária, sem conteúdo para mostrar;
# `request_welcome` avisa que alguém ABRIU a conversa sem escrever nada —
# balão vazio viraria ruído na fila de quem atende. O payload cru continua no
# WebhookEvent, então nada se perde para investigação.
IGNORED_KINDS = {"ephemeral", "request_welcome"}

STATUS_MAP = {
    "sent": MessageStatus.SENT,
    "delivered": MessageStatus.DELIVERED,
    "read": MessageStatus.READ,
    "failed": MessageStatus.FAILED,
}


def _ts(value) -> datetime | None:
    """Timestamp Meta (unix segundos, string) → datetime aware (UTC)."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (ValueError, TypeError, OSError):
        return None


def _names_by_wa_id(value: dict) -> dict:
    names = {}
    for contact in value.get("contacts", []) or []:
        wa_id = contact.get("wa_id", "")
        name = (contact.get("profile") or {}).get("name", "")
        if wa_id:
            names[wa_id] = name
    return names


def _contatos(cartoes: list) -> list[dict]:
    """
    Cartão de contato (vCard) em algo que a tela desenha.

    O WhatsApp repete o MESMO número quando ele tem etiqueta no celular de
    quem mandou (o cartão real que chegou aqui trazia "Antigo" e "CELL" com o
    mesmo telefone). Duas linhas iguais na tela pareceriam defeito nosso, então
    o número é deduplicado pelo `wa_id`.
    """
    resultado = []
    for cartao in cartoes:
        nome = (cartao.get("name") or {}).get("formatted_name", "")
        telefones = []
        vistos = set()
        for telefone in cartao.get("phones") or []:
            wa_id = telefone.get("wa_id") or telefone.get("phone", "")
            if not wa_id or wa_id in vistos:
                continue
            vistos.add(wa_id)
            telefones.append(
                {"phone": telefone.get("phone", ""), "wa_id": telefone.get("wa_id", "")}
            )
        resultado.append({"name": nome, "phones": telefones})
    return resultado


def _localizacao(local: dict) -> dict:
    """
    Coordenadas E rótulo. O wacrm guarda só um texto "nome - endereço - lat,long";
    ficamos com o desenho do Chatwoot, que preserva latitude e longitude —
    texto não abre mapa nenhum.
    """
    nome = local.get("name", "")
    endereco = local.get("address", "")
    titulo = ", ".join(parte for parte in (nome, endereco) if parte)
    return {
        "latitude": local.get("latitude"),
        "longitude": local.get("longitude"),
        "name": nome,
        "address": endereco,
        "url": local.get("url", ""),
        # Sem nome nem endereço (pino solto no mapa), as coordenadas são o
        # único rótulo honesto.
        "title": titulo or f"{local.get('latitude')}, {local.get('longitude')}",
    }


def _resposta_interativa(interativa: dict) -> tuple[str, str]:
    """(título, id) do botão ou item de lista que a pessoa tocou."""
    resposta = interativa.get("button_reply") or interativa.get("list_reply") or {}
    return resposta.get("title", ""), resposta.get("id", "")


def _parse_message(message: dict, *, kind: str, wa_id: str, names: dict) -> WhatsAppEvent:
    meta_type = message.get("type", "")
    message_kind = KIND_MAP.get(meta_type, MessageKind.UNSUPPORTED)

    body = caption = media_id = mime_type = filename = ""
    reaction_emoji = reaction_to = ""
    content_data: dict = {}
    if meta_type == "reaction":
        reacao = message.get("reaction") or {}
        reaction_emoji = reacao.get("emoji", "")
        reaction_to = reacao.get("message_id", "")
    elif meta_type == "text":
        body = (message.get("text") or {}).get("body", "")
    elif meta_type in MEDIA_KINDS:
        payload = message.get(meta_type) or {}
        media_id = payload.get("id", "")
        mime_type = payload.get("mime_type", "")
        caption = payload.get("caption", "")
        # Só documento traz nome, e é o nome que o paciente vê no celular
        # dele. Jogá-lo fora fazia o exame chegar na recepção como
        # "1037387288883307.pdf".
        filename = payload.get("filename", "")
    elif meta_type == "contacts":
        content_data = {"contacts": _contatos(message.get("contacts") or [])}
        # O nome vira o texto da mensagem (regra do Chatwoot): é o que a
        # prévia da fila mostra e o que se busca na conversa.
        body = ", ".join(c["name"] for c in content_data["contacts"] if c["name"])
    elif meta_type == "location":
        content_data = {"location": _localizacao(message.get("location") or {})}
        body = content_data["location"]["title"]
    elif meta_type == "interactive":
        body, resposta_id = _resposta_interativa(message.get("interactive") or {})
        # O ID é o que o motor de jornadas da F3 usa para saber QUAL caminho o
        # paciente escolheu (wacrm): o texto do botão muda a cada template.
        content_data = {"interactive_id": resposta_id}
    elif meta_type == "button":
        botao = message.get("button") or {}
        body = botao.get("text", "")
        content_data = {"interactive_id": botao.get("payload", "")}

    return WhatsAppEvent(
        kind=kind,
        provider_message_id=message.get("id", ""),
        wa_id=wa_id,
        message_kind=message_kind,
        body=body,
        caption=caption,
        media_id=media_id,
        mime_type=mime_type,
        filename=filename,
        content_data=content_data,
        reaction_emoji=reaction_emoji,
        reaction_to=reaction_to,
        reply_to_provider_id=(message.get("context") or {}).get("id", ""),
        wa_timestamp=_ts(message.get("timestamp")),
        contact_name=names.get(wa_id, ""),
        raw=message,
    )


def parse_meta_webhook(payload: dict) -> list[WhatsAppEvent]:
    events: list[WhatsAppEvent] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = _names_by_wa_id(value)

            for message in value.get("messages", []) or []:
                if message.get("type") in IGNORED_KINDS:
                    continue
                events.append(
                    _parse_message(
                        message,
                        kind=WhatsAppEventKind.INBOUND,
                        wa_id=message.get("from", ""),
                        names=names,
                    )
                )

            for echo in value.get("message_echoes", []) or []:
                if echo.get("type") in IGNORED_KINDS:
                    continue
                events.append(
                    _parse_message(
                        echo,
                        kind=WhatsAppEventKind.ECHO,
                        wa_id=echo.get("to", ""),
                        names=names,
                    )
                )

            # Preferência de marketing (RF-SEQ-8.1). Chega no MESMO formato dos
            # demais, numa chave própria do `value`, então entra como mais um
            # laço: `stop` é o contato pedindo para parar promoções, `resume` é
            # ele voltando atrás.
            for pref in value.get("user_preferences", []) or []:
                events.append(
                    WhatsAppEvent(
                        kind=WhatsAppEventKind.PREFERENCE,
                        wa_id=pref.get("wa_id", ""),
                        marketing_opt_out=pref.get("value") == "stop",
                        wa_timestamp=_ts(pref.get("timestamp")),
                        raw=pref,
                    )
                )

            for status in value.get("statuses", []) or []:
                events.append(
                    WhatsAppEvent(
                        kind=WhatsAppEventKind.STATUS,
                        provider_message_id=status.get("id", ""),
                        wa_id=status.get("recipient_id", ""),
                        status=STATUS_MAP.get(status.get("status", ""), ""),
                        status_error=_status_error(status),
                        wa_timestamp=_ts(status.get("timestamp")),
                        raw=status,
                    )
                )
    return events


def _status_error(status: dict) -> str:
    """
    `errors[]` do status FAILED em uma linha legível. O código importa (é o
    que se busca na documentação da Meta) e `error_data.details` costuma ser
    a única parte que explica de verdade.
    """
    parts = []
    for error in status.get("errors", []) or []:
        code = error.get("code", "")
        title = error.get("title", "")
        details = (error.get("error_data") or {}).get("details", "")
        text = " ".join(str(p) for p in (code, title) if p)
        if details and details != title:
            text = f"{text}: {details}" if text else details
        if text:
            parts.append(text)
    return "; ".join(parts)
