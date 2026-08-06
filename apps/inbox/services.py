"""
Serviços do inbox:
  1. Denormalização da conversa a partir das mensagens (RF-INB-1) - signal.
  2. Ingestão de eventos do webhook (§7) - idempotente por wamid.
  3. Envio de mensagens OUT via provider (§7).

O provedor entra sempre pelo registry (`get_whatsapp_provider(channel)`) -
este módulo não conhece Datafy.
"""

import logging
from datetime import timedelta

from django.db.models import F
from django.utils import timezone

from apps.inbox.choices import (
    ActivityType,
    AttendedBy,
    ConversationStatus,
    MessageDirection,
    MessageKind,
    MessageStatus,
    SenderKind,
)
from apps.inbox.dispatch import inbound_ingested
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

    # Evento de atividade (RF-ATD-4) é METADADO da conversa, não conteúdo dela:
    # não vira prévia nem move a conversa na lista. Sem esta guarda, resolver um
    # atendimento trocava a última fala do paciente por "Evento" na listagem -
    # apagando justamente a informação que diz onde a conversa parou.
    if message.kind == MessageKind.ACTIVITY:
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
        # A MESMA guarda de retrocesso da prévia — e aqui ela é o relógio da
        # janela de 24h. A Meta REENTREGA webhook que falhou, às vezes um dia
        # depois (visto ao vivo em 29/07: a rajada pós-queda trouxe mensagem
        # da véspera e rebobinou o relógio, "fechando" a janela de uma conversa
        # que tinha acabado de receber mensagem). A mensagem velha entra na
        # thread e conta como não lida; só o relógio não volta no tempo.
        if (
            conversation.last_inbound_at is None
            or message.wa_timestamp >= conversation.last_inbound_at
        ):
            conversation.last_inbound_at = message.wa_timestamp
            fields.append("last_inbound_at")
        conversation.unread_count = F("unread_count") + 1
        fields.append("unread_count")

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


def _resolver_contato(clinic, event):
    """
    Contato pelo wa_id, com a autocura do nono dígito (§6.2): se o wa_id exato
    não existe mas a grafia alternativa (com/sem o 9) existe, o contato é
    RENOMEADO para a forma que a Meta usou — ela é dona do wa_id. Conversa,
    histórico e vínculos ficam intactos porque a FK não muda. É o que fecha o
    ciclo do outbound-first: criamos com 9 por palpite e a primeira resposta
    do paciente corrige o palpite sozinha.
    """
    from apps.patients.models import Contact
    from apps.patients.phone import grafia_alternativa

    contact = Contact.objects.filter(clinic=clinic, wa_id=event.wa_id).first()
    if contact is None:
        alternativa = grafia_alternativa(event.wa_id)
        if alternativa:
            contact = Contact.objects.filter(clinic=clinic, wa_id=alternativa).first()
            if contact is not None:
                contact.wa_id = event.wa_id
                contact.save(update_fields=["wa_id", "updated_at"])
    if contact is None:
        contact, _ = Contact.objects.get_or_create(
            clinic=clinic,
            wa_id=event.wa_id,
            defaults={"display_name": event.contact_name[:160]},
        )
    return contact


