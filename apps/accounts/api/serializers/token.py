"""Renovação de token que respeita a troca de senha (RF-CTA-3)."""

from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import (
    TokenRefreshSerializer as BaseTokenRefreshSerializer,
)
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.authentication import STALE_TOKEN_DETAIL, token_predates_password_change


class TokenRefreshSerializer(BaseTokenRefreshSerializer):
    """
    ⚠️ Sem esta checagem, derrubar sessão não derrubaria nada: o access morre
    em 4 horas, mas o cliente troca o refresh de 30 dias por um access NOVO,
    com carimbo de emissão novo, e a guarda do token velho nunca o alcança.
    """

    def validate(self, attrs):
        from apps.accounts.models import User

        try:
            refresh = RefreshToken(attrs["refresh"])
        except TokenError:
            # Token estragado ou vencido: deixa o erro padrão do SimpleJWT sair.
            return super().validate(attrs)

        user = User.objects.filter(pk=refresh.payload.get("user_id")).first()
        if user is not None and token_predates_password_change(user, refresh):
            raise InvalidToken(STALE_TOKEN_DETAIL)
        return super().validate(attrs)
