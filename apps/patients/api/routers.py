from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.patients.api.viewsets import (
    ClinicalEntryViewSet,
    PatientViewSet,
    PrescriptionModelViewSet,
    TagViewSet,
)

router = SimpleRouter()
router.register("patients", PatientViewSet, basename="patients")
router.register("tags", TagViewSet, basename="tags")
router.register("clinical-entries", ClinicalEntryViewSet, basename="clinical-entries")
router.register(
    "prescription-models", PrescriptionModelViewSet, basename="prescription-models"
)

urlpatterns = [
    path("", include(router.urls)),
]