def _get_or_create_conversation(channel, event):
    from apps.patients.models import PatientContact

    contact = _resolver_contato(channel.clinic, event)
    conversation, created = channel.conversations.get_or_create(
        clinic=channel.clinic,
        channel=channel,
        contact=contact,
        # A conversa nasce AGUARDANDO, então nasce com o relógio da fila
        # correndo (RF-ATD-11): sem isto, o "aguardando há X" da situação mais
        # comum - conversa nova - ficaria em branco.
        defaults={"waiting_since": timezone.now()},
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


# --------------------------------------------------------------------- #
# Conversa iniciada pela clínica (RF-INB-11)
# --------------------------------------------------------------------- #


class ConversaSemDestino(ValueError):
    """Não há número para onde a conversa possa ir (ou ele não é celular)."""


def iniciar_conversa(clinic, user, *, patient=None, phone=None):
    """
    Get-or-create da conversa outbound-first (RF-INB-11). Devolve
    `(conversation, created)`.

    Resolução do número (RF-PAC-8): contato principal do paciente → telefone
    do cadastro → sem ambos, `ConversaSemDestino`. Criada, a conversa nasce
    ABERTA com quem clicou (regra do Chatwoot: open + assignee = criador) —
    nunca "Aguardando", que significa "o paciente espera por nós". Existente,
    NADA muda: navegar não rouba posse nem reabre Resolvida (RF-ATD-2 — quem
    reabre é a resposta do paciente).
    """
    from apps.inbox.attendance import log_activity
    from apps.inbox.models import Channel, Conversation
    from apps.inbox.realtime import notify_conversation_updated_on_commit
    from apps.patients.models import PatientContact
    from apps.patients.phone import canonizar_telefone, pode_ser_celular
    from apps.patients.vinculos import contato_do_numero, vincular

    channel = Channel.objects.filter(clinic=clinic).first()
    if channel is None:
        raise ConversaSemDestino("Esta clínica não tem canal de WhatsApp configurado.")

    contact = None
    if patient is not None:
        link = (
            PatientContact.objects.filter(patient=patient)
            .order_by("-is_primary", "pk")
            .select_related("contact")
            .first()
        )
        if link is not None:
            contact = link.contact

    if contact is None:
        numero = canonizar_telefone(phone if patient is None else patient.phone)
        if not numero:
            raise ConversaSemDestino(
                "Paciente sem telefone e sem contato vinculado."
                if patient is not None
                else "Informe um número de telefone."
            )
        if not pode_ser_celular(numero):
            raise ConversaSemDestino(
                "Este número não pode receber WhatsApp (telefone fixo)."
            )
        # Procura pelas duas grafias do nono dígito antes de criar (§6.2).
        contact, _ = contato_do_numero(
            clinic, numero, display_name=patient.name if patient is not None else ""
        )

    if patient is not None:
        # Registra o vínculo número↔paciente; o primeiro do número vira o
        # principal (regra única em `patients.vinculos`).
        vincular(patient, contact)

    conversation, created = Conversation.objects.get_or_create(
        clinic=clinic,
        channel=channel,
        contact=contact,
        defaults={
            "patient": patient,
            "status": ConversationStatus.OPEN,
            "attended_by": AttendedBy.AGENT,
            "assigned_to": user,
            "attended_since": timezone.now(),
        },
    )
    if created:
        log_activity(
            conversation,
            ActivityType.ASSIGNED,
            user=user,
            data={"from": AttendedBy.NONE, "by": "start"},
        )
        notify_conversation_updated_on_commit(conversation)
    return conversation, created


MOTIVO_CREDENCIAL = "Canal do WhatsApp desconectado. Avise o suporte para reconectar."


def registrar_saude_do_canal(channel, *, erro=None) -> None:
    """
    Ponto ÚNICO que decide se o canal está vivo.

    Chamado por todo caminho que fala com a Meta (envio e download). Erro de
    credencial conta; qualquer sucesso cura. Está aqui, e não espalhado, porque
    "o canal caiu" tem de significar a mesma coisa venha de onde vier — e
    porque a faixa da tela e o aviso ao gestor saem daqui, uma vez só.
    """
    from apps.inbox.realtime import notify_channel_health
    from apps.integrations.whatsapp.exceptions import WhatsAppAuthError

    if erro is None:
        if channel.reconectado():
            notify_channel_health(channel)
        return

    if not isinstance(erro, WhatsAppAuthError):
        # Rede caindo, 5xx, limite de taxa: nada disso é credencial morta.
        # Contar essas falhas derrubaria o canal por causa de instabilidade.
        return

    if channel.registrar_falha_de_auth(MOTIVO_CREDENCIAL):
        # Só na TRANSIÇÃO: sem isso, cada mensagem que tentasse sair depois
        # emitiria o mesmo aviso de novo.
        notify_channel_health(channel)
        logger.error(
            "Canal %s da clínica %s desconectado: credencial recusada pela Meta",
            channel.pk,
            channel.clinic_id,
        )


def _mensagem_por_wamid(clinic, wamid: str):
    """
    Acha a mensagem pelo wamid, com plano B por SUFIXO.

    O plano B veio do whatomate: o WhatsApp codifica o telefone dentro do
    prefixo do wamid, então o id que volta numa reação pode não bater
    caractere a caractere com o que gravamos — acontece na coexistência, com
    mensagem enviada pelo celular da clínica. O sufixo é a parte estável.
    """
    from apps.inbox.models import Message

    if not wamid:
        return None
    exata = Message.objects.filter(clinic=clinic, provider_message_id=wamid).first()
    if exata is not None:
        return exata
    sufixo = wamid.rsplit("=", 1)[-1][-24:]
    if len(sufixo) < 12:
        return None
    return (
        Message.objects.filter(clinic=clinic, provider_message_id__endswith=sufixo)
        .order_by("-id")
        .first()
    )


def _ingest_reaction(channel, event, sender_kind) -> bool:
    """
    Reação (👍 numa mensagem) COLA na mensagem alvo — não vira balão.

    Virava: o tipo `reaction` caía no `unsupported` e entrava na thread como
    "Não suportado", subindo a conversa na fila e pedindo resposta por causa
    de um joinha. Reação é um selo, não uma fala.

    Uma linha POR ATOR (desenho do wacrm): antes era um campo de texto na
    mensagem, então a clínica reagindo pelo celular apagava a reação do
    paciente. Emoji vazio APAGA a linha — é assim que a Meta manda o desfazer.
    """
    from apps.inbox.choices import ReactionActor, SenderKind
    from apps.inbox.models import MessageReaction

    alvo = _mensagem_por_wamid(channel.clinic, event.reaction_to)
    if alvo is None:
        # Reação a uma mensagem que não temos (anterior à integração): não há
        # onde colar o selo, e inventar um balão seria pior.
        return False

    # Echo = a própria clínica reagiu pelo app do celular. Não há usuário do
    # MedEssence por trás, mas o ator é a equipe, não o paciente.
    ator = ReactionActor.CONTACT if sender_kind == SenderKind.CONTACT else ReactionActor.AGENT

    if not event.reaction_emoji:
        # Apagar de VERDADE, e não o soft delete do projeto: a linha marcada
        # como deletada continuaria ocupando a chave única, e reagir de novo
        # na mesma mensagem estouraria IntegrityError. Selo desfeito não tem
        # valor de auditoria — o payload cru fica no WebhookEvent.
        apagadas = 0
        for reacao in MessageReaction.all_objects.filter(
            message=alvo, actor_kind=ator, actor_user=None
        ):
            reacao.hard_delete()
            apagadas += 1
        if not apagadas:
            return False
    else:
        # `all_objects` pelo mesmo motivo: se sobrou alguma linha soft-deletada
        # de uma versão anterior, ela é REAPROVEITADA em vez de colidir.
        MessageReaction.all_objects.update_or_create(
            message=alvo,
            actor_kind=ator,
            actor_user=None,
            defaults={
                "clinic": alvo.clinic,
                "conversation_id": alvo.conversation_id,
                "emoji": event.reaction_emoji,
                "deleted_at": None,
            },
        )

    from apps.inbox.realtime import notify_message_reaction

    notify_message_reaction(alvo)
    return True


def _ingest_message(channel, event, sender_kind) -> bool:
    """Upsert idempotente de uma mensagem IN (contato) ou echo (OUT do celular)."""
    from apps.inbox.models import MediaAsset, Message

    if event.reaction_to:
        return _ingest_reaction(channel, event, sender_kind)

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
            filename=event.filename,
        )

    message = Message.objects.create(
        clinic=channel.clinic,
        conversation=conversation,
        provider_message_id=event.provider_message_id,
        sender_kind=sender_kind,
        kind=event.message_kind or MessageKind.TEXT,
        body=event.body,
        caption=event.caption,
        content_data=event.content_data,
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

    # Avisa quem quiser reagir ao inbound - hoje é o motor de fluxos (F2.6).
    # É SINAL e não chamada direta de propósito: o inbox não pode passar a
    # depender do app de automação, ou os dois viram um só. Sai por último,
    # com a conversa já atualizada e a mídia já enfileirada.
    if sender_kind == SenderKind.CONTACT:
        inbound_ingested.send(
            sender=Message, conversation=conversation, message=message
        )
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

    note = Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=MessageKind.TEXT,
        sender_kind=SenderKind.AGENT,
        sent_by=user,
        body=body,
        is_internal=True,
        wa_timestamp=timezone.now(),
    )
    # A nota existe PARA a equipe: se ela só aparecesse para quem escreveu, a
    # colega com a mesma conversa aberta continuaria sem o aviso - que é o
    # motivo de a nota existir. Nenhuma task é enfileirada (ela não sai), então
    # o aviso ao vivo tem de partir daqui.
    from apps.inbox.realtime import notify_message_new_on_commit

    notify_message_new_on_commit(note)
    return note


