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
