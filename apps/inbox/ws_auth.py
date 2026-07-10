"""
Middleware ASGI de autenticação do WebSocket (§12).

Contexto pela query string (`/ws/inbox/?token=...&clinic_id=...`): valida o
JWT (SimpleJWT) e resolve o Membership ativo (mesma regra do §3.1, mas por
query string em vez de header). Sem regra de negócio — só popula o scope.
"""

from urllib.parse import parse_qs

from channels.db import database_sync_to_async


class JWTClinicMiddleware:
    """Injeta `scope["user"]` e `scope["membership"]` a partir de token+clinic_id."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        params = parse_qs((scope.get("query_string") or b"").decode())
        token = (params.get("token") or [None])[0]
        clinic_id = (params.get("clinic_id") or [None])[0]

        scope["user"] = None
        scope["membership"] = await self._resolve(token, clinic_id)
        return await self.app(scope, receive, send)

    @database_sync_to_async
    def _resolve(self, token, clinic_id):
        if not token or not clinic_id:
            return None

        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken

        from apps.accounts.models import Membership, User

        try:
            access = AccessToken(token)
            user = User.objects.get(pk=access["user_id"], is_active=True)
        except (TokenError, KeyError, User.DoesNotExist):
            return None

        return (
            Membership.objects.select_related("clinic")
            .filter(
                user=user,
                clinic_id=clinic_id,
                is_active=True,
                clinic__deleted_at__isnull=True,
            )
            .first()
        )
