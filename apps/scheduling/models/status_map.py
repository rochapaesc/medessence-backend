from django.db.models import CharField, UniqueConstraint

from apps.core.models import BaseModel
from apps.scheduling.choices import AppointmentStatus
from apps.tenants.choices import EHRProviderKind


class EHRStatusMap(BaseModel):
    """
    Tradução administrável do status numérico do EHR para o nosso enum
    (RF-AGE-3). GLOBAL por provedor (M7): a calibração do P4 vale para
    todos os tenants de uma vez.
    """

    provider = CharField(
        verbose_name="Provedor",
        max_length=20,
        choices=EHRProviderKind.choices,
    )
    source_status = CharField(verbose_name="Código do EHR", max_length=8)
    status = CharField(
        verbose_name="Status normalizado",
        max_length=15,
        choices=AppointmentStatus.choices,
    )

    class Meta:
        verbose_name = "Mapa de status do EHR"
        verbose_name_plural = "Mapas de status do EHR"
        constraints = [
            UniqueConstraint(fields=["provider", "source_status"], name="uniq_status_map"),
        ]

    def __str__(self):
        return f"{self.get_provider_display()}: {self.source_status} → {self.get_status_display()}"
