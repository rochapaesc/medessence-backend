from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.patients.api.viewsets import PatientViewSet, TagViewSet

router = SimpleRouter()
router.register("patients", PatientViewSet, basename="patients")
router.register("tags", TagViewSet, basename="tags")

urlpatterns = [
    path("", include(router.urls)),
]
