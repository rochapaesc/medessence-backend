from django.db.models import TextChoices


class WebhookSource(TextChoices):
    """Origem do webhook cru (§9.11). ASAAS reservado para a F5 (billing)."""

    DATAFY = "datafy", "Datafy (WhatsApp)"
    ASAAS = "asaas", "Asaas (billing)"
