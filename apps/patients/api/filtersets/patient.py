from django.db.models import Q
from django_filters.rest_framework import (
    CharFilter,
    ChoiceFilter,
    FilterSet,
    NumberFilter,
)

from apps.patients.choices import PatientStatus
from apps.patients.models import Patient


class PatientFilterset(FilterSet):
    """Filtros do CRM (RF-PAC-1): busca server-side, tag, cidade, status e profissional."""

    search = CharFilter(method="filter_search")
    tag = NumberFilter(method="filter_tag")
    city = CharFilter(lookup_expr="iexact")
    status = ChoiceFilter(choices=PatientStatus.choices, method="filter_status")
    practitioner = NumberFilter(method="filter_practitioner")

    class Meta:
        model = Patient
        fields = ["search", "tag", "city", "status", "practitioner", "source", "state"]

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value) | Q(cpf__icontains=value) | Q(phone__icontains=value)
        )

    def filter_tag(self, queryset, name, value):
        return queryset.filter(
            patient_tags__tag_id=value,
            patient_tags__deleted_at__isnull=True,
        ).distinct()

    def filter_status(self, queryset, name, value):
        """
        Janela configurável (RF-PAC-2): padrão da clínica; com ?practitioner=
        junto, usa a janela efetiva DELE e a atividade relativa à carteira.
        """
        window_days, practitioner = self._resolve_window()
        return queryset.by_status(value, window_days=window_days, practitioner=practitioner)

    def filter_practitioner(self, queryset, name, value):
        return queryset.filter(appointments__practitioner_id=value).distinct()

    def _resolve_window(self):
        from apps.core.context import resolve_active_membership
        from apps.scheduling.models import Practitioner

        clinic = resolve_active_membership(self.request).clinic
        practitioner = None
        practitioner_id = self.data.get("practitioner")
        if practitioner_id:
            practitioner = Practitioner.objects.filter(clinic=clinic, pk=practitioner_id).first()
        if practitioner is not None:
            return practitioner.effective_active_window_days, practitioner
        return clinic.active_window_days, None
