"""
Port EHR (§5) - a fronteira entre o MedEssence e qualquer prontuário.

Adapters normalizam na entrada (o banco NUNCA vê formato de terceiro) e
desnormalizam na saída. Os DTOs abaixo são a linguagem comum: campos já
limpos (telefone E.164, HTML sanitizado, nomes sem espaços duplicados),
com o payload cru preservado apenas para auditoria/replay.

Leitura (pull) desde a F1; escrita (write-through, §10.2) no bloco de
agenda+pacientes: os métodos de escrita recebem dicts no NOSSO formato
normalizado (mesmas chaves dos models) e o adapter desnormaliza na saída.

Transições de agenda são AÇÕES SEMÂNTICAS no nosso vocabulário
(`AppointmentStatus`: confirmed/in_progress/completed/canceled/no_show) -
cada adapter traduz para a rota/código do provedor. O código final quem
grava é o EHR; o chamador confirma com `get_appointment`/re-pull.
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
    address: dict = field(default_factory=dict)
    # Disponibilidade da unidade (workJourney da vSaúde): lista de janelas
    # {startDate, endDate, rRule, available, ...} - alimenta a sugestão de
    # dias no form "Nova consulta".
    work_journey: list = field(default_factory=list)


@dataclass(frozen=True)
class EHRProfessional:
    external_id: str
    name: str
    email: str = ""
    phone: str = ""
    cpf: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EHRProfessionalProcedure:
    """Procedimento OFERECIDO por um profissional (duração/preço próprios)."""

    procedure_external_id: str
    name: str
    duration_min: int | None = None
    price: str = ""  # decimal serializado ("400.00"); vazio = sem preço
    allow_online: bool = False
    is_active: bool = True


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

    def list_professionals(self) -> list[EHRProfessional]:
        """Catálogo de profissionais - inclui quem nunca atendeu."""
        ...

    def list_professional_procedures(
        self, professional_external_id: str
    ) -> list[EHRProfessionalProcedure]:
        """Procedimentos oferecidos POR profissional (duração/preço do form)."""
        ...

    # ------------------- escrita (write-through, §10.2) ------------------- #
    # `data` chega no NOSSO formato normalizado (chaves dos models); o
    # adapter desnormaliza. Erros de rede/limite levantam as exceções de
    # ehr.exceptions - a fila de push decide retry/FAILED.

    def search_patients(self, keyword: str) -> list[EHRPatient]:
        """Busca por nome/CPF - dedupe antes do create (§10.2)."""
        ...

    def create_patient(self, data: dict) -> EHRPatient:
        """Cria o paciente e devolve o registro normalizado (com external_id)."""
        ...

    def update_patient(self, external_id: str, data: dict) -> None:
        """
        Atualiza demográficos. Provedores PUT-full-object (vSaúde) devem
        buscar o registro atual e sobrepor apenas os nossos campos - nunca
        apagar o que não gerimos (tipo sanguíneo, mãe/pai, etc.).
        """
        ...

    def delete_patient(self, external_id: str) -> None:
        """Exclusão (soft no provedor) - delete bidirecional (§10.1)."""
        ...

    def add_patient_tag(self, patient_external_id: str, tag_name: str) -> EHRTag:
        """Atribui tag POR NOME (cria no catálogo se faltar) e devolve id/identifier."""
        ...

    def remove_patient_tag(self, patient_external_id: str, tag_name: str) -> None: ...

    def create_appointment(self, data: dict) -> EHRAppointment:
        """Cria o agendamento e devolve o registro normalizado (id + status cru)."""
        ...

    def update_appointment(self, external_id: str, data: dict) -> None:
        """Remarca/edita. NÃO troca o paciente (limitação do provedor)."""
        ...

    def delete_appointment(self, external_id: str) -> None: ...

    def transition_appointment(self, external_id: str, target_status: str) -> None:
        """
        Ação semântica → rota do provedor. `target_status` no nosso
        vocabulário: confirmed/in_progress/completed/canceled/no_show.
        """
        ...

    def get_appointment(self, external_id: str) -> EHRAppointment | None:
        """Refresh pontual - confirma o código de status pós-ação."""
        ...
