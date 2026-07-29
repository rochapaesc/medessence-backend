from django.db.models import CharField, Q, TextField, UniqueConstraint

from apps.core.models import TenantScopedModel


class QuickReply(TenantScopedModel):
    """
    Resposta rápida reutilizável do atendente (RF-INB-8).

    `shortcut` é o que vem depois da barra no composer (`/preparo`) — ideia do
    `short_code` do Chatwoot. Quem atende cinquenta conversas por dia digita
    mais rápido do que procura numa fileira de chips.
    """

    label = CharField(verbose_name="Rótulo", max_length=60)
    shortcut = CharField(
        verbose_name="Atalho",
        max_length=32,
        blank=True,
        help_text="Sem a barra. Minúsculo, sem espaço, único na clínica.",
    )
    body = TextField(verbose_name="Texto")

    class Meta:
        verbose_name = "Resposta rápida"
        verbose_name_plural = "Respostas rápidas"
        ordering = ["label"]
        constraints = [
            # Dois "/preparo" diferentes fariam a sugestão virar sorteio.
            # Vazio não colide: atalho é opcional.
            UniqueConstraint(
                fields=["clinic", "shortcut"],
                condition=Q(deleted_at__isnull=True) & ~Q(shortcut=""),
                name="uniq_quick_reply_shortcut",
            ),
        ]

    def __str__(self):
        return f"/{self.shortcut}" if self.shortcut else self.label
