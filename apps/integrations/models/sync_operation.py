from django.db.models import (
    BigIntegerField,
    CharField,
    Index,
    JSONField,
    PositiveSmallIntegerField,
    TextField,
)

from apps.core.models import TenantScopedModel
from apps.integrations.choices import OperationStatus, SyncDirection, SyncResource
from apps.tenants.choices import EHRProviderKind


class SyncOperation(TenantScopedModel):
    """
    Fila de write-back para o EHR (§10.2) — a razão de o sistema seguir
    usável com o EHR fora do ar (RNF-3). Mutação local em recurso de dono
    compartilhado/EHR cria uma operação PENDING; a task processa com retry
    exponencial; falha definitiva fica FAILED, visível no painel de sync.

    O processamento (task + adapter vSaúde) entra na fase de conexão do EHR.
    """

    provider = CharField(
        verbose_name="Provedor",
        max_length=20,
        choices=EHRProviderKind.choices,
    )
    direction = CharField(
        verbose_name="Direção",
        max_length=10,
        choices=SyncDirection.choices,
        default=SyncDirection.PUSH,
    )
    resource_type = CharField(
        verbose_name="Recurso",
        max_length=20,
        choices=SyncResource.choices,
    )
    local_id = BigIntegerField(verbose_name="ID local")
    payload = JSONField(
        verbose_name="Payload",
        help_text="Formato NOSSO — o adapter desnormaliza na saída.",
    )
    status = CharField(
        verbose_name="Status",
        max_length=10,
        choices=OperationStatus.choices,
        default=OperationStatus.PENDING,
    )
    attempts = PositiveSmallIntegerField(verbose_name="Tentativas", default=0)
    last_error = TextField(verbose_name="Último erro", blank=True)

    class Meta:
        verbose_name = "Operação de sync"
        verbose_name_plural = "Operações de sync"
        indexes = [
            Index(fields=["status", "created_at"]),  # varredura do push_sync_operations
        ]

    def __str__(self):
        return f"{self.get_resource_type_display()}#{self.local_id} ({self.get_status_display()})"
