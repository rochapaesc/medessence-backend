from django.db.models import (
    CharField,
    JSONField,
    Q,
    TextField,
    UniqueConstraint,
)

from apps.core.models import TenantScopedModel


class WhatsAppTemplate(TenantScopedModel):
    """
    Cache local dos templates aprovados na Meta (§9.5), atualizado pelo beat
    `refresh_wa_templates` (Fatia B). Fora da janela de 24h, o composer só
    permite enviar um destes com status APPROVED (RF-INB-3).
    """

    name = CharField(verbose_name="Nome", max_length=120)
    language = CharField(verbose_name="Idioma", max_length=10, default="pt_BR")
    category = CharField(verbose_name="Categoria", max_length=30, blank=True)
    status = CharField(verbose_name="Status", max_length=20, blank=True)
    components = JSONField(verbose_name="Componentes", default=list, blank=True)

    #: O id da Meta desta VARIANTE de idioma (RF-INB-3.2).
    #:
    #: ⚠️ Sem ele não dá para editar nem apagar uma variante sozinha: a Meta
    #: apaga pelo NOME, e sem `hsm_id` remove todas as línguas de uma vez.
    #: Vazio nos que vieram só da sincronização, que é o caso de todos os
    #: anteriores a 13/08/2026.
    meta_template_id = CharField(
        verbose_name="Id na Meta", max_length=64, blank=True, db_index=True
    )

    #: Por que a Meta recusou, quando recusou.
    #:
    #: Template recusado NÃO some: fica como rascunho local com o motivo, para
    #: a clínica corrigir em cima do que escreveu em vez de recomeçar.
    rejection_reason = TextField(verbose_name="Motivo da recusa", blank=True)

    class Meta:
        verbose_name = "Template WhatsApp"
        verbose_name_plural = "Templates WhatsApp"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name", "language"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_template",
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.language})"
