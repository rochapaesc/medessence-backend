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
# `errors` entrou em 21/08/2026: é o aviso de falha de sistema/conta da Meta,
# não uma fala do paciente - virava balão VAZIO na conversa. O payload cru
# fica no WebhookEvent, e a saúde do canal já cobre o que é acionável.
IGNORED_KINDS = {"ephemeral", "request_welcome", "errors"}

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
    """
    Nome do perfil, indexado pelo telefone E pelo identificador da Meta.

    ⚠️ As duas chaves são necessárias desde a F2.7: quem adota nome de usuário
    chega SEM telefone, e indexar só por `wa_id` fazia o contato nascer sem
    nome nenhum. A fila mostrava "Contato sem número" para alguém cujo nome a
    Meta tinha acabado de mandar. Achado rodando o `ensaio_de_coexistencia`.
    """
    names = {}
    for contact in value.get("contacts", []) or []:
        name = (contact.get("profile") or {}).get("name", "")
        if not name:
            continue
        for chave in (contact.get("wa_id", ""), contact.get("user_id", "")):
            if chave:
                names[chave] = name
    return names


def _user_ids(value: dict) -> dict:
    """
    O identificador da Meta de cada contato do bloco (RF-CON-6).

    ⚠️ Ele vem em `contacts[].user_id` **sempre**, mesmo para quem não usa nome
    de usuário. É por aqui que o contato ganha o identificador ANTES de o
    telefone sumir, e é isso que torna possível continuar falando com a pessoa
    quando ele sumir.
    """
    ids = {}
    for contact in value.get("contacts", []) or []:
        wa_id = contact.get("wa_id", "")
        user_id = contact.get("user_id", "")
        if wa_id and user_id:
            ids[wa_id] = user_id
    return ids


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


# Tipos que AGEM sobre uma mensagem que já existe, em vez de criar uma nova.
# ⚠️ Chegam na mesma lista das mensagens (e também na dos ECOS, quando é a
# própria clínica que apaga ou edita pelo celular): tratá-los como mensagem
# faz nascer um balão vazio, que foi o defeito visto em produção em 21/08.
KIND_POR_ACAO = {
    "revoke": WhatsAppEventKind.REVOKE,
    "edit": WhatsAppEventKind.EDIT,
    "system": WhatsAppEventKind.NUMBER_CHANGE,
}


def _kind_do_evento(message: dict, padrao: str) -> str:
    """O tipo do evento: a AÇÃO quando é uma, senão o padrão do laço."""
    return KIND_POR_ACAO.get(message.get("type", ""), padrao)


def _parse_message(
    message: dict, *, kind: str, wa_id: str, names: dict, user_ids: dict | None = None
) -> WhatsAppEvent:
    meta_type = message.get("type", "")
    message_kind = KIND_MAP.get(meta_type, MessageKind.UNSUPPORTED)

    body = caption = media_id = mime_type = filename = ""
    reaction_emoji = reaction_to = ""
    revoked_message_id = edited_message_id = new_wa_id = ""
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
    elif meta_type == "revoke":
        # ⚠️ A Meta DIZ qual mensagem foi apagada. Até 21/08/2026 este tipo não
        # estava no mapa, caía em `unsupported` e o identificador da original
        # ia para o lixo - a conversa ganhava um balão vazio no lugar de marcar
        # a mensagem certa.
        revoked_message_id = (message.get("revoke") or {}).get("original_message_id", "")
    elif meta_type == "edit":
        # O paciente corrigiu o que escreveu (só coexistência). O payload traz
        # a mensagem INTEIRA de novo, com o conteúdo atualizado: texto no
        # `text.body`, legenda dentro do bloco da mídia.
        edicao = message.get("edit") or {}
        edited_message_id = edicao.get("original_message_id", "")
        nova = edicao.get("message") or {}
        tipo_novo = nova.get("type", "")
        if tipo_novo == "text":
            body = (nova.get("text") or {}).get("body", "")
        elif tipo_novo in MEDIA_KINDS:
            caption = (nova.get(tipo_novo) or {}).get("caption", "")
    elif meta_type == "system":
        # Hoje o único `system.type` documentado é a troca de número.
        sistema = message.get("system") or {}
        if sistema.get("type") == "user_changed_number":
            new_wa_id = sistema.get("wa_id", "")
        content_data = {"system_type": sistema.get("type", "")}
    elif meta_type == "unsupported":
        # ⚠️ A Meta usa `unsupported` para coisas MUITO diferentes, e sem o
        # subtipo a tela dava a mesma frase genérica para todas. Os dois casos
        # reais que apareceram na clínica (achados nos webhooks arquivados):
        # `unknown`, que é a mensagem APAGADA pelo paciente, e `poll_creation`,
        # que é enquete.
        #
        # ⚠️ Ela NÃO diz qual mensagem foi apagada: não vem `context` nem o
        # wamid da original, então não há como marcar o balão certo. O aviso na
        # posição em que a Meta o entregou é tudo o que dá para fazer com
        # honestidade, e é o que o Chatwoot também faz.
        subtipo = (message.get("unsupported") or {}).get("type", "")
        content_data = {"unsupported_type": subtipo}

    # No inbound o identificador vem em `from_user_id`; no eco, em
    # `to_user_id`, porque ali quem manda é a clínica. O bloco `contacts[]` é o
    # terceiro caminho e o mais confiável, então ele desempata.
    do_bloco = (user_ids or {}).get(wa_id, "")
    user_id = (
        do_bloco
        or message.get("from_user_id", "")
        or message.get("to_user_id", "")
        or ""
    )

    return WhatsAppEvent(
        kind=kind,
        provider_message_id=message.get("id", ""),
        wa_id=wa_id,
        user_id=user_id,
        message_kind=message_kind,
        body=body,
        caption=caption,
        media_id=media_id,
        mime_type=mime_type,
        filename=filename,
        content_data=content_data,
        reaction_emoji=reaction_emoji,
        reaction_to=reaction_to,
        revoked_message_id=revoked_message_id,
        edited_message_id=edited_message_id,
        new_wa_id=new_wa_id,
        reply_to_provider_id=(message.get("context") or {}).get("id", ""),
        wa_timestamp=_ts(message.get("timestamp")),
        # Pelo telefone quando ele existe, pelo identificador quando não.
        contact_name=names.get(wa_id) or names.get(user_id, ""),
        raw=message,
    )


