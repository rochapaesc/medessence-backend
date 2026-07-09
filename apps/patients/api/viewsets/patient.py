from django.db.models import Prefetch
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api.permissions import IsClinicManager
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin, SoftDeleteMixin
from apps.patients.api.filtersets import PatientFilterset
from apps.patients.api.serializers import (
    PatientDetailSerializer,
    PatientReadSerializer,
    PatientWriteSerializer,
)
from apps.patients.models import Patient, PatientTag


class PatientViewSet(AuditMixin, SoftDeleteMixin, ClinicScopedModelViewSet):
    """
    CRM de pacientes (RF-PAC-1..7), escopado pela clínica ativa.
    Busca server-side (?search=) e filtros por tag/cidade/status/profissional.
    """

    model = Patient
    audit_resource = "Patient"
    filterset_class = PatientFilterset
    serializer_class = PatientReadSerializer
    restore_permission_classes = [IsClinicManager]
    ordering_fields = ["name", "last_appointment_at", "created_at"]

    action_serializer_classes = {
        "list": PatientReadSerializer,
        "retrieve": PatientDetailSerializer,
        "create": PatientWriteSerializer,
        "update": PatientWriteSerializer,
        "partial_update": PatientWriteSerializer,
    }

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .prefetch_related(
                Prefetch(
                    "patient_tags",
                    queryset=PatientTag.objects.select_related("tag"),
                )
            )
            .order_by("name")
        )

    @action(detail=False, methods=["get"], url_path="counters")
    def counters(self, request):
        """
        Contadores por status (RF-PAC-2) — endpoint dedicado (RNF-5).

        Sem parâmetros: janela da clínica, carteira inteira.
        Com ?practitioner=<id>: carteira do profissional (pacientes que já
        consultaram com ele), na janela efetiva dele.
        """
        from rest_framework.exceptions import ValidationError

        from apps.scheduling.models import Practitioner

        queryset = self.get_queryset()
        practitioner = None
        practitioner_id = request.query_params.get("practitioner")
        if practitioner_id:
            practitioner = Practitioner.objects.filter(
                clinic=self.clinic, pk=practitioner_id
            ).first()
            if practitioner is None:
                raise ValidationError({"practitioner": "Profissional não encontrado."})
            queryset = queryset.filter(appointments__practitioner=practitioner).distinct()
            window_days = practitioner.effective_active_window_days
        else:
            window_days = self.clinic.active_window_days

        return Response(queryset.status_counters(window_days, practitioner))
