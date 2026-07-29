from rest_framework.serializers import (
    ListSerializer,
    ModelSerializer,
    PrimaryKeyRelatedField,
    SerializerMethodField,
    ValidationError,
)

from apps.inbox import media_rules
from apps.inbox.choices import MediaState, MessageKind
from apps.inbox.models import Conversation, MediaAsset, Message


def media_payload(media, request=None) -> dict | None:
    """
    A mídia como a tela precisa dela: URL que abre em qualquer lugar, tipo,
    nome, tamanho, duração, onda e o ESTADO do download.

    Vive numa função solta (e não só no serializer) porque o evento de tempo
    real manda exatamente o mesmo objeto — dois formatos para a mesma mídia
    fariam o balão mudar de forma quando o socket chegasse antes do REST.
    """
    if media is None:
        return None
    url = media.stored_file.url if media.stored_file else ""
    # URL ABSOLUTA: relativa funciona no localhost e quebra em qualquer outro
    # lugar — o front pode estar em outra origem que a API.
    if url and request is not None:
        url = request.build_absolute_uri(url)
    return {
        "id": media.pk,
        "url": url,
        "mime_type": media.mime_type,
        "filename": media.filename,
        "size_bytes": media.size_bytes,
        "duration_ms": media.duration_ms,
        "waveform": media.waveform or [],
        "state": media.state,
        "error": media.error,
    }


def citacao_payload(message) -> dict:
    """A mensagem citada, do tamanho de um chip. O balão mostra de quem é e
    um pedaço do texto — nunca a mensagem inteira, que roubaria a leitura da
    resposta."""
    return {
        "id": message.pk,
        "kind": message.kind,
        "sender_kind": message.sender_kind,
        "preview": (message.body or message.caption or message.get_kind_display())[:120],
    }


class MessageListSerializer(ListSerializer):
    """
    Resolve TODAS as citações da página numa consulta só.

    A citação viaja por wamid (é o que a Meta usa), então cada balão que
    responde a outro precisaria de um `filter(provider_message_id=...)`. Numa
    thread onde metade das mensagens é resposta, isso é meia página de
    consultas para desenhar um chip.
    """

    def to_representation(self, data):
        mensagens = list(data)
        wamids = {m.reply_to_provider_id for m in mensagens if m.reply_to_provider_id}
        if wamids:
            self.child.context["citadas"] = {
                m.provider_message_id: m
                for m in Message.objects.filter(
                    # Escopo pela clínica da própria página: wamid é da Meta, e
                    # buscar sem filtro deixaria a porta aberta para citar
                    # mensagem de outro tenant.
                    clinic_id=mensagens[0].clinic_id,
                    provider_message_id__in=wamids,
                )
            }
        return super().to_representation(mensagens)


class MessageSerializer(ModelSerializer):
    """Balão da thread (RF-INB-2) - leitura."""

    media_url = SerializerMethodField()
    media_asset = SerializerMethodField()
    reactions = SerializerMethodField()
    reply_to = SerializerMethodField()
    # Quem agiu, por extenso: a frase do evento ("Ana assumiu o atendimento")
    # e a assinatura da nota interna são montadas no front, e um id de usuário
    # obrigaria a tela a buscar o nome mensagem a mensagem.
    sent_by_name = SerializerMethodField()

    class Meta:
        model = Message
        list_serializer_class = MessageListSerializer
        fields = [
            "id",
            "conversation",
            "provider_message_id",
            "direction",
            "kind",
            "body",
            "caption",
            "media",
            "media_url",
            "media_asset",
            "reply_to_provider_id",
            "reply_to",
            "reactions",
            "content_data",
            "status",
            "status_error",
            "is_internal",
            "activity_type",
            "activity_data",
            "sender_kind",
            "sent_by",
            "sent_by_name",
            "template_name",
            "edited_at",
            "wa_timestamp",
        ]

    def get_media_url(self, obj):
        """Mantido por compatibilidade com quem já lia este campo; o que a
        tela usa agora é `media_asset`, que sabe também o estado e o nome."""
        payload = self.get_media_asset(obj)
        return payload["url"] if payload else ""

    def get_media_asset(self, obj):
        if not obj.media_id:
            return None
        return media_payload(obj.media, self.context.get("request"))

    def get_reactions(self, obj):
        from apps.inbox.realtime import _reactions_min

        return _reactions_min(obj)

    def get_reply_to(self, obj):
        """
        A mensagem citada. `None` quando ela não está no nosso banco — resposta
        a algo anterior à integração, que o WhatsApp mostra e nós não temos.
        Nesse caso o balão desenha só a resposta, sem chip: melhor do que um
        chip vazio dizendo que existe algo que não dá para mostrar.
        """
        if not obj.reply_to_provider_id:
            return None
        citadas = self.context.get("citadas")
        if citadas is not None:
            citada = citadas.get(obj.reply_to_provider_id)
        else:
            citada = Message.objects.filter(
                clinic_id=obj.clinic_id, provider_message_id=obj.reply_to_provider_id
            ).first()
        return citacao_payload(citada) if citada else None

    def get_sent_by_name(self, obj):
        if not obj.sent_by_id:
            return ""
        return obj.sent_by.get_full_name() or obj.sent_by.email


