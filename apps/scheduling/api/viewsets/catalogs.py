from apps.core.api.viewsets import ClinicScopedReadOnlyViewSet
from apps.scheduling.api.serializers import (
    CareUnitSerializer,
    InsuranceCompanySerializer,
    PractitionerSerializer,
    ProcedureSerializer,
)
from apps.scheduling.models import CareUnit, InsuranceCompany, Practitioner, Procedure


class PractitionerViewSet(ClinicScopedReadOnlyViewSet):
    """Catálogo de profissionais — alimenta filtros e o form de agendamento."""

    model = Practitioner
    serializer_class = PractitionerSerializer
    ordering_fields = ["name"]

    def get_queryset(self):
        return super().get_queryset().order_by("name")


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
