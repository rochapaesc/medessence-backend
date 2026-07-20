from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api.viewsets import ClinicScopedReadOnlyViewSet
from apps.scheduling.api.serializers import (
    CareUnitSerializer,
    InsuranceCompanySerializer,
    PractitionerProcedureSerializer,
    PractitionerSerializer,
    ProcedureSerializer,
)
from apps.scheduling.models import (
    CareUnit,
    InsuranceCompany,
    Practitioner,
    PractitionerProcedure,
    Procedure,
)


class PractitionerViewSet(ClinicScopedReadOnlyViewSet):
    """Catálogo de profissionais - alimenta filtros e o form de agendamento."""

    model = Practitioner
    serializer_class = PractitionerSerializer
    ordering_fields = ["name"]

    def get_queryset(self):
        return super().get_queryset().order_by("name")

    @action(detail=True, methods=["get"], url_path="procedures")
    def procedures(self, request, pk=None):
        """Procedimentos que o profissional OFERECE (duração/preço do form)."""
        practitioner = self.get_object()
        offers = (
            PractitionerProcedure.objects.filter(
                clinic=self.clinic, practitioner=practitioner, is_active=True
            )
            .select_related("procedure")
            .order_by("procedure__name")
        )
        return Response(PractitionerProcedureSerializer(offers, many=True).data)

    @action(detail=True, methods=["get"], url_path="availability")
    def availability(self, request, pk=None):
        """
        Horários livres do profissional num dia (form Nova consulta).
        `?date=YYYY-MM-DD&procedure=<pk>&care_unit=<pk>`. Com EHR usa o
        GetAvailability real; sem EHR deriva do work_journey da unidade
        menos as consultas locais do dia.
        """
        from datetime import date as date_cls
        from datetime import timedelta

        from rest_framework.exceptions import ValidationError

        practitioner = self.get_object()
        try:
            day = date_cls.fromisoformat(request.query_params.get("date") or "")
        except ValueError:
            raise ValidationError({"date": "Use o formato YYYY-MM-DD."}) from None

        procedure = _by_pk(Procedure, self.clinic, request.query_params.get("procedure"))
        care_unit = _by_pk(CareUnit, self.clinic, request.query_params.get("care_unit"))

        if self.clinic.ehr_provider and practitioner.external_id:
            from apps.integrations.ehr.registry import get_ehr_provider

            result = get_ehr_provider(self.clinic).get_availability(
                practitioner.external_id,
                procedure.external_id if procedure else "",
                care_unit.external_id if care_unit else "",
                day.isoformat(),
            )
            return Response(
                {
                    "date": result.date,
                    "has_availability": result.has_availability,
                    "times": result.times,
                    "source": "ehr",
                }
            )

        # Standalone: janelas do work_journey da unidade no dia, em passos da
        # duração do procedimento, menos as consultas locais do profissional.
        duration = timedelta(minutes=(procedure.duration_min if procedure else None) or 30)
        windows = []
        for window in (care_unit.work_journey if care_unit else []) or []:
            if window.get("available") is False:
                continue
            start = _parse_iso(window.get("startDate"))
            end = _parse_iso(window.get("endDate"))
            if start and end and start.date() == day:
                windows.append((start, end))

        from apps.scheduling.models import Appointment

        busy = [
            (a.starts_at, a.starts_at + timedelta(minutes=a.duration_min))
            for a in Appointment.objects.filter(
                clinic=self.clinic,
                practitioner=practitioner,
                starts_at__date=day,
            ).exclude(status__in=["canceled", "no_show"])
        ]

        times = []
        for start, end in windows:
            cursor = start
            while cursor + duration <= end:
                slot_end = cursor + duration
                if not any(b_start < slot_end and cursor < b_end for b_start, b_end in busy):
                    times.append(cursor.strftime("%H:%M"))
                cursor = slot_end

        return Response(
            {
                "date": day.isoformat(),
                "has_availability": bool(times),
                "times": times,
                "source": "local",
            }
        )


def _by_pk(model, clinic, raw_pk):
    if not raw_pk:
        return None
    return model.objects.filter(clinic=clinic, pk=raw_pk).first()


def _parse_iso(value):
    from datetime import datetime

    from django.utils import timezone as dj_timezone

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dj_timezone.is_naive(parsed):
        parsed = dj_timezone.make_aware(parsed)
    return dj_timezone.localtime(parsed)


class CareUnitViewSet(ClinicScopedReadOnlyViewSet):
    model = CareUnit
    serializer_class = CareUnitSerializer

    def get_queryset(self):
        return super().get_queryset().order_by("name")


class ProcedureViewSet(ClinicScopedReadOnlyViewSet):
    model = Procedure
    serializer_class = ProcedureSerializer

    def get_queryset(self):
        return super().get_queryset().order_by("name")


class InsuranceCompanyViewSet(ClinicScopedReadOnlyViewSet):
    model = InsuranceCompany
    serializer_class = InsuranceCompanySerializer

    def get_queryset(self):
        return super().get_queryset().prefetch_related("plans").order_by("name")
