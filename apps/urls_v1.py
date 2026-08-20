from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView

from apps.accounts.api.views import MeMembershipsView, MePasswordView, MeView
from apps.inbox.api.views_reactivation import ReactivationMessageView
from apps.inbox.api.views_signup import (
    ChannelConnectView,
    ChannelDisconnectView,
    ChannelSignupConfigView,
    ChannelView,
)
from apps.integrations.api.views import EHRSyncView
from apps.notifications.api.views import (
    NotificationsCountersView,
    NotificationsReadView,
    NotificationsView,
)
from apps.patients.api.viewsets.partners import (
    PartnerDocumentOpenView,
    PartnersCalendarView,
    PartnersDayView,
)
from apps.tenants.api.views import ClinicBusinessHoursView

urlpatterns = [
    # Swagger
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path("", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("schema/redoc/", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
    # Auth (JWT)
    path("auth/", include("apps.accounts.api.routers")),
    # Usuário logado
    path("me/", MeView.as_view(), name="me"),
    # Troca da própria senha (RF-CTA-2) e saída do primeiro acesso (RF-EQP-7).
    # ⚠️ O `name` é o que a autenticação usa para deixar a rota passar com a
    # senha temporária de pé: renomear aqui tranca a única saída.
    path("me/password/", MePasswordView.as_view(), name="me-password"),
    # Vínculos do usuário com clínicas - alimenta o seletor de clínica do front
    path("me/memberships/", MeMembershipsView.as_view(), name="me-memberships"),
    # Equipe da clínica (§4.12): o gestor admite, edita e desativa sozinho.
    path("", include("apps.accounts.api.team_routers")),
    # CRM (pacientes e tags)
    path("", include("apps.patients.api.routers")),
    # Agenda e catálogos
    path("", include("apps.scheduling.api.routers")),
    # Inbox (WhatsApp)
    path("", include("apps.inbox.api.routers")),
    # Fluxos de atendimento (F2.6)
    path("", include("apps.automation.api.routers")),
    # Horário de funcionamento: quem decide se o fluxo atende ou se a conversa
    # vai para a recepção (RF-FLW-5.1)
    path(
        "clinic/business-hours/",
        ClinicBusinessHoursView.as_view(),
        name="clinic-business-hours",
    ),
    # Conexão do canal WhatsApp (F2.7, §4.3.3): o gestor liga o número da
    # clínica pelo cadastro incorporado da Meta, sem ver credencial nenhuma.
    path("channel/", ChannelView.as_view(), name="channel"),
    path(
        "channel/signup-config/",
        ChannelSignupConfigView.as_view(),
        name="channel-signup-config",
    ),
    path("channel/connect/", ChannelConnectView.as_view(), name="channel-connect"),
    path(
        "channel/disconnect/",
        ChannelDisconnectView.as_view(),
        name="channel-disconnect",
    ),
    # Mensagem de resgate: qual template sai e o que cada variável recebe
    # (RF-REA-2.2/2.3). O disparo em si segue bloqueado (RF-REA-2).
    path(
        "reactivation-message/",
        ReactivationMessageView.as_view(),
        name="reactivation-message",
    ),
    # Sincronização manual com o EHR (complementa o beat)
    path("sync/ehr/", EHRSyncView.as_view(), name="ehr-sync"),

    # Área de Parceiros (RF-PAR): a ÚNICA superfície que o papel partner vê.
    path("partners/day/", PartnersDayView.as_view(), name="partners-day"),
    path("partners/calendar/", PartnersCalendarView.as_view(), name="partners-calendar"),
    path(
        "partners/documents/<int:pk>/open/",
        PartnerDocumentOpenView.as_view(),
        name="partners-document-open",
    ),
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
