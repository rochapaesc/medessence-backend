from django.db.models import (
    SET_NULL,
    CharField,
    DateTimeField,
    ForeignKey,
    GenericIPAddressField,
    Index,
    JSONField,
    Model,
    TextChoices,
)


class AuditAction(TextChoices):
    CREATE = "CREATE", "Criação"
    UPDATE = "UPDATE", "Atualização"
    DELETE = "DELETE", "Exclusão"
    RESTORE = "RESTORE", "Restauração"
    # Leitura de conteúdo clínico (LGPD §15) - usada a partir da F4
    READ = "READ", "Leitura"
    # Documento pessoal revelado na tela (§15): registro dedicado para
    # responder "quem viu o CPF de quem, e quando" sem garimpar no meio dos
    # acessos comuns à ficha. O valor do documento NUNCA entra no payload.
    READ_CPF = "READ_CPF", "Leitura de CPF"
    LOGIN = "LOGIN", "Login"
    LOGIN_FAILED = "LOGIN_FAILED", "Login falhou"
    LOGOUT = "LOGOUT", "Logout"


class AuditLog(Model):
    """Imutável por design - não herda de BaseModel (sem soft delete/update)."""

    user = ForeignKey(
        "accounts.User",
        verbose_name="Usuário",
        null=True,
        on_delete=SET_NULL,
        related_name="audit_logs",
    )
    clinic = ForeignKey(
        "tenants.Clinic",
        verbose_name="Clínica",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="audit_logs",
        help_text="Nula em eventos globais (login, ações do plano plataforma).",
    )
    action = CharField(verbose_name="Ação", max_length=50, choices=AuditAction.choices)
    resource = CharField(verbose_name="Recurso", max_length=100)
    resource_id = CharField(verbose_name="ID do recurso", max_length=50)
    payload = JSONField(verbose_name="Payload", default=dict)
    ip_address = GenericIPAddressField(verbose_name="Endereço IP", null=True)
    timestamp = DateTimeField(verbose_name="Timestamp", auto_now_add=True)

    class Meta:
        verbose_name = "Log de auditoria"
        verbose_name_plural = "Logs de auditoria"
        indexes = [
            Index(fields=["user", "timestamp"]),
            Index(fields=["resource", "resource_id"]),
            Index(fields=["clinic", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.action} {self.resource}#{self.resource_id}"
