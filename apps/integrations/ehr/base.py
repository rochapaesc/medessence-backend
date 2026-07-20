"""
Port EHR (§5) - a fronteira entre o MedEssence e qualquer prontuário.

Adapters normalizam na entrada (o banco NUNCA vê formato de terceiro) e
desnormalizam na saída. Os DTOs abaixo são a linguagem comum: campos já
limpos (telefone E.164, HTML sanitizado, nomes sem espaços duplicados),
com o payload cru preservado apenas para auditoria/replay.

F1 cobre o sentido de LEITURA (pull). Os métodos de escrita (create/update)
entram nas fases de write-through (F3/F4).
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EHRPage:
    items: list
    total_count: int


@dataclass(frozen=True)
class EHRPatient:
    external_id: str
    name: str
    cpf: str = ""
    birth_date: date | None = None
    gender: str = "unknown"  # já normalizado para Gender choices
    email: str = ""
    phone: str = ""  # E.164 sem "+"
    city: str = ""
    state: str = ""
    address: dict = field(default_factory=dict)
    profession: str = ""
    comments_html: str = ""  # já sanitizado
    insurance_name: str = ""
    tags_bitmask: int = 0  # decodificado pelo catálogo no motor de pull
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EHRTag:
    external_id: str
    name: str
    identifier: int  # bit no bitmask de 64 bits (chega a 2^62)


@dataclass(frozen=True)
class EHRAppointment:
    external_id: str
    patient_external_id: str
    practitioner_external_id: str
    starts_at: datetime  # aware
    ends_at: datetime | None = None
    duration_min: int | None = None
    # A vSaúde embute os catálogos na agenda ({id, name}) - os nomes abaixo
    # permitem upsert dos catálogos sem chamadas extras.
    practitioner_name: str = ""  # não há endpoint de profissionais - vem da agenda
    practitioner_license: str = ""
    care_unit_external_id: str = ""
    care_unit_name: str = ""
    procedure_external_id: str = ""
    procedure_name: str = ""
    insurance_company_external_id: str = ""
    insurance_company_name: str = ""
    insurance_plan_external_id: str = ""
    insurance_plan_name: str = ""
    source_status: str = ""  # código cru (5/10/20/30/81/100…) → EHRStatusMap
    remotely: bool = False
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EHRProcedure:
    external_id: str
    name: str
    duration_min: int | None = None
    remotely: bool = False


@dataclass(frozen=True)
class EHRCareUnit:
    external_id: str
    name: str


@dataclass(frozen=True)
class EHRInsurancePlan:
    external_id: str
    name: str


@dataclass(frozen=True)
class EHRInsuranceCompany:
    external_id: str
    name: str
    plans: list[EHRInsurancePlan] = field(default_factory=list)


@runtime_checkable
class EHRProvider(Protocol):
    """Interface de leitura da F1 - um adapter por provedor."""

    def list_patients(self, page: int) -> EHRPage:
        """Página de pacientes (1-indexada). Vazia encerra a varredura."""
        ...

    def get_patient(self, external_id: str) -> EHRPatient | None:
        """Refresh pontual - usado quando a agenda referencia paciente ausente."""
        ...

    def list_tags(self) -> list[EHRTag]:
        """Catálogo de tags (decodificação do bitmask)."""
        ...

    def list_appointments(self, start: date, end: date) -> list[EHRAppointment]:
        """Agenda na janela [start, end] - pull deslizante D-7 → D+60."""
        ...

    def list_procedures(self) -> list[EHRProcedure]: ...

    def list_care_units(self) -> list[EHRCareUnit]: ...

    def list_insurance_companies(self) -> list[EHRInsuranceCompany]:
        """
        Convênios. A vSaúde não tem endpoint documentado para isso
        (pendência P12) - o adapter real retorna lista vazia até lá.
        """
        ...
