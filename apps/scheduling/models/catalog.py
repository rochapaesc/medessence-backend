from django.db.models import (
    CASCADE,
    BooleanField,
    CharField,
    ForeignKey,
    PositiveSmallIntegerField,
)

from apps.core.models import TenantScopedModel


class CareUnit(TenantScopedModel):
    """Unidade de atendimento (catálogo do EHR — sync_catalogs diário)."""

    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32)

    class Meta:
        verbose_name = "Unidade"
        verbose_name_plural = "Unidades"

    def __str__(self):
        return self.name


class Procedure(TenantScopedModel):
    """Procedimento (catálogo do EHR)."""

    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32)
    duration_min = PositiveSmallIntegerField(verbose_name="Duração (min)", null=True, blank=True)
    remotely = BooleanField(verbose_name="Remoto", default=False)

    class Meta:
        verbose_name = "Procedimento"
        verbose_name_plural = "Procedimentos"

    def __str__(self):
        return self.name


class InsuranceCompany(TenantScopedModel):
    """Convênio (M1) — exigido pelo payload de criação de agendamento."""

    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32)

    class Meta:
        verbose_name = "Convênio"
        verbose_name_plural = "Convênios"

    def __str__(self):
        return self.name


class InsurancePlan(TenantScopedModel):
    """Plano do convênio (M1) — nullable no payload da vSaúde."""

    company = ForeignKey(
        InsuranceCompany,
        verbose_name="Convênio",
        on_delete=CASCADE,
        related_name="plans",
    )
    name = CharField(verbose_name="Nome", max_length=160)
    external_id = CharField(verbose_name="ID no EHR", max_length=32)

    class Meta:
        verbose_name = "Plano de convênio"
        verbose_name_plural = "Planos de convênio"

    def __str__(self):
        return f"{self.company} · {self.name}"
