from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from apps.accounts.api.views import MeMembershipsView, MeView
from apps.integrations.api.views import EHRSyncView
from apps.notifications.api.views import (
    NotificationsCountersView,
    NotificationsReadView,
    NotificationsView,
)

urlpatterns = [
    # Swagger
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # Auth (JWT)
    path("auth/", include("apps.accounts.api.routers")),
    # Usuário logado
    path("me/", MeView.as_view(), name="me"),
    # Vínculos do usuário com clínicas - alimenta o seletor de clínica do front
    path("me/memberships/", MeMembershipsView.as_view(), name="me-memberships"),
    # CRM (pacientes e tags)
    path("", include("apps.patients.api.routers")),
    # Agenda e catálogos
    path("", include("apps.scheduling.api.routers")),
    # Inbox (WhatsApp)
    path("", include("apps.inbox.api.routers")),
    # Sincronização manual com o EHR (complementa o beat)
    path("sync/ehr/", EHRSyncView.as_view(), name="ehr-sync"),
    # Central de notificações (o sino da topbar)
    path("notifications/", NotificationsView.as_view(), name="notifications"),
    path(
        "notifications/counters/",
        NotificationsCountersView.as_view(),
        name="notifications-counters",
    ),
    path("notifications/read/", NotificationsReadView.as_view(), name="notifications-read"),
    # Recursos internos (auditoria)
    path("core/", include("apps.core.api.routers")),
]
