from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db.models import Count
from django.db.models.functions import TruncDate
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api.guards import EHRDataGuardMixin
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


class AppointmentViewSet(EHRDataGuardMixin, AuditMixin, ClinicScopedModelViewSet):
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

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """
        Contagem agregada da agenda (RF-AGE-1): total, por dia e por status,
        com os MESMOS filtros da listagem (janela, profissional, unidade...).

        Existe para o calendário e os KPIs não precisarem baixar o mês inteiro:
        um mês cheio passava de 400 consultas e o front paginava de 100 em 100
        só para contar quantas caem em cada dia.
        """
        queryset = self.filter_queryset(self.get_queryset())

        # O dia é o do FUSO DA CLÍNICA: em UTC, uma consulta das 21h em
        # Fortaleza cairia no dia seguinte e o calendário divergiria da lista.
        try:
            tz = ZoneInfo(self.clinic.timezone or settings.TIME_ZONE)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo(settings.TIME_ZONE)

        # `order_by()` limpa a ordenação padrão: sem isso o `starts_at` entra
        # no GROUP BY e a contagem sai fragmentada (uma linha por consulta).
        by_day = {
            row["day"].isoformat(): row["total"]
            for row in queryset.order_by()
            .annotate(day=TruncDate("starts_at", tzinfo=tz))
            .values("day")
            .annotate(total=Count("id"))
            .order_by("day")
        }
        by_status = {
            row["status"]: row["total"]
            for row in queryset.order_by()
            .values("status")
            .annotate(total=Count("id"))
        }

        return Response(
            {
                "total": sum(by_day.values()),
                "by_day": by_day,
                "by_status": by_status,
            }
        )

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
