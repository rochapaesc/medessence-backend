from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin
from apps.scheduling.api.filtersets import AppointmentFilterset
from apps.scheduling.api.serializers import (
    AppointmentReadSerializer,
    AppointmentWriteSerializer,
)
from apps.scheduling.models import Appointment


class AppointmentViewSet(AuditMixin, ClinicScopedModelViewSet):
    """Agenda escopada pela clínica ativa (RF-AGE-1/2)."""

    model = Appointment
    audit_resource = "Appointment"
    filterset_class = AppointmentFilterset
    serializer_class = AppointmentReadSerializer
    select_related = ["patient", "practitioner", "care_unit", "procedure"]
    ordering_fields = ["starts_at", "created_at"]

    action_serializer_classes = {
        "list": AppointmentReadSerializer,
        "retrieve": AppointmentReadSerializer,
        "create": AppointmentWriteSerializer,
        "update": AppointmentWriteSerializer,
        "partial_update": AppointmentWriteSerializer,
    }

    def get_queryset(self):
        return super().get_queryset().order_by("starts_at")
