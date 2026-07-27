from django.db.models import TextChoices


class WebhookSource(TextChoices):
    """Origem do webhook cru (§9.11). ASAAS reservado para a F5 (billing).

    DATAFY é legado somente-leitura: a tabela é imutável e eventos antigos
    guardam o source da época — o valor fica para as linhas continuarem
    legíveis, nenhum evento novo o usa."""

    META = "meta", "Meta (WhatsApp)"
    DATAFY = "datafy", "Datafy (WhatsApp, legado)"
    ASAAS = "asaas", "Asaas (billing)"
