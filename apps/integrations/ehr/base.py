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
    blood_type: str = ""
    weight_kg: str = ""  # decimal serializado ("115.0"); vazio = ausente
    height_cm: str = ""
    guardians: dict = field(default_factory=dict)  # {mother/father/partner/sponsor: {name, phone}}
    emergency_contacts: list = field(default_factory=list)
    birth_info: dict = field(default_factory=dict)
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
    price: str = ""  # decimal serializado; vazio = ausente
    comments_html: str = ""  # já sanitizado
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
class EHRClinicalEntry:
    """
    Entrada da linha do tempo clínica, já normalizada dos discriminators
    do provedor (Note/Prescription/Exam/FormResponse na vSaúde). `source`
    distingue fontes múltiplas do mesmo kind (record | examination).
    """

    external_id: str
    kind: str  # note | prescription | exam | form_response (ClinicalEntryKind)
    date: datetime | None
    source: str = "record"
    text: str = ""  # HTML sanitizado (notas)
    title: str = ""
    description: str = ""
    document_url: str = ""
    form_answers: list = field(default_factory=list)
    creator_external_id: str = ""  # casa com Practitioner.external_id
    creator_name: str = ""
    creator_license: str = ""
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EHRPrescriptionModel:
    external_id: str
    name: str
    content: str = ""  # HTML sanitizado
    hint: str = ""
    medications: list = field(default_factory=list)
    smart: bool = False
    special_prescription: bool = False


@dataclass(frozen=True)
class EHRAvailability:
    """Horários livres de um dia (form Nova consulta)."""

    date: str  # ISO date
    has_availability: bool = False
    times: list = field(default_factory=list)  # ["09:00", ...] ou objetos do provedor


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
    description: str = ""  # HTML sanitizado
    comments: str = ""  # orientações pós-agendamento (HTML sanitizado)
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


@dataclass(frozen=True)
class EHRFile:
    """
    Um item da árvore de arquivos do paciente (RF-PRO-7).

    `url` é o endereço do arquivo no storage do EHR e **não pode sair na
    listagem da nossa API** — ele só é entregue pela ação de abrir, que é a
    que audita (§4.10). Aqui ele existe porque o adapter o recebe do provedor.
    """

    external_id: str
    name: str
    is_directory: bool = False
    size: int = 0
    mime_type: str = ""
    url: str = ""
    created_at: str = ""
    #: O EHR gerou este item (pasta de exames, pedido emitido) — não se
    #: renomeia nem se apaga por aqui.
    read_only: bool = False
    can_delete: bool = True
    #: Pasta de sistema: `.internal` some da tela, `.exams` vira "Exames".
    system: bool = False
    hidden: bool = False


@dataclass(frozen=True)
class EHRFolderListing:
    """Conteúdo de uma pasta: subpastas e arquivos, já separados."""

    folder_external_id: str = ""
    folder_name: str = ""
    #: A pasta ABERTA é do próprio prontuário (`.exams` e companhia). Vem
    #: no DTO porque a API precisa recusar envio para dentro dela, e o nome
    #: que sai daqui já está traduzido — não dá para reconhecê-la depois.
    folder_is_system: bool = False
    folders: list[EHRFile] = field(default_factory=list)
    files: list[EHRFile] = field(default_factory=list)


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

    def get_clinical_entries(self, patient_external_id: str) -> list[EHRClinicalEntry]:
        """
        Linha do tempo clínica do paciente (prontuário + exames solicitados),
        já normalizada. Espelho somente leitura (§10.1).
        """
        ...

    def list_prescription_models(self) -> list[EHRPrescriptionModel]:
        """Catálogo de modelos de prescrição."""
        ...

    def get_availability(
        self,
        professional_external_id: str,
        procedure_external_id: str,
        care_unit_external_id: str,
        date: str,
    ) -> EHRAvailability:
        """Horários livres do profissional num dia (form Nova consulta)."""
        ...

    def list_professional_procedures(
        self, professional_external_id: str
    ) -> list[EHRProfessionalProcedure]:
        """Procedimentos oferecidos POR profissional (duração/preço do form)."""
        ...

    # ---------------- arquivos do paciente (RF-PRO-7) ---------------- #
    # Proxy AO VIVO, sem espelho local (decisão de 30/07/2026, §18): a
    # listagem sai do EHR na hora, como o `get_availability`.

    def list_files(
        self, patient_external_id: str, folder_external_id: str = ""
    ) -> EHRFolderListing:
        """Conteúdo de uma pasta do paciente. Sem pasta = raiz."""
        ...

    def upload_file(
        self,
        patient_external_id: str,
        folder_external_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> None:
        """
        Envia um arquivo para a pasta. Não devolve nada: a vSaúde responde
        vazio e o chamador re-lista para confirmar.
        """
        ...

    def rename_file(self, file_external_id: str, name: str) -> str:
        """Renomeia e devolve o nome final (o EHR preserva a extensão)."""
        ...

    def delete_file(self, file_external_id: str) -> None:
        """Remove o arquivo do prontuário."""
        ...

    def export_document(self, document_external_id: str) -> tuple[bytes, str]:
        """
        O PDF de um documento clínico (receita/pedido), como `(bytes, mime)`.
        RF-PAR-3: é o que a ação de abrir entrega, depois de auditar.
        """
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

    def transition_appointment(self, external_id: str, target_status: str) -> bool:
        """
        Ação semântica → rota do provedor. `target_status` no nosso vocabulário:
        confirmed/waiting/in_progress/completed/canceled/no_show. Devolve True
        quando EMPURROU uma rota ao EHR (o caller então confirma por Get);
        False quando o status não tem rota no provedor (ex.: in_progress na
        vSaúde) - transição LOCAL-only, sem re-busca de confirmação
        (guarda anti-regressão, RF-AGE-5).
        """
        ...

    def get_appointment(self, external_id: str) -> EHRAppointment | None:
        """Refresh pontual - confirma o código de status pós-ação."""
        ...
