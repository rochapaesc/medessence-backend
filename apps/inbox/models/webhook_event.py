from django.db.models import (
    SET_NULL,
    CharField,
    DateTimeField,
    ForeignKey,
    Index,
    JSONField,
    Model,
    TextField,
)

from apps.inbox.choices import WebhookSource


class WebhookEvent(Model):
    """
    Log CRU e imutável de todo webhook recebido, antes de qualquer parse
    (RNF-3): permite replay e diagnóstico. Segue o padrão do `AuditLog` - NÃO
    herda de `BaseModel`: um log de webhook nunca é editado nem soft-deletado
    (só nasce e marca `processed_at`/`error`). Não é TenantScoped: a clínica
    pode ser nula até o processamento resolver o canal.
    """

    source = CharField(verbose_name="Origem", max_length=20, choices=WebhookSource.choices)
    clinic = ForeignKey(
        "tenants.Clinic",
        verbose_name="Clínica",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="webhook_events",
    )
    dedupe_key = CharField(
        verbose_name="Chave de deduplicação", max_length=200, blank=True, db_index=True
    )
    payload = JSONField(verbose_name="Payload")
    created_at = DateTimeField(verbose_name="Recebido em", auto_now_add=True)
    processed_at = DateTimeField(verbose_name="Processado em", null=True, blank=True)
    error = TextField(verbose_name="Erro", blank=True)

    class Meta:
        verbose_name = "Evento de webhook"
        verbose_name_plural = "Eventos de webhook"
        ordering = ["-created_at"]
        indexes = [
            Index(fields=["source", "created_at"]),
            Index(fields=["clinic", "processed_at"]),
        ]

    def __str__(self):
        return f"{self.get_source_display()} @ {self.created_at:%d/%m/%Y %H:%M}"
