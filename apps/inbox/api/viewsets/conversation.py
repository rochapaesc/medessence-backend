from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.api.viewsets import ClinicScopedReadOnlyViewSet
from apps.core.mixins import AuditMixin
from apps.inbox.api.filtersets import ConversationFilterset
from apps.inbox.api.serializers import ConversationSerializer
from apps.inbox.models import Conversation


class ConversationViewSet(AuditMixin, ClinicScopedReadOnlyViewSet):
    """
    Lista/detalhe de conversas (RF-INB-1), ordenada por recência. As mutações
    do inbox são ações explícitas (o corpo da conversa é derivado das
    mensagens, não editável direto):

        POST /{id}/read/          zera as não lidas (RF-INB-4)
        POST /{id}/assign/        assume o atendimento (RF-INB-5/8)
        POST /{id}/mark-waiting/  marca como aguardando atendente (manual — F2)
        POST /{id}/link-patient/  desambigua o vínculo contato↔paciente (RF-INB-7)
        GET  /counters/           contadores do inbox (RNF-5)
    """

    model = Conversation
    audit_resource = "Conversation"
    serializer_class = ConversationSerializer
    filterset_class = ConversationFilterset
    ordering_fields = ["last_message_at", "unread_count"]
    select_related = ["contact", "patient", "channel", "assigned_to"]

    def get_queryset(self):
        return super().get_queryset().order_by("-last_message_at")

    @action(detail=True, methods=["post"], url_path="read")
    def read(self, request, pk=None):
        """Marca como lida localmente (RF-INB-4). O `messages/read` no provedor
        entra na Fatia B."""
        conversation = self.get_object()
        if conversation.unread_count:
            conversation.unread_count = 0
            conversation.save(update_fields=["unread_count", "updated_at"])
        # RF-INB-4: além de local, confirma a leitura no provedor.
        from apps.inbox.tasks import mark_whatsapp_read

        mark_whatsapp_read.delay(conversation.pk)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        """Assume o atendimento (RF-INB-5). Sem corpo → assume para si; gestor
        pode passar `assigned_to` para atribuir a outro atendente (RF-INB-8)."""
        conversation = self.get_object()
        assignee = request.user
        assigned_to_id = request.data.get("assigned_to")
        if assigned_to_id:
            assignee = self._resolve_clinic_user(assigned_to_id)
        conversation.assigned_to = assignee
        conversation.needs_agent = False
        conversation.save(update_fields=["assigned_to", "needs_agent", "updated_at"])
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="mark-waiting")
    def mark_waiting(self, request, pk=None):
        """Sinaliza que a conversa aguarda um atendente (aba 'aguardando').
        Na F2 é manual; a marcação automática por inbound+jornada entra na F3."""
        conversation = self.get_object()
        conversation.needs_agent = True
        conversation.save(update_fields=["needs_agent", "updated_at"])
        # Realtime (§12): aba "aguardando" acende em todas as telas da clínica.
        from apps.inbox.realtime import notify_handoff

        notify_handoff(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="link-patient")
    def link_patient(self, request, pk=None):
        """Vincula a conversa a um paciente (RF-INB-7) e garante o vínculo
        contato↔paciente em PatientContact."""
        from apps.patients.models import Patient, PatientContact

        conversation = self.get_object()
        patient_id = request.data.get("patient")
        if not patient_id:
            raise ValidationError({"patient": "Informe o paciente."})
        patient = Patient.objects.filter(clinic=self.clinic, pk=patient_id).first()
        if patient is None:
            raise ValidationError({"patient": "Paciente não encontrado nesta clínica."})

        conversation.patient = patient
        conversation.save(update_fields=["patient", "updated_at"])
        PatientContact.objects.get_or_create(patient=patient, contact=conversation.contact)
        return Response(self.get_serializer(conversation).data)

    @action(detail=False, methods=["get"], url_path="counters")
    def counters(self, request):
        """Contadores do inbox (RNF-5) — endpoint dedicado."""
        queryset = self.get_queryset()
        return Response(
            {
                "total": queryset.count(),
                "unread": queryset.filter(unread_count__gt=0).count(),
                "needs_agent": queryset.filter(needs_agent=True).count(),
                "unassigned": queryset.filter(assigned_to__isnull=True).count(),
            }
        )

    def _resolve_clinic_user(self, user_id):
        from apps.accounts.models import Membership

        membership = Membership.objects.filter(clinic=self.clinic, user_id=user_id).first()
        if membership is None:
            raise ValidationError({"assigned_to": "Usuário não pertence a esta clínica."})
        return membership.user
