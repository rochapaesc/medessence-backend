from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from apps.accounts.api.views import MeMembershipsView, MeView

urlpatterns = [
    # Swagger
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # Auth (JWT)
    path("auth/", include("apps.accounts.api.routers")),
    # Usuário logado
    path("me/", MeView.as_view(), name="me"),
    # Vínculos do usuário com clínicas — alimenta o seletor de clínica do front
    path("me/memberships/", MeMembershipsView.as_view(), name="me-memberships"),
    # Recursos internos (auditoria)
    path("core/", include("apps.core.api.routers")),
]
