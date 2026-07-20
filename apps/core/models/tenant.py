from django.db.models import RESTRICT, ForeignKey

from apps.core.models.base_model import BaseModel


class TenantScopedModel(BaseModel):
    """
    Base de todo modelo de negócio do plano clínica.

    A FK `clinic` é o escopo do multi-tenant (schema único): nenhum queryset
    de negócio roda sem filtro por clínica - o `ClinicScopedViewSet` garante
    isso na leitura e injeta a clínica ativa na escrita.
    """

    clinic = ForeignKey(
        "tenants.Clinic",
        verbose_name="Clínica",
        on_delete=RESTRICT,
        related_name="%(class)ss",
    )

    class Meta:
        abstract = True
