from drf_spectacular.utils import extend_schema
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenBlacklistView as BaseTokenBlacklistView
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.views import TokenRefreshView as BaseTokenRefreshView

from apps.accounts.api.serializers import (
    MembershipSerializer,
    PasswordChangeSerializer,
    TokenRefreshSerializer,
    UserMeSerializer,
    UserMeUpdateSerializer,
)
from apps.accounts.models import Membership
from apps.accounts.passwords import issue_tokens, set_user_password
from apps.accounts.throttling import LoginEmailRateThrottle, LoginIPRateThrottle
from apps.core.audit import log_action
from apps.core.models.audit_log import AuditAction


@extend_schema(tags=["auth"])
class AuditedTokenObtainPairView(TokenObtainPairView):
    """
    Login JWT com auditoria de LOGIN (o SimpleJWT não dispara o signal
    `user_logged_in` do Django; o LOGIN_FAILED continua vindo do signal
    `user_login_failed`, disparado pelo `authenticate()`).

    O teto de tentativas (RF-CTA-5) vive aqui, não numa permission: a request
    que interessa barrar é justamente a que ainda não tem usuário nenhum.
    """

    throttle_classes = [LoginIPRateThrottle, LoginEmailRateThrottle]

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            from apps.accounts.models import User

            user = User.objects.filter(email=request.data.get("email", "")).first()
            if user:
                log_action(user, "LOGIN", "User", user.pk, request=request)
        return response


@extend_schema(tags=["auth"])
class TokenRefreshView(BaseTokenRefreshView):
    """Renovação que recusa token anterior à troca de senha (RF-CTA-3)."""

    serializer_class = TokenRefreshSerializer


@extend_schema(tags=["auth"])
class AuditedTokenBlacklistView(BaseTokenBlacklistView):
    """
    Logout com registro (§15, 20/08/2026).

    O SimpleJWT não dispara `user_logged_out`: o Django só emite esse signal
    no logout de SESSÃO, e aqui a sessão é o token. Sem esta view a auditoria
    mostrava todo LOGIN e nenhum LOGOUT, e não havia como distinguir quem
    encerrou o acesso de quem largou a aba aberta.
    """

    def post(self, request, *args, **kwargs):
        # ⚠️ ANTES do super: depois dele o refresh já está na lista negra e
        # não pode mais ser lido.
        quem_saiu = self._quem_saiu(request)
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200 and quem_saiu is not None:
            log_action(quem_saiu, AuditAction.LOGOUT, "User", quem_saiu.pk, request=request)
        return response

    @staticmethod
    def _quem_saiu(request):
        """
        O dono do refresh que está sendo invalidado.

        ⚠️ NÃO existe `request.user` aqui: o `TokenViewBase` do SimpleJWT
        zera `authentication_classes`, então esta request é sempre anônima -
        mesmo quando o front manda o cabeçalho de autenticação junto. O
        refresh é a única identificação que ela carrega, e ele é validado
        (assinatura e validade) antes de virar um nome no log.
        """
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.settings import api_settings
        from rest_framework_simplejwt.tokens import RefreshToken

        from apps.accounts.models import User

        try:
            token = RefreshToken(request.data.get("refresh") or "")
        except TokenError:
            return None
        return User.objects.filter(pk=token.get(api_settings.USER_ID_CLAIM)).first()


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
class MePasswordView(GenericAPIView):
    """
    Troca da própria senha (RF-CTA-2), e a saída do primeiro acesso (RF-EQP-7).

    Devolve um par de tokens novo porque a troca invalida os anteriores: sem
    isto, a pessoa se derrubaria ao trocar a própria senha.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = PasswordChangeSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        set_user_password(user, serializer.validated_data["new_password"])
        log_action(
            user,
            AuditAction.UPDATE,
            "User",
            user.pk,
            # Só QUE mudou, nunca o valor - a mesma régua do `changed_fields`
            # do AuditMixin.
            payload={"changed_fields": ["password"]},
            request=request,
        )
        return Response(issue_tokens(user))


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
