from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.choices import SyncStatus
from apps.core.mixins import AuditMixin
from apps.scheduling.api.filtersets import AppointmentFilterset
from apps.scheduling.api.serializers import (
    AppointmentReadSerializer,
    AppointmentWriteSerializer,
)
from apps.scheduling.models import Appointment

# Campos cuja mudança vira UM update no EHR (remarcar/editar); mudança de
# `status` vira TRANSIÇÃO (ação semântica → rota do provedor no adapter).
SCHEDULE_FIELDS = (
    "starts_at",
    "duration_min",
    "practitioner_id",
    "procedure_id",
    "care_unit_id",
    "insurance_company_id",
    "insurance_plan_id",
    "remotely",
)


class AppointmentViewSet(AuditMixin, ClinicScopedModelViewSet):
    """Agenda escopada pela clínica ativa (RF-AGE-1/2) com write-through."""

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

    # ----------------- write-through p/ o EHR (§10.2) ----------------- #

    def _enqueue(self, appointment, payload: dict) -> None:
        from apps.integrations.push import enqueue_push

        operation = enqueue_push(self.clinic, "appointment", appointment.pk, payload)
        if operation is not None and appointment.sync_status != SyncStatus.PENDING:
            appointment.sync_status = SyncStatus.PENDING
            appointment.save(update_fields=["sync_status", "updated_at"])

    def perform_create(self, serializer):
        super().perform_create(serializer)
        self._enqueue(serializer.instance, {"op": "create"})

    def perform_update(self, serializer):
        old = Appointment.objects.get(pk=serializer.instance.pk)
        old_values = {field: getattr(old, field) for field in SCHEDULE_FIELDS}
        old_status = old.status

        super().perform_update(serializer)
        appointment = serializer.instance

        schedule_changed = any(
            getattr(appointment, field) != value for field, value in old_values.items()
        )
        if schedule_changed:
            self._enqueue(appointment, {"op": "update"})
        if appointment.status != old_status:
            self._enqueue(
                appointment,
                {"op": "transition", "target_status": appointment.status},
            )

    def perform_destroy(self, instance):
        external_id = instance.external_id
        super().perform_destroy(instance)  # soft delete + auditoria
        if external_id:
            from apps.integrations.push import enqueue_push

            enqueue_push(
                self.clinic,
                "appointment",
                instance.pk,
                {"op": "delete", "external_id": external_id},
            )