def _template_language(message) -> str:
    """
    O idioma vem do template SINCRONIZADO, não de constante. Achado ao vivo no
    fechamento (28/07): o `hello_world` só existe em `en_US`, e o envio cravado
    em `pt_BR` morria com 132001 "template não existe" - o código do erro nem
    menciona idioma, o que faria alguém procurar o nome por horas.

    Com o mesmo nome em vários idiomas, `pt_BR` ganha: é o idioma da clínica.
    """
    from apps.inbox.models import WhatsAppTemplate

    idiomas = list(
        WhatsAppTemplate.objects.filter(
            clinic_id=message.clinic_id, name=message.template_name
        ).values_list("language", flat=True)
    )
    if "pt_BR" in idiomas:
        return "pt_BR"
    return idiomas[0] if idiomas else "pt_BR"


# Quanto tempo uma mensagem falha continua reenviável. É o mesmo teto do
# Chatwoot (`canRetry = !hasOneDayPassed(createdAt)`) e não é arbitrário: passa
# de 24h, a janela da Meta fechou e o reenvio produziria a MESMA falha — o
# botão só existiria para frustrar quem clica.
JANELA_DE_REENVIO = timedelta(hours=24)


def assinar_mensagem(message, user) -> bool:
    """
    Põe o nome de quem escreveu no COMEÇO da mensagem, em negrito:

        *Gabriel Rocha:*
        Bom dia, como posso ajudar

    Do outro lado é um número só — o da clínica. Sem a assinatura, o paciente
    não sabe se está falando com a Ana da recepção, com a doutora ou com um
    robô, e responde "quem é?" antes de responder a pergunta.

    O texto é gravado JÁ ASSINADO, e não assinado só na hora de enviar: assim
    a thread mostra exatamente o que chegou no celular do paciente. Guardar um
    corpo e mandar outro criaria duas versões da mesma fala.

    Não assina: nota da equipe (não sai daqui), template (o conteúdo é o
    aprovado pela Meta e mexer nele reprova o envio), mensagem da automação
    (não tem pessoa por trás) e legenda de anexo (o nome dominaria a linha
    curta que descreve a foto).

    Devolve `True` quando assinou.
    """
    if message.is_internal or message.template_name:
        return False
    if message.kind != MessageKind.TEXT or not message.body.strip():
        return False
    if message.sender_kind != SenderKind.AGENT:
        return False

    nome = (user.get_full_name() or "").strip()
    if not nome:
        return False

    # Dois pontos DENTRO do negrito: separa o nome da fala sem virar
    # uma segunda linha de enfeite.
    assinatura = f"*{nome}:*"
    # Reenvio e eco não assinam de novo: a linha já está lá.
    if message.body.startswith(assinatura):
        return False

    message.body = f"{assinatura}\n{message.body}"
    message.save(update_fields=["body", "updated_at"])
    return True


