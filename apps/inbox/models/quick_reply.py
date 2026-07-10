from django.db.models import CharField, TextField

from apps.core.models import TenantScopedModel


class QuickReply(TenantScopedModel):
    """Resposta rápida reutilizável do atendente (RF-INB-8)."""

    label = CharField(verbose_name="Rótulo", max_length=60)
    body = TextField(verbose_name="Texto")

    class Meta:
        verbose_name = "Resposta rápida"
        verbose_name_plural = "Respostas rápidas"
        ordering = ["label"]

    def __str__(self):
        return self.label
