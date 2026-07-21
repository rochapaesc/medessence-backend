from drf_spectacular.utils import extend_schema
from rest_framework.generics import ListAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.api.serializers import (
    MembershipSerializer,
    UserMeSerializer,
    UserMeUpdateSerializer,
)
from apps.accounts.models import Membership
from apps.core.audit import log_action


@extend_schema(tags=["auth"])
class AuditedTokenObtainPairView(TokenObtainPairView):
    """
    Login JWT com auditoria de LOGIN (o SimpleJWT não dispara o signal
    `user_logged_in` do Django; o LOGIN_FAILED continua vindo do signal
    `user_login_failed`, disparado pelo `authenticate()`).
    """

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            from apps.accounts.models import User

            user = User.objects.filter(email=request.data.get("email", "")).first()
            if user:
                log_action(user, "LOGIN", "User", user.pk, request=request)
        return response


@extend_schema(tags=["me"])
class MeView(RetrieveUpdateAPIView):
    """GET/PATCH do perfil do usuário logado."""

    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "patch", "options", "head"]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        if self.request.method == "PATCH":
            return UserMeUpdateSerializer
        return UserMeSerializer


@extend_schema(tags=["me"])
class MeMembershipsView(ListAPIView):
    """
    Vínculos ativos do usuário logado com clínicas.

    É a fonte do seletor de clínica do front: 0 vínculos = sem acesso ao
    plano clínica; 1 = auto-resolve; N = o front pede o X-Clinic-Id.
    Sem paginação - ninguém tem centenas de vínculos.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = MembershipSerializer
    pagination_class = None

    def get_queryset(self):
        return (
            Membership.objects.select_related("clinic", "practitioner")
            .filter(
                user=self.request.user,
                is_active=True,
                clinic__deleted_at__isnull=True,
            )
            .order_by("clinic__name")
        )