def _sincronizacao_de_contato(value: dict) -> WhatsAppEvent | None:
    """
    Contato da agenda do celular (RF-CON-5.3, campo `smb_app_state_sync`).

    ⚠️ Este evento NÃO segue o formato dos outros: não há lista de mensagens
    nem `contacts[]`, e os dados vêm soltos na raiz do `value`. Por isso ele é
    o único que precisa saber em que `field` está.
    """
    telefone = (value.get("contact_phone_number") or "").lstrip("+")
    if not telefone:
        return None
    nome = value.get("contact_name") or value.get("contact_first_name") or ""
    return WhatsAppEvent(
        kind=WhatsAppEventKind.CONTACT_SYNC,
        wa_id=telefone,
        contact_name=nome,
        sync_action=(value.get("action") or "").lower(),
        raw=value,
    )


def parse_meta_webhook(payload: dict) -> list[WhatsAppEvent]:
    events: list[WhatsAppEvent] = []
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            names = _names_by_wa_id(value)
            user_ids = _user_ids(value)

            # ⚠️ O nome do EVENTO e o nome do CAMPO são diferentes: o evento é
            # `smb_app_state_sync` e o conteúdo dele vem solto no `value`.
            if change.get("field") == "smb_app_state_sync":
                sincronia = _sincronizacao_de_contato(value)
                if sincronia is not None:
                    events.append(sincronia)
                continue

            for message in value.get("messages", []) or []:
                if message.get("type") in IGNORED_KINDS:
                    continue
                # ⚠️ Apagar NÃO é mandar mensagem: o evento chega na mesma
                # lista, mas o que ele pede é marcar uma mensagem que já
                # existe. Entrar como INBOUND o transformaria num balão novo,
                # que é o defeito que a clínica viu (21/08/2026).
                events.append(
                    _parse_message(
                        message,
                        kind=_kind_do_evento(message, WhatsAppEventKind.INBOUND),
                        wa_id=message.get("from", ""),
                        names=names,
                        user_ids=user_ids,
                    )
                )

            # ⚠️ O evento se chama `smb_message_echoes` e a chave do payload é
            # `message_echoes`. Procurar o nome do evento dentro do corpo não
            # acha nada (RF-CON-5). A chave é lida sem olhar o `field` porque
            # ela já é única no payload.
            for echo in value.get("message_echoes", []) or []:
                if echo.get("type") in IGNORED_KINDS:
                    continue
                # ⚠️ O eco também traz APAGAR e EDITAR: é a própria clínica
                # mexendo na conversa pelo app do celular. Sem passar pelo
                # mesmo mapa, cada apagada pelo aparelho virava um balão vazio
                # na tela da recepção (visto em produção, 21/08/2026).
                events.append(
                    _parse_message(
                        echo,
                        kind=_kind_do_evento(echo, WhatsAppEventKind.ECHO),
                        wa_id=echo.get("to", ""),
                        names=names,
                        user_ids=user_ids,
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
                        user_id=pref.get("user_id", ""),
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
                        user_id=status.get("recipient_user_id", ""),
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
