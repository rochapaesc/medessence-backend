from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.scheduling.api.viewsets import (
    AppointmentViewSet,
    CareUnitViewSet,
    InsuranceCompanyViewSet,
    PractitionerViewSet,
    ProcedureViewSet,
)

router = SimpleRouter()
router.register("appointments", AppointmentViewSet, basename="appointments")
router.register("practitioners", PractitionerViewSet, basename="practitioners")
router.register("care-units", CareUnitViewSet, basename="care-units")
router.register("procedures", ProcedureViewSet, basename="procedures")
router.register("insurance-companies", InsuranceCompanyViewSet, basename="insurance-companies")

urlpatterns = [
    path("", include(router.urls)),
]
