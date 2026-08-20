"""
Teto de tentativas no login (RF-CTA-5).

A recusa já era auditada desde a F0, mas nada freava a repetição: sem teto, o
`LOGIN_FAILED` vira o registro de um ataque em curso em vez de defesa. São dois
tetos porque as duas perguntas são diferentes: um cliente martelando muitas
contas (por IP) e muitos clientes martelando uma conta (por e-mail).
"""

from rest_framework.throttling import SimpleRateThrottle


class LoginIPRateThrottle(SimpleRateThrottle):
    """Teto por origem da tentativa."""

    scope = "login_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class LoginEmailRateThrottle(SimpleRateThrottle):
    """
    Teto por conta alvo.

    ⚠️ A conta alvo é a que o cliente DIGITOU, e não uma que exista: contar só
    e-mail cadastrado entregaria, pelo próprio limite, quais e-mails existem.
    """

    scope = "login_email"

    def get_cache_key(self, request, view):
        email = str(request.data.get("email") or "").strip().lower()
        if not email:
            return None
        return self.cache_format % {"scope": self.scope, "ident": email}
