from django_filters.rest_framework import (
    CharFilter,
    DateTimeFromToRangeFilter,
    FilterSet,
    NumberFilter,
)

from apps.scheduling.models import Appointment


class AppointmentFilterset(FilterSet):
    """
    Agenda (RF-AGE-1): visão mensal/diária via intervalo
    (?starts_at_after=...&starts_at_before=...), filtrável por profissional
    e unidade. `status` aceita lista: ?status=scheduled,confirmed
    """

    starts_at = DateTimeFromToRangeFilter()
    practitioner = NumberFilter(field_name="practitioner_id")
    care_unit = NumberFilter(field_name="care_unit_id")
    patient = NumberFilter(field_name="patient_id")
    status = CharFilter(method="filter_status")

    class Meta:
        model = Appointment
        fields = ["starts_at", "practitioner", "care_unit", "patient", "status", "remotely"]

    def filter_status(self, queryset, name, value):
        statuses = [s.strip() for s in value.split(",") if s.strip()]
        return queryset.filter(status__in=statuses) if statuses else queryset
