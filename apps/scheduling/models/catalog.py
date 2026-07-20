from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    DecimalField,
    ForeignKey,
    JSONField,
    PositiveSmallIntegerField,
    TextField,
    UniqueConstraint,
)

from apps.core.models import TenantScopedModel


class CareUnit(TenantScopedModel):
    """
    Unidade de atendimento. Com EHR é espelho (sync_catalogs diário, dono =
    EHR); sem EHR é catálogo local (external_id vazio, gestão nossa) - o
    sistema funciona standalone.
    """

    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32, blank=True)
    address = JSONField(verbose_name="Endereço", default=dict, blank=True)
    work_journey = JSONField(
        verbose_name="Disponibilidade",
        default=list,
        blank=True,
        help_text=(
            "Janelas de atendimento da unidade (workJourney do EHR): "
            '[{"startDate", "endDate", "rRule", "available"}...] - alimenta '
            "a sugestão de dias no form de agendamento."
        ),
    )

    class Meta:
        verbose_name = "Unidade"
        verbose_name_plural = "Unidades"

    def __str__(self):
        return self.name


class Procedure(TenantScopedModel):
    """Procedimento (catálogo do EHR; local quando standalone)."""

    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32, blank=True)
    duration_min = PositiveSmallIntegerField(verbose_name="Duração (min)", null=True, blank=True)
    remotely = BooleanField(verbose_name="Remoto", default=False)

    class Meta:
        verbose_name = "Procedimento"
        verbose_name_plural = "Procedimentos"

    def __str__(self):
        return self.name


class PractitionerProcedure(TenantScopedModel):
    """
    Procedimento OFERECIDO por um profissional, com duração/preço próprios
    (HealthProfessionalMedicalProcedureService) - alimenta o form "Nova
    consulta": escolher o profissional filtra os procedimentos e define
    duração e preço padrão do agendamento.
    """

    practitioner = ForeignKey(
        "scheduling.Practitioner",
        verbose_name="Profissional",
        on_delete=CASCADE,
        related_name="offered_procedures",
    )
    procedure = ForeignKey(
        Procedure,
        verbose_name="Procedimento",
        on_delete=CASCADE,
        related_name="practitioner_offers",
    )
    duration_min = PositiveSmallIntegerField(verbose_name="Duração (min)", null=True, blank=True)
    price = DecimalField(
        verbose_name="Preço", max_digits=10, decimal_places=2, null=True, blank=True
    )
    description = TextField(
        verbose_name="Descrição", blank=True, help_text="HTML sanitizado do EHR."
    )
    comments = TextField(
        verbose_name="Orientações", blank=True, help_text="Instruções pós-agendamento."
    )
    allow_online = BooleanField(verbose_name="Agenda online", default=False)
    is_active = BooleanField(verbose_name="Ativo", default=True)

    class Meta:
        verbose_name = "Procedimento do profissional"
        verbose_name_plural = "Procedimentos dos profissionais"
        constraints = [
            UniqueConstraint(
                fields=["practitioner", "procedure"],
                name="uniq_practitioner_procedure",
            ),
        ]

    def __str__(self):
        return f"{self.practitioner} · {self.procedure}"


class InsuranceCompany(TenantScopedModel):
    """Convênio (M1) - exigido pelo payload de criação de agendamento."""

    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32, blank=True)

    class Meta:
        verbose_name = "Convênio"
        verbose_name_plural = "Convênios"

    def __str__(self):
        return self.name


class InsurancePlan(TenantScopedModel):
    """Plano do convênio (M1) - nullable no payload da vSaúde."""

    company = ForeignKey(
        InsuranceCompany,
        verbose_name="Convênio",
        on_delete=CASCADE,
        related_name="plans",
    )
    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32, blank=True)

    class Meta:
        verbose_name = "Plano de convênio"
        verbose_name_plural = "Planos de convênio"

    def __str__(self):
        return f"{self.company} · {self.name}"
