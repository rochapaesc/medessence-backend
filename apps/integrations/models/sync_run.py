from django.db.models import CharField, DateTimeField, JSONField, TextField

from apps.core.models import TenantScopedModel
from apps.integrations.choices import SyncRunKind


class SyncRun(TenantScopedModel):
    """
    Histórico de execuções de pull por tenant - alimenta o painel de sync
    (RNF-7, RF-ADM-3). Criado pelas tasks de sync (fase do adapter).
    """

    kind = CharField(
        verbose_name="Tipo",
        max_length=20,
        choices=SyncRunKind.choices,
    )
    started_at = DateTimeField(verbose_name="Início", null=True, blank=True)
    finished_at = DateTimeField(verbose_name="Fim", null=True, blank=True)
    stats = JSONField(
        verbose_name="Estatísticas",
        default=dict,
        blank=True,
        help_text='Ex.: {"fetched": 120, "created": 3, "updated": 5}',
    )
    error = TextField(verbose_name="Erro", blank=True)

    class Meta:
        verbose_name = "Execução de sync"
        verbose_name_plural = "Execuções de sync"

    def __str__(self):
        return (
            f"{self.get_kind_display()} @ {self.clinic} ({self.started_at:%d/%m %H:%M})"
            if self.started_at
            else f"{self.get_kind_display()} @ {self.clinic}"
        )
