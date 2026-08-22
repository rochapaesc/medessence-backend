"""
Rotas do plano plataforma (§4.8).

Separadas das de `tenants/api/views.py`, que são da CLÍNICA ativa e passam
pelo contexto: aqui não há contexto de clínica nenhum, e misturar as duas
faria uma rota escopada nascer ao lado de uma que enxerga todos os tenants.
"""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.tenants.api.platform_views import (
    PlatformClinicViewSet,
    PlatformHealthView,
    PlatformOverviewView,
    PlatformSyncView,
    PlatformUsersView,
)

router = SimpleRouter()
router.register("clinics", PlatformClinicViewSet, basename="platform-clinic")

urlpatterns = [
    path("overview/", PlatformOverviewView.as_view(), name="platform-overview"),
    path("sync/", PlatformSyncView.as_view(), name="platform-sync"),
    path("users/", PlatformUsersView.as_view(), name="platform-users"),
    path("health/", PlatformHealthView.as_view(), name="platform-health"),
    path("", include(router.urls)),
]
