from datetime import timedelta

from django.db.models import (
    CharField,
    DateField,
    DateTimeField,
    EmailField,
    JSONField,
    Max,
    Q,
    TextField,
    UniqueConstraint,
)
from django.utils import timezone

from apps.core.choices import SyncStatus
from apps.core.managers import ActiveManager
from apps.core.models import TenantScopedModel
from apps.core.querysets import SoftDeleteQuerySet
from apps.patients.choices import Gender, PatientSource, PatientStatus

# RF-PAC-2 (decisão 09/07/2026): ativo = consulta nos últimos N dias; inativo
# = acima disso (ou nunca consultou). N é CONFIGURÁVEL: padrão da clínica
# (Clinic.active_window_days, default 90) com override por profissional
# (Practitioner.active_window_days) na visão da carteira.
DEFAULT_ACTIVE_WINDOW_DAYS = 90


def active_cutoff(window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS, now=None):
    return (now or timezone.now()) - timedelta(days=window_days)


class PatientQuerySet(SoftDeleteQuerySet):
    def by_status(
        self,
        status: str,
        window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS,
        practitioner=None,
    ):
        """
        Filtra por status calculado. Sem `practitioner`, usa o denormalizado
        `last_appointment_at` (visão da clínica). Com `practitioner`, a
        atividade é relativa à CARTEIRA dele: última consulta COM ELE dentro
        da janela efetiva (que o chamador já resolveu).
        """
        now = timezone.now()
        cutoff = active_cutoff(window_days, now)

        if practitioner is not None:
            queryset = self.annotate(
                practitioner_last_appointment=Max(
                    "appointments__starts_at",
                    filter=Q(appointments__practitioner=practitioner)
                    & Q(appointments__starts_at__lte=now)
                    & Q(appointments__deleted_at__isnull=True)
                    & ~Q(appointments__status__in=["canceled", "no_show"]),
                )
            )
            if status == PatientStatus.ACTIVE:
                return queryset.filter(practitioner_last_appointment__gte=cutoff)
            if status == PatientStatus.INACTIVE:
                return queryset.filter(
                    Q(practitioner_last_appointment__isnull=True)
                    | Q(practitioner_last_appointment__lt=cutoff)
                )
            return queryset

        if status == PatientStatus.ACTIVE:
            return self.filter(last_appointment_at__gte=cutoff)
        if status == PatientStatus.INACTIVE:
            return self.filter(
                Q(last_appointment_at__isnull=True) | Q(last_appointment_at__lt=cutoff)
            )
        return self

    def status_counters(
        self,
        window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS,
        practitioner=None,
    ) -> dict:
        """Contadores por status (RF-PAC-1/RF-DSH-1) — endpoint dedicado."""
        return {
            "total": self.count(),
            PatientStatus.ACTIVE.value: self.by_status(
                PatientStatus.ACTIVE, window_days, practitioner
            ).count(),
            PatientStatus.INACTIVE.value: self.by_status(
                PatientStatus.INACTIVE, window_days, practitioner
            ).count(),
        }


class PatientManager(ActiveManager):
    def get_queryset(self):
        return PatientQuerySet(self.model, using=self._db).alive()


class Patient(TenantScopedModel):
    """
    Paciente do CRM. Demográficos têm o EHR como dono (§10.1): edição local
    vira push (fase do adapter); pull sobrescreve. `last_appointment_at` é
    denormalizado da agenda e deriva o status ativo/inativo (90 dias).
    """

    name = CharField(verbose_name="Nome", max_length=200)
    cpf = CharField(
        verbose_name="Documento (CPF)",
        max_length=32,
        blank=True,
        db_index=True,
        help_text="personalIdentifier do EHR — pode ser CPF, DNI ou passaporte.",
    )
    birth_date = DateField(verbose_name="Nascimento", null=True, blank=True)
    gender = CharField(
        verbose_name="Gênero", max_length=10, choices=Gender.choices, default=Gender.UNKNOWN
    )
    email = EmailField(verbose_name="Email", blank=True)
    phone = CharField(
        verbose_name="Telefone",
        max_length=20,
        blank=True,
        db_index=True,
        help_text="E.164 — vincula o Contact do WhatsApp.",
    )
    city = CharField(verbose_name="Cidade", max_length=120, blank=True)
    state = CharField(verbose_name="UF", max_length=2, blank=True)
    address = JSONField(verbose_name="Endereço", default=dict, blank=True)
    profession = CharField(verbose_name="Profissão", max_length=120, blank=True)
    comments_html = TextField(
        verbose_name="Observações",
        blank=True,
        help_text="HTML sanitizado na entrada (whitelist).",
    )
    insurance_name = CharField(verbose_name="Convênio (texto)", max_length=120, blank=True)
    source = CharField(
        verbose_name="Origem",
        max_length=10,
        choices=PatientSource.choices,
        default=PatientSource.LOCAL,
    )
    external_id = CharField(verbose_name="ID no EHR", max_length=64, blank=True)
    raw_payload = JSONField(verbose_name="Payload cru", default=dict, blank=True)
    last_appointment_at = DateTimeField(
        verbose_name="Última consulta",
        null=True,
        blank=True,
        db_index=True,
        help_text="Denormalizado da agenda — deriva o status ativo/inativo (90 dias).",
    )
    sync_status = CharField(
        verbose_name="Sincronização",
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.SYNCED,
    )

    objects = PatientManager()

    class Meta:
        verbose_name = "Paciente"
        verbose_name_plural = "Pacientes"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_patient_ext",
            ),
        ]

    def __str__(self):
        return self.name

    def status_for_window(self, window_days: int) -> str:
        """Status calculado (RF-PAC-2) para uma janela específica."""
        if self.last_appointment_at and self.last_appointment_at >= active_cutoff(window_days):
            return PatientStatus.ACTIVE
        return PatientStatus.INACTIVE

    @property
    def status(self) -> str:
        """
        Status pela janela DA CLÍNICA. Em listagens, prefira
        `status_for_window` com a janela já resolvida (evita query por linha).
        """
        return self.status_for_window(self.clinic.active_window_days)
