from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.core.api.viewsets import AuditLogViewSet

router = SimpleRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-logs")


urlpatterns = [
    path("", include(router.urls)),
]