def recalcular_ultima_mensagem(conversation) -> None:
    """
    Refaz a prévia da fila depois que uma mensagem sumiu.

    Sem isto, apagar a última nota deixava a lista mostrando um texto que já
    não existe — e a recepção clicaria na conversa procurando algo que sumiu.
    """
    from apps.inbox.models import Message

    ultima = (
        Message.objects.filter(conversation=conversation)
        .exclude(kind=MessageKind.ACTIVITY)
        .order_by("-wa_timestamp", "-id")
        .first()
    )
    conversation.last_message_at = ultima.wa_timestamp if ultima else None
    conversation.last_message_preview = _preview(ultima) if ultima else ""
    conversation.save(
        update_fields=["last_message_at", "last_message_preview", "updated_at"]
    )


def reagir(message, user, emoji: str) -> None:
    """
    O selo da EQUIPE numa mensagem, com dono (Bloco D).

    Espelho do `_ingest_reaction`, que faz o mesmo para o que CHEGA. Emoji
    vazio apaga a linha de VERDADE (não o soft delete): a linha marcada como
    deletada continuaria ocupando a chave única, e reagir de novo na mesma
    mensagem estouraria IntegrityError.

    A reação vai para o WhatsApp em task — o paciente tem de ver o 👍 no
    celular dele, senão é um selo que só existe na nossa tela.
    """
    from apps.inbox.choices import ReactionActor
    from apps.inbox.models import MessageReaction
    from apps.inbox.realtime import notify_message_reaction
    from apps.inbox.tasks import send_whatsapp_reaction

    if emoji:
        MessageReaction.all_objects.update_or_create(
            message=message,
            actor_kind=ReactionActor.AGENT,
            actor_user=user,
            defaults={
                "clinic": message.clinic,
                "conversation_id": message.conversation_id,
                "emoji": emoji,
                "deleted_at": None,
            },
        )
    else:
        for reacao in MessageReaction.all_objects.filter(
            message=message, actor_kind=ReactionActor.AGENT, actor_user=user
        ):
            reacao.hard_delete()

    notify_message_reaction(message)
    send_whatsapp_reaction.delay(message.pk, emoji)


