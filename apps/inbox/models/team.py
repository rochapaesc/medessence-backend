from django.db.models import BooleanField, CharField, Q, UniqueConstraint

from apps.core.models import TenantScopedModel


class Team(TenantScopedModel):
    """
    Setor de atendimento (RF-ATD-5): Recepção, Financeiro, Comercial.

    Nasce no B1 SEM tela e sem comportamento, de propósito. O que é caro é a FK
    em `Conversation` - tabela que cresce, e acrescentar coluna nela depois
    custa migração e backfill (mesmo motivo pelo qual a posse nasceu no Bloco
    A). Esta tabela tem ~1 linha por clínica: `assignment_strategy` e
    `per_agent_timeout_secs` entram no B2 junto com a distribuição automática,
    quando tiverem consumidor.
    """

    name = CharField(verbose_name="Nome", max_length=60)
    is_active = BooleanField(verbose_name="Ativo", default=True)

    class Meta:
        verbose_name = "Setor"
        verbose_name_plural = "Setores"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_team_name",
            ),
        ]

    def __str__(self):
        return self.name
