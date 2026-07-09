from rest_framework.serializers import (
    ModelSerializer,
    SerializerMethodField,
)

from apps.inbox.models import Conversation
from apps.patients.models import Contact


class ContactSummarySerializer(ModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "wa_id", "display_name"]


class ConversationSerializer(ModelSerializer):
    """Linha da lista de conversas (RF-INB-1) — enxuta, com flags de UI."""

    contact = ContactSummarySerializer(read_only=True)
    patient_name = SerializerMethodField()
    window_open = SerializerMethodField()
    assigned_to_name = SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "channel",
            "contact",
            "patient",
            "patient_name",
            "last_message_preview",
            "last_message_at",
            "last_inbound_at",
            "window_open",
            "unread_count",
            "needs_agent",
            "assigned_to",
            "assigned_to_name",
        ]

    def get_patient_name(self, obj):
        return obj.patient.name if obj.patient_id else ""

    def get_window_open(self, obj):
        return obj.window_open

    def get_assigned_to_name(self, obj):
        return obj.assigned_to.get_full_name() if obj.assigned_to_id else ""