def mensagens_para_reenviar(clinic):
    """
    O que não saiu e ainda dá para tentar de novo.

    Nota interna fica de fora porque ela nunca tentou sair; mensagem de mais
    de 24h também, pelo motivo acima.
    """
    from apps.inbox.choices import ConversationStatus
    from apps.inbox.models import Message

    return Message.objects.filter(
        clinic=clinic,
        direction=MessageDirection.OUT,
        status=MessageStatus.FAILED,
        is_internal=False,
        wa_timestamp__gte=timezone.now() - JANELA_DE_REENVIO,
    ).exclude(
        # Atendimento ENCERRADO não pede reenvio: quem resolveu decidiu que o
        # assunto acabou, e a faixa cobrando "3 mensagens não saíram" mandaria
        # a recepção reabrir conversa fechada para mandar algo vencido.
        conversation__status=ConversationStatus.RESOLVED
    )


def pode_reenviar(message) -> bool:
    """
    Reenviar é útil? Template passa sempre (é o que existe para fora da
    janela); o resto depende da janela da conversa estar aberta.
    """
    if message.kind == MessageKind.TEMPLATE and message.template_name:
        return True
    return message.conversation.window_open


def reenviar(message) -> bool:
    """
    Devolve a mensagem para a fila de envio. `False` quando não adianta.

    Nenhum dos três repositórios de referência reenvia sozinho ao reconectar —
    Chatwoot põe o botão no balão, whatomate só faz lote em campanha. O motivo
    é bom: mensagem que ficou presa meia hora pode ter virado assunto vencido,
    e quem decide isso é quem atende, não o sistema.
    """
    if not pode_reenviar(message):
        return False

    message.status = ""
    message.status_error = ""
    # A mensagem passa a valer AGORA, não na hora em que foi escrita.
    #
    # No celular do paciente ela chega neste instante; se a thread a mantivesse
    # às 20:22 de ontem, a conversa teria um buraco: uma fala antiga no meio do
    # passado que o outro lado só leu hoje. O relógio do balão tem de ser o do
    # envio de verdade — é o que os dois lados veem igual.
    message.wa_timestamp = timezone.now()
    message.save(
        update_fields=["status", "status_error", "wa_timestamp", "updated_at"]
    )

    # A conversa sobe na fila e ganha a prévia nova: a mensagem que acabou de
    # sair é a última do fio, e a lista tem de refletir isso.
    apply_message_to_conversation(message, created=True)

    from apps.inbox.realtime import notify_conversation_updated_on_commit
    from apps.inbox.tasks import send_whatsapp_message

    notify_conversation_updated_on_commit(message.conversation)
    send_whatsapp_message.delay(message.pk)
    return True


