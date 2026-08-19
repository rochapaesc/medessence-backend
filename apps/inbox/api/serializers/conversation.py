from rest_framework.serializers import ModelSerializer, SerializerMethodField

from apps.inbox.api.serializers.label import ConversationLabelSummarySerializer
from apps.inbox.models import Conversation
from apps.patients.models import Contact


class ContactSummarySerializer(ModelSerializer):
    class Meta:
        model = Contact
        fields = ["id", "wa_id", "display_name"]


class ConversationSerializer(ModelSerializer):
    """Linha da lista de conversas (RF-INB-1) - enxuta, com flags de UI."""

    contact = ContactSummarySerializer(read_only=True)
    patient_name = SerializerMethodField()
    window_open = SerializerMethodField()
    assigned_to_name = SerializerMethodField()
    labels = ConversationLabelSummarySerializer(many=True, read_only=True)
    sequencias_segurando = SerializerMethodField()

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
            "status",
            "attended_by",
            "attended_since",
            "snoozed_until",
            "waiting_since",
            "resolved_at",
            "assigned_to",
            "assigned_to_name",
            "priority",
            "labels",
            "team",
            "sequencias_segurando",
        ]

    def get_patient_name(self, obj):
        return obj.patient.name if obj.patient_id else ""

    def get_window_open(self, obj):
        return obj.window_open

    def get_assigned_to_name(self, obj):
        return obj.assigned_to.get_full_name() if obj.assigned_to_id else ""

    def get_sequencias_segurando(self, obj):
        """
        Qual trilha está parada esperando esta conversa ficar livre (RF-SEQ-5.5).

        Prefere a anotação da listagem, e só vai ao banco quando ela não veio
        (objeto avulso, como o do `retrieve` e o do evento). A pergunta por
        linha é o que a anotação existe para evitar.
        """
        from apps.automation.sequence_holds import (
            ANOTACAO_DESDE,
            ANOTACAO_NOME,
            ANOTACAO_QUANTAS,
            espera_da_conversa,
            montar,
        )

        if hasattr(obj, ANOTACAO_QUANTAS):
            return montar(
                getattr(obj, ANOTACAO_QUANTAS),
                getattr(obj, ANOTACAO_NOME),
                getattr(obj, ANOTACAO_DESDE),
            )
        return espera_da_conversa(obj)
