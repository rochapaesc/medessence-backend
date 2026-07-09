"""
Provider FAKE — gerador determinístico para desenvolvimento.

Permite exercitar o motor de pull completo (SyncRun, upserts, diff de tags,
vínculo de contatos, janela da agenda) sem API externa: basta uma clínica
com `ehr_provider=fake` e rodar `manage.py sync <slug>`.

Determinístico por clínica (seed = pk): rodar duas vezes retorna os mesmos
dados — o pull deve ser idempotente sobre eles. Casos cobertos de propósito:
    - tag no bit 2^62 (limite do bitmask, §10.3);
    - telefone compartilhado entre dois pacientes (RF-PAC-7);
    - paciente sem telefone; consulta cancelada (status cru "100").
"""

from datetime import timedelta

from django.utils import timezone
from faker import Faker

from apps.integrations.ehr.base import (
    EHRAppointment,
    EHRCareUnit,
    EHRInsuranceCompany,
    EHRInsurancePlan,
    EHRPage,
    EHRPatient,
    EHRProcedure,
    EHRTag,
)
from apps.patients.choices import Gender

PATIENTS = 25
PAGE_SIZE = 10

TAGS = [
    ("VIP", 1),
    ("Pós-operatório", 2),
    ("Convênio", 4),
    ("Indicação", 8),
    ("Retorno anual", 16),
    ("Limite do bitmask", 2**62),  # exercita o DecimalField(20,0)
]
PROCEDURES = [("Consulta", 30), ("Retorno", 20), ("Avaliação", 40)]
CARE_UNITS = ["Matriz", "Filial Sul"]
# Códigos crus no formato observado da vSaúde (10=agendada, 90=realizada, 100=cancelada)
STATUS_CYCLE = ["10", "81", "90", "100"]


class FakeAdapter:
    def __init__(self, clinic):
        self.clinic = clinic
        self.fake = Faker("pt_BR")
        Faker.seed(clinic.pk * 1000)
        self._patients = [self._build_patient(i) for i in range(1, PATIENTS + 1)]

    def _build_patient(self, index: int) -> EHRPatient:
        # Telefone: compartilhado entre pacientes 3 e 4; ausente no 5 (RF-PAC-7)
        if index == 4:
            phone = f"5585977{self.clinic.pk:02d}00003"
        elif index == 5:
            phone = ""
        else:
            phone = f"5585977{self.clinic.pk:02d}{index:05d}"

        bitmask = 0
        for position, (_name, value) in enumerate(TAGS):
            if index % (position + 2) == 0:
                bitmask |= value
        return EHRPatient(
            external_id=f"fake-{self.clinic.pk}-pat-{index}",
            name=self.fake.name(),
            cpf=self.fake.cpf(),
            birth_date=self.fake.date_of_birth(minimum_age=18, maximum_age=85),
            gender=Gender.FEMALE if index % 2 else Gender.MALE,
            email=f"fake{index}@paciente.dev",
            phone=phone,
            city="Fortaleza" if index % 3 else "Caucaia",
            state="CE",
            profession=self.fake.job()[:120],
            comments_html=f"<p>Paciente de teste {index}</p>",
            insurance_name="Unimed" if index % 4 == 0 else "",
            tags_bitmask=bitmask,
            raw={"fake": True, "index": index},
        )

    # ------------------------------- port -------------------------------- #

    def list_patients(self, page: int) -> EHRPage:
        start = (page - 1) * PAGE_SIZE
        return EHRPage(
            items=self._patients[start : start + PAGE_SIZE],
            total_count=len(self._patients),
        )

    def get_patient(self, external_id: str) -> EHRPatient | None:
        return next((p for p in self._patients if p.external_id == external_id), None)

    def list_tags(self) -> list[EHRTag]:
        return [
            EHRTag(external_id=f"fake-tag-{i}", name=name, identifier=value)
            for i, (name, value) in enumerate(TAGS, start=1)
        ]

    def list_appointments(self, start, end) -> list[EHRAppointment]:
        now = timezone.now()
        appointments = []
        for index, patient in enumerate(self._patients, start=1):
            # Distribui consultas pela janela: passadas e futuras alternadas
            offset_days = (index % 14) - 7
            starts_at = now + timedelta(days=offset_days, hours=index % 8)
            if not (start <= starts_at.date() <= end):
                continue
            appointments.append(
                EHRAppointment(
                    external_id=f"fake-{self.clinic.pk}-appt-{index}",
                    patient_external_id=patient.external_id,
                    practitioner_external_id=f"fake-prof-{(index % 2) + 1}",
                    practitioner_name=f"Dr(a). Fake {(index % 2) + 1}",
                    starts_at=starts_at,
                    ends_at=starts_at + timedelta(minutes=30),
                    care_unit_external_id=f"fake-unit-{(index % len(CARE_UNITS)) + 1}",
                    procedure_external_id=f"fake-proc-{(index % len(PROCEDURES)) + 1}",
                    source_status=STATUS_CYCLE[index % len(STATUS_CYCLE)],
                    raw={"fake": True},
                )
            )
        return appointments

    def list_procedures(self) -> list[EHRProcedure]:
        return [
            EHRProcedure(
                external_id=f"fake-proc-{i}",
                name=name,
                duration_min=duration,
            )
            for i, (name, duration) in enumerate(PROCEDURES, start=1)
        ]

    def list_care_units(self) -> list[EHRCareUnit]:
        return [
            EHRCareUnit(external_id=f"fake-unit-{i}", name=name)
            for i, name in enumerate(CARE_UNITS, start=1)
        ]

    def list_insurance_companies(self) -> list[EHRInsuranceCompany]:
        return [
            EHRInsuranceCompany(
                external_id="fake-ins-1",
                name="Unimed Fake",
                plans=[EHRInsurancePlan(external_id="fake-plan-1", name="Nacional")],
            ),
        ]
