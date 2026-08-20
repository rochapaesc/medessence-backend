"""
Autenticação JWT com as duas regras do §4.12 que não cabem numa permission.

⚠️ Por que o gate da senha temporária mora AQUI, e não numa permission em
`DEFAULT_PERMISSION_CLASSES`: 36 views do projeto declaram `permission_classes`
próprio, e cada declaração SUBSTITUI o default do DRF em vez de somar. Um gate
global posto ali nasceria com buraco em toda view com permissão própria, e o
buraco seria invisível. A autenticação é o único ponto por onde toda request
com token passa, sem exceção.
"""

from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication as BaseJWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

# Rotas que continuam abertas enquanto a senha temporária está de pé
# (RF-EQP-7): trocar a senha é a saída, e o perfil é o que a tela precisa para
# dizer o nome de quem entrou. As views do SimpleJWT (entrar, renovar, sair)
# não passam por aqui, porque não têm classe de autenticação.
URL_NAMES_ALLOWED_WITH_TEMPORARY_PASSWORD = frozenset(
    {"me", "me-password", "token-obtain", "token-refresh", "token-blacklist"}
)

PASSWORD_CHANGE_REQUIRED_CODE = "password_change_required"

# A mesma frase nos dois lugares em que a sessão morre por troca de senha.
STALE_TOKEN_DETAIL = "A sua senha mudou. Entre de novo."


def token_predates_password_change(user, token) -> bool:
    """
    Diz se o token foi emitido ANTES da última troca de senha (RF-CTA-3).

    É o que faz "derrubar as outras sessões" significar alguma coisa: sem
    isto, quem saiu da clínica continua entrando pelo navegador já aberto,
    porque o refresh vive 30 dias e nada o invalida.
    """
    changed_at = getattr(user, "password_changed_at", None)
    issued_at = token.payload.get("iat") if token is not None else None
    if changed_at is None or issued_at is None:
        return False
    # ⚠️ O `iat` do JWT vem em segundos inteiros e o carimbo tem
    # microssegundos. Sem truncar o carimbo, o token emitido PELA PRÓPRIA
    # troca de senha (frações de segundo depois dela) seria recusado, e quem
    # acabou de trocar a senha cairia na hora. Truncado, a folga é de no
    # máximo um segundo.
    return int(issued_at) < int(changed_at.timestamp())


def assert_password_is_not_temporary(request, user) -> None:
    """
    Recusa a request de quem ainda usa a senha temporária (RF-EQP-7).

    Responde 403 com código próprio de propósito: 401 faria o front tratar
    como sessão expirada e mandar a pessoa para o login, que é justamente o
    lugar de onde ela acabou de vir.
    """
    if not getattr(user, "must_change_password", False):
        return

    match = getattr(request, "resolver_match", None)
    if match is not None and match.url_name in URL_NAMES_ALLOWED_WITH_TEMPORARY_PASSWORD:
        return

    raise PermissionDenied(
        {
            "detail": "Escolha uma senha própria para continuar.",
            "code": PASSWORD_CHANGE_REQUIRED_CODE,
        }
    )


class JWTAuthentication(BaseJWTAuthentication):
    """JWT do SimpleJWT mais o carimbo de senha e o gate da senha temporária."""

    def get_user(self, validated_token):
        user = super().get_user(validated_token)
        if token_predates_password_change(user, validated_token):
            raise InvalidToken(STALE_TOKEN_DETAIL)
        return user

    def authenticate(self, request):
        authenticated = super().authenticate(request)
        if authenticated is None:
            return None
        user, _token = authenticated
        assert_password_is_not_temporary(request, user)
        return authenticated
