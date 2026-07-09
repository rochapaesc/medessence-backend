from django.db.models import TextChoices


class WhatsAppProviderKind(TextChoices):
    """Provedor do canal WhatsApp (§5). Datafy é proxy da Meta Cloud API;
    FAKE alimenta o desenvolvimento sem número real (mesmo padrão do EHR)."""

    DATAFY = "datafy", "Datafy"
    FAKE = "fake", "Fake (desenvolvimento)"
