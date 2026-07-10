from rest_framework.serializers import ModelSerializer

from apps.inbox.models import QuickReply, WhatsAppTemplate


class QuickReplySerializer(ModelSerializer):
    class Meta:
        model = QuickReply
        fields = ["id", "label", "body"]


class WhatsAppTemplateSerializer(ModelSerializer):
    class Meta:
        model = WhatsAppTemplate
        fields = ["id", "name", "language", "category", "status", "components"]
