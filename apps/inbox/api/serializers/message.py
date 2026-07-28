from rest_framework.serializers import (
    ModelSerializer,
    PrimaryKeyRelatedField,
    SerializerMethodField,
    ValidationError,
)

from apps.inbox.choices import MessageKind
from apps.inbox.models import Conversation, Message


class MessageSerializer(ModelSerializer):
    """Balão da thread (RF-INB-2) - leitura."""

    media_url = SerializerMethodField()

    class Meta:
        model = Message
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
            "reply_to_provider_id",
            "status",
            "status_error",
            "is_internal",
            "activity_type",
            "activity_data",
            "sender_kind",
            "sent_by",
            "template_name",
            "wa_timestamp",
        ]

    def get_media_url(self, obj):
        if obj.media_id and obj.media.stored_file:
            return obj.media.stored_file.url
        return ""


class MessageCreateSerializer(ModelSerializer):
    """
    Composer do atendente (RF-INB-2/3). Na Fatia A a mensagem é apenas
    PERSISTIDA como OUT/AGENT; o envio real (task `send_whatsapp_message`)
    entra na Fatia B.

    Regra da janela de 24h (RF-INB-3): fora dela, só template aprovado -
    texto livre é recusado com erro claro.
    """

    conversation = PrimaryKeyRelatedField(queryset=Conversation.objects.all())

    class Meta:
        model = Message
        fields = [
            "id",
            "conversation",
            "kind",
            "body",
            "caption",
            "template_name",
            "reply_to_provider_id",
            "is_internal",
        ]

    def validate(self, attrs):
        conversation = attrs["conversation"]
        kind = attrs.get("kind", MessageKind.TEXT)
        template_name = attrs.get("template_name", "")

        # Nota interna (RF-ATD-3) não passa pela regra da janela: ela não vai
        # para o WhatsApp. Barrá-la impediria a equipe de registrar contexto
        # justamente na conversa parada há dias — quando mais se precisa dele.
        if attrs.get("is_internal"):
            if not (attrs.get("body") or "").strip():
                raise ValidationError("A nota interna precisa de texto.")
            attrs["template_name"] = ""
            return attrs

        is_template = kind == MessageKind.TEXT and template_name
        if kind == MessageKind.TEXT and not template_name:
            if not conversation.window_open:
                raise ValidationError(
                    "A janela de 24h está fechada. Fora dela só é possível enviar um "
                    "template aprovado (informe `template_name`)."
                )
        if is_template:
            attrs["kind"] = MessageKind.TEMPLATE
        return attrs
