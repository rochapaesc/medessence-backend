from django.db.models import TextChoices


class TagSyncScope(TextChoices):
    """Escopo de sincronização do catálogo de tags (§4.1 / §10.3)."""

    SYNCED = "synced", "Sincronizada com o EHR"
    PENDING_PUSH = "pending_push", "Aguardando criação no EHR"
    LOCAL_ONLY = "local_only", "Somente local"


class TagOrigin(TextChoices):
    """Origem de uma ATRIBUIÇÃO de tag — o diff do pull só toca nas do EHR."""

    EHR = "ehr", "EHR"
    LOCAL = "local", "Local"