class MessageEditSerializer(ModelSerializer):
    """
    Reescrita de NOTA INTERNA — só o texto, e só ela.

    Mensagem enviada não entra aqui, e não é limitação de código: a Cloud API
    não edita mensagem já entregue. O paciente já leu o original; mudar o texto
    do nosso lado faria a thread contar uma história que o celular dele
    desmente. Nenhum dos três repositórios de referência edita mensagem.
    """

    class Meta:
        model = Message
        fields = ["id", "body"]

    def validate_body(self, value):
        if not value.strip():
            raise ValidationError("A nota precisa de texto.")
        return value.strip()


class MessageCreateSerializer(ModelSerializer):
    """
    Composer do atendente (RF-INB-2/3). Na Fatia A a mensagem é apenas
    PERSISTIDA como OUT/AGENT; o envio real (task `send_whatsapp_message`)
    entra na Fatia B.

    Regra da janela de 24h (RF-INB-3): fora dela, só template aprovado -
    texto livre é recusado com erro claro.
    """

    conversation = PrimaryKeyRelatedField(queryset=Conversation.objects.all())
    media = PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = Message
        fields = [
            "id",
            "conversation",
            "kind",
            "body",
            "caption",
            "media",
            "template_name",
            "reply_to_provider_id",
            "is_internal",
        ]

    def validate(self, attrs):
        conversation = attrs["conversation"]
        kind = attrs.get("kind", MessageKind.TEXT)
        template_name = attrs.get("template_name", "")
        media = attrs.get("media")

        # Nota interna (RF-ATD-3) não passa pela regra da janela: ela não vai
        # para o WhatsApp. Barrá-la impediria a equipe de registrar contexto
        # justamente na conversa parada há dias — quando mais se precisa dele.
        if attrs.get("is_internal"):
            if not (attrs.get("body") or "").strip():
                raise ValidationError("A nota interna precisa de texto.")
            attrs["template_name"] = ""
            attrs["media"] = None
            return attrs

        if media is not None:
            attrs["kind"] = kind = self._validar_anexo(media)
            attrs["template_name"] = ""
        elif kind in media_rules.ENVIAVEIS:
            raise ValidationError("Anexe um arquivo antes de enviar.")

        is_template = kind == MessageKind.TEXT and template_name
        if kind == MessageKind.TEXT and not template_name and not media:
            if not (attrs.get("body") or "").strip():
                raise ValidationError("A mensagem precisa de texto.")

        # A janela de 24h fecha para TUDO que vai ao WhatsApp, anexo inclusive
        # (RF-INB-3): fora dela só passa template aprovado.
        if kind != MessageKind.TEMPLATE and not template_name:
            if not conversation.window_open:
                raise ValidationError(
                    "A janela de 24h está fechada. Fora dela só é possível enviar um "
                    "template aprovado (informe `template_name`)."
                )
        if is_template:
            attrs["kind"] = MessageKind.TEMPLATE
        return attrs

    def _validar_anexo(self, media) -> str:
        """
        O anexo é usável, e é de quem diz ser.

        A mídia RECEBIDA do paciente também é um `MediaAsset` da clínica — sem
        esta guarda, mandar o id de uma mídia de outra conversa reencaminharia
        o exame de um paciente para outro. O que se pode anexar é o que ainda
        não tem mensagem: o upload recém-feito.
        """
        clinic = self.context.get("clinic")
        if clinic is not None and media.clinic_id != clinic.pk:
            raise ValidationError("Anexo não encontrado.")
        if media.state != MediaState.READY or not media.stored_file:
            raise ValidationError("O anexo ainda não está pronto.")
        if Message.all_objects.filter(media=media).exists():
            raise ValidationError("Este anexo já foi enviado.")
        kind = media_rules.tipo_do_arquivo(media.mime_type)
        try:
            media_rules.validar(kind, media.mime_type, media.size_bytes or 0)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return kind
