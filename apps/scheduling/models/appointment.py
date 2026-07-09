from django.db.models import (
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    JSONField,
    PositiveSmallIntegerField,
    Q,
    UniqueConstraint,
)

from apps.core.choices import SyncStatus
from apps.core.models import TenantScopedModel
from apps.scheduling.choices import AppointmentStatus


class Appointment(TenantScopedModel):
    """
    Espelho local da agenda do EHR (RF-AGE-1). Criação nossa nasce
    `pending` e confirma com o id externo no write-through (fase do
    adapter). A agenda alimenta o motor de jornadas (RF-AGE-4) e o
    `last_appointment_at` do paciente (recalculado por signal).
    """

    patient = ForeignKey(
        "patients.Patient",
        verbose_name="Paciente",
        on_delete=RESTRICT,
        related_name="appointments",
    )
    practitioner = ForeignKey(
        "scheduling.Practitioner",
        verbose_name="Profissional",
        on_delete=RESTRICT,
        related_name="appointments",
    )
    care_unit = ForeignKey(
        "scheduling.CareUnit",
        verbose_name="Unidade",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="appointments",
    )
    procedure = ForeignKey(
        "scheduling.Procedure",
        verbose_name="Procedimento",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="appointments",
    )
    insurance_company = ForeignKey(  # M1
        "scheduling.InsuranceCompany",
        verbose_name="Convênio",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="appointments",
    )
    insurance_plan = ForeignKey(  # M1
        "scheduling.InsurancePlan",
        verbose_name="Plano",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="appointments",
    )
    starts_at = DateTimeField(verbose_name="Início", db_index=True)
    duration_min = PositiveSmallIntegerField(verbose_name="Duração (min)", default=30)
    status = CharField(
        verbose_name="Status",
        max_length=15,
        choices=AppointmentStatus.choices,
        default=AppointmentStatus.SCHEDULED,
    )
    source_status = CharField(
        verbose_name="Status cru do EHR",
        max_length=8,
        blank=True,
        help_text="Código numérico observado (10, 81, 90, 100…) — P4.",
    )
    remotely = BooleanField(verbose_name="Remoto", default=False)
    external_id = CharField(verbose_name="ID no EHR", max_length=64, blank=True)
    raw_payload = JSONField(verbose_name="Payload cru", default=dict, blank=True)
    sync_status = CharField(
        verbose_name="Sincronização",
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.SYNCED,
    )

    class Meta:
        verbose_name = "Consulta"
        verbose_name_plural = "Consultas"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_appt_ext",
            ),
        ]

    def __str__(self):
        return f"{self.patient} — {self.starts_at:%d/%m/%Y %H:%M}"

    @property
    def counts_for_last_appointment(self) -> bool:
        """Consultas canceladas/faltas não contam para o status do paciente."""
        return self.status not in (AppointmentStatus.CANCELED, AppointmentStatus.NO_SHOW)
