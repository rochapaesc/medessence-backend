"""Rotas da equipe da clínica (§4.12). Separadas das de auth, que não são escopadas."""

from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.accounts.api.viewsets import TeamViewSet

router = SimpleRouter()
router.register("team", TeamViewSet, basename="team")

urlpatterns = [path("", include(router.urls))]
