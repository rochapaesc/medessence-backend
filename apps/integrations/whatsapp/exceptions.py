class WhatsAppError(Exception):
    """Erro genérico de integração com o WhatsApp (Datafy/Meta)."""


class WhatsAppAuthError(WhatsAppError):
    """Token inválido/expirado - não adianta retry."""


class WhatsAppRateLimitedError(WhatsAppError):
    """429 - recuar e tentar depois (§7: 500 envios/min, 60/min mídia)."""


class WhatsAppUnavailableError(WhatsAppError):
    """Provedor fora do ar / 5xx - o inbox segue usável (RNF-3)."""


class WhatsAppNotConfiguredError(WhatsAppError):
    """Canal sem credenciais configuradas."""