def _enviar_anexo(provider, to: str, message):
    """
    O anexo pelo CAMINHO no disco — o PyWa sobe o arquivo e usa o media id.

    A alternativa seria mandar a URL do nosso storage, o que exigiria que a
    mídia da clínica fosse pública na internet para a Meta buscar. Não é: laudo
    e exame não podem ficar acessíveis a quem tiver o link.
    """
    media = message.media
    try:
        arquivo = media.stored_file.path
    except NotImplementedError:
        # Storage remoto (S3 e afins) não tem caminho local. O PyWa aceita os
        # bytes direto — mais caro, mas continua funcionando sem tornar a
        # mídia da clínica pública para a Meta buscar por URL.
        with media.stored_file.open("rb") as f:
            arquivo = f.read()

    return provider.send_media(
        to,
        message.kind,
        arquivo,
        message.caption or None,
        filename=media.filename or None,
        mime_type=media.mime_type or None,
        reply_to=message.reply_to_provider_id or None,
        # Áudio com onda calculada é gravação da recepção: vai como nota de
        # voz. Áudio anexado de um arquivo vai como áudio comum — que é o que
        # ele é, e o que o paciente espera ver.
        is_voice=bool(media.waveform),
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
            result = provider.send_template(
                to, message.template_name, _template_language(message)
            )
        elif message.media_id:
            result = _enviar_anexo(provider, to, message)
        elif message.content_data.get("buttons"):
            # Interativo (F2.6): quem monta é o motor de fluxos, e os ids dos
            # botões voltam no webhook como `interactive_id` - é por eles que
            # o motor sabe qual caminho o paciente escolheu.
            result = provider.send_buttons(to, message.body, message.content_data["buttons"])
        elif message.content_data.get("list"):
            lista = message.content_data["list"]
            result = provider.send_list(
                to,
                message.body,
                lista.get("button_label") or "Ver opções",
                lista.get("sections") or [],
            )
        else:
            result = provider.send_text(
                to, message.body, message.reply_to_provider_id or None
            )
    except (WhatsAppRateLimitedError, WhatsAppUnavailableError):
        # Transitórios: sobem para o autoretry da task - a mensagem continua
        # pendente e a fila tenta de novo.
        raise
    except WhatsAppError as exc:
        registrar_saude_do_canal(conversation.channel, erro=exc)
        # Erro de negócio (janela fechada, número inválido...): retry não
        # resolve. A mensagem morre FAILED **com o motivo** - antes desta
        # captura ela ficava pendente para sempre, sem explicação nenhuma.
        message.status = MessageStatus.FAILED
        # Credencial morta tem frase HUMANA; o resto mantém o motivo da Meta,
        # que é o que diz se adianta tentar de novo.
        from apps.integrations.whatsapp.exceptions import WhatsAppAuthError

        message.status_error = (
            MOTIVO_CREDENCIAL if isinstance(exc, WhatsAppAuthError) else str(exc)
        )
        message.save(update_fields=["status", "status_error", "updated_at"])
        # Avisa a tela pelo ID da mensagem — ela nunca chegou a ter wamid.
        # Antes daqui o balão ficava eterno em "enviando", e a recepção só
        # descobria que não saiu quando o paciente não respondia (queda de
        # 29/07). Com o evento, o balão vira vermelho com o botão Reenviar.
        from apps.inbox.realtime import notify_message_status

        notify_message_status(
            message.clinic_id,
            "",
            MessageStatus.FAILED,
            conversation.pk,
            message_id=message.pk,
            error=message.status_error,
        )
        return

    # Deu certo: o canal está vivo (o `reauthorized!` do Chatwoot).
    registrar_saude_do_canal(conversation.channel)

    message.provider_message_id = result.provider_message_id
    message.status = result.status or MessageStatus.SENT
    message.save(update_fields=["provider_message_id", "status", "updated_at"])

    # Realtime (§12): telas dos demais atendentes veem a mensagem enviada.
    from apps.inbox.realtime import notify_message_new

    notify_message_new(message)
