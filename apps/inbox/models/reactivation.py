from django.db.models import (
    SET_NULL,
    ForeignKey,
    JSONField,
    Q,
    UniqueConstraint,
)

from apps.core.models import TenantScopedModel


class ReactivationMessage(TenantScopedModel):
    """
    A mensagem de resgate da clínica: qual template sai e o que cada variável
    recebe (RF-REA-2.2/2.3).

    ⚠️ Mora no `inbox`, e não em `patients`, porque a dependência entre os dois
    apps hoje é de mão única (inbox → patients) e este modelo não precisa de
    paciente nenhum: ele guarda template e mapa. Pô-lo em `patients` inverteria
    a seta e acoplaria os dois em ciclo por uma tabela de uma linha por clínica.

    É UMA por clínica na v1 (constraint). Se um dia houver mais de uma campanha
    de resgate ao mesmo tempo, a constraint sai e entra um nome - mas inventar
    isso agora seria máquina para problema que a clínica não tem.
    """

    template = ForeignKey(
        "inbox.WhatsAppTemplate",
        verbose_name="Template",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="reactivation_messages",
        help_text="Nulo enquanto a conta da Meta não tiver template de resgate.",
    )
    #: Mapa `{"1": {"source": "...", "value": "..."}}`. `value` só é lido
    #: quando `source` é FIXED - guardar o texto junto do resto evita uma
    #: tabela de uma coluna só.
    variables = JSONField(verbose_name="Variáveis", default=dict, blank=True)

    class Meta:
        verbose_name = "Mensagem de reativação"
        verbose_name_plural = "Mensagens de reativação"
        constraints = [
            UniqueConstraint(
                fields=["clinic"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_reactivation_message_per_clinic",
            ),
        ]

    def __str__(self):
        alvo = self.template.name if self.template_id else "sem template"
        return f"Reativação de {self.clinic_id} ({alvo})"
