from rest_framework.serializers import IntegerField, ModelSerializer

from apps.inbox.models import ConversationLabel


class ConversationLabelSummarySerializer(ModelSerializer):
    """Etiqueta como ela aparece DENTRO da conversa: só o que a tela desenha."""

    class Meta:
        model = ConversationLabel
        fields = ["id", "name", "color"]


class ConversationLabelSerializer(ModelSerializer):
    """
    Catálogo da clínica (RF-ATD-9.1). `usage_count` não é enfeite: é o que
    deixa o gestor ver que criou uma etiqueta que ninguém usa, ou duas que
    dizem a mesma coisa.
    """

    usage_count = IntegerField(read_only=True)

    class Meta:
        model = ConversationLabel
        fields = ["id", "name", "color", "is_active", "usage_count"]
