class EHRError(Exception):
    """Erro genérico de integração com o EHR."""


class EHRAuthError(EHRError):
    """API key inválida/expirada - não adianta retry."""


class EHRRateLimitedError(EHRError):
    """429 - recuar e tentar depois (P1: limite real desconhecido)."""


class EHRUnavailableError(EHRError):
    """EHR fora do ar / 5xx - o sistema segue usável (RNF-3)."""


class EHRNotConfiguredError(EHRError):
    """Clínica sem provedor/credenciais de EHR configurados."""
