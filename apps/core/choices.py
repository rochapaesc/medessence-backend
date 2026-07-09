from django.db.models import TextChoices


class SyncStatus(TextChoices):
    """
    Estado de write-through de recursos espelhados no EHR (M8).

    SYNCED: em dia com o EHR (ou recurso sem integração).
    PENDING: mutação local aguardando push (selo "aguardando sincronização" na UI).
    FAILED: push esgotou os retries — visível no painel de sync.
    """

    SYNCED = "synced", "Sincronizado"
    PENDING = "pending", "Aguardando sincronização"
    FAILED = "failed", "Falha na sincronização"
