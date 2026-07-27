from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.core.api.viewsets import AuditLogViewSet, MyAccessLogViewSet

router = SimpleRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-logs")
# "Meus acessos" (§15.2) - rota própria, e não uma action da auditoria do
# gestor: o que separa as duas é a permissão, e permissão relaxada por action
# é onde vazamento nasce.
router.register("my-access", MyAccessLogViewSet, basename="my-access")


urlpatterns = [
    path("", include(router.urls)),
]
