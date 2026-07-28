from django.db.models import BooleanField, CharField, Q, UniqueConstraint

from apps.core.models import TenantScopedModel


class ConversationLabel(TenantScopedModel):
    """
    Assunto da conversa (RF-ATD-9): reagendamento, orçamento, reclamação.

    ⚠️ NÃO é `patients.Tag`. Aquela é do paciente e SINCRONIZA COM A vSAÚDE -
    uma etiqueta "reclamação" tentaria virar tag no prontuário. Esta é local e
    nunca sai daqui.

    Catálogo FECHADO, mantido pelo gestor (RF-ATD-9.1): o atendente escolhe,
    não digita. Texto livre vira "reclamacao", "Reclamação" e "reclamaçao" na
    mesma semana - e aí medir assunto, que é o motivo de a etiqueta existir,
    deixa de ser possível.
    """

    name = CharField(verbose_name="Nome", max_length=40)
    color = CharField(
        verbose_name="Cor",
        max_length=7,
        blank=True,
        help_text="Hex (#RRGGBB) do ponto que identifica a etiqueta na lista.",
    )
    is_active = BooleanField(
        verbose_name="Ativa",
        default=True,
        help_text=(
            "Desligar APOSENTA a etiqueta: ela some da escolha e continua nas "
            "conversas que já a têm. Apagar reescreveria o passado - a conversa "
            "que foi uma reclamação continua tendo sido."
        ),
    )

    class Meta:
        verbose_name = "Etiqueta de conversa"
        verbose_name_plural = "Etiquetas de conversa"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_conversation_label_name",
            ),
        ]

    def __str__(self):
        return self.name
