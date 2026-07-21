from django.db.models import CASCADE, DateTimeField, ForeignKey, UniqueConstraint

from apps.core.models import TenantScopedModel


class NotificationRead(TenantScopedModel):
    """
    Marca d'água de leitura da central de notificações.

    A central não persiste notificações - o feed é derivado de `Appointment` e
    `SyncRun` a cada leitura (ver `apps.notifications.services`). O único estado
    guardado é até quando este usuário já viu, nesta clínica: uma notificação é
    não-lida quando `occurred_at > read_at`.

    Uma linha por (clínica, usuário) - a leitura é por contexto de clínica,
    então quem atende em duas clínicas tem um badge independente em cada uma.
    """

    user = ForeignKey(
        "accounts.User",
        verbose_name="Usuário",
        on_delete=CASCADE,
        related_name="notification_reads",
    )
    read_at = DateTimeField(verbose_name="Lido até")

    class Meta:
        verbose_name = "Leitura de notificações"
        verbose_name_plural = "Leituras de notificações"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "user"],
                name="uniq_notification_read_per_user_clinic",
            )
        ]

    def __str__(self):
        return f"{self.user} @ {self.clinic} (lido até {self.read_at:%d/%m %H:%M})"
