from django.db.models import TextChoices


class WhatsAppProviderKind(TextChoices):
    """Provedor do canal WhatsApp (§5). META fala com a Cloud API oficial,
    direto (decisão de 27/07/2026 — o proxy Datafy foi descartado); FAKE
    alimenta o desenvolvimento sem número real (mesmo padrão do EHR)."""

    META = "meta", "Meta Cloud API"
    FAKE = "fake", "Fake (desenvolvimento)"
