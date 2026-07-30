"""
Provider FAKE - gerador determinístico para desenvolvimento.

Permite exercitar o motor de pull completo (SyncRun, upserts, diff de tags,
vínculo de contatos, janela da agenda) sem API externa: basta uma clínica
com `ehr_provider=fake` e rodar `manage.py sync <slug>`.

Determinístico por clínica (seed = pk): rodar duas vezes retorna os mesmos
dados - o pull deve ser idempotente sobre eles. Casos cobertos de propósito:
    - tag no bit 2^62 (limite do bitmask, §10.3);
    - telefone compartilhado entre dois pacientes (RF-PAC-7);
    - paciente sem telefone; consulta que passou do horário (status cru
      "100" → no_show) e consulta cancelada ("51").
"""

import re
from dataclasses import replace
from datetime import timedelta
from typing import ClassVar

from django.utils import timezone
from faker import Faker

from apps.integrations.ehr.base import (
    EHRAppointment,
    EHRAvailability,
    EHRFile,
    EHRFolderListing,
    EHRCareUnit,
    EHRClinicalEntry,
    EHRInsuranceCompany,
    EHRInsurancePlan,
    EHRPage,
    EHRPatient,
    EHRPrescriptionModel,
    EHRProcedure,
    EHRProfessional,
    EHRProfessionalProcedure,
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
# Códigos crus com a semântica OFICIAL da vSaúde (P4, migration 0003):
#   10 = agendada pelo profissional   30 = confirmada pelo profissional
#   81 = finalizada                   100 = passou do horário (→ no_show)
#   51 = cancelada por funcionário
#
# O ciclo antigo emitia "90" com o rótulo "realizada" - código que NÃO EXISTE
# na vSaúde (erro do levantamento original, confirmado em 21/07/2026 e
# retirado do mapa pela migration 0008) - e chamava 100 de "cancelada", que é
# "passou do horário". Um fake que emite código inexistente faz o dev
# exercitar um caminho impossível em produção, e enchia o pull de
# `unmapped_statuses`.
STATUS_CYCLE = ["10", "30", "81", "100", "51"]


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
        created = self._created_patients.get(self.clinic.pk, {})
        if external_id in created:
            return created[external_id]
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

    def list_professionals(self) -> list[EHRProfessional]:
        return [
            EHRProfessional(
                external_id=f"fake-prof-{i}",
                name=f"Dr(a). Fake {i}",
                email=f"prof{i}@clinica.dev",
                phone=f"5585988{self.clinic.pk:02d}000{i}",
            )
            for i in (1, 2)
        ]

    def list_professional_procedures(
        self, professional_external_id: str
    ) -> list[EHRProfessionalProcedure]:
        return [
            EHRProfessionalProcedure(
                procedure_external_id=f"fake-proc-{i}",
                name=name,
                duration_min=duration,
                price="400.00" if i == 1 else "0.00",
                allow_online=i != 1,
            )
            for i, (name, duration) in enumerate(PROCEDURES, start=1)
        ]

    def get_clinical_entries(self, patient_external_id: str) -> list[EHRClinicalEntry]:
        """Linha do tempo determinística: nota + prescrição + 2 exames + form."""
        base_date = timezone.now() - timedelta(days=30)
        prefix = f"{patient_external_id}-ce"
        creator = {"ext": "fake-prof-1", "name": "Dr(a). Fake 1", "license": "CRM-FK 0001"}
        return [
            EHRClinicalEntry(
                external_id=f"{prefix}-note",
                kind="note",
                source="record",
                date=base_date,
                text="<p>Evolução de teste do paciente.</p>",
                creator_external_id=creator["ext"],
                creator_name=creator["name"],
                creator_license=creator["license"],
                raw={"fake": True},
            ),
            EHRClinicalEntry(
                external_id=f"{prefix}-presc",
                kind="prescription",
                source="record",
                date=base_date,
                document_url="https://fake.ehr/doc/presc-1",
                creator_external_id=creator["ext"],
                creator_name=creator["name"],
                raw={"fake": True},
            ),
            EHRClinicalEntry(
                external_id=f"{prefix}-exam-rec",
                kind="exam",
                source="record",
                date=base_date - timedelta(days=60),
                document_url="https://fake.ehr/doc/exam-1",
                creator_external_id=creator["ext"],
                creator_name=creator["name"],
                raw={"fake": True},
            ),
            EHRClinicalEntry(
                external_id=f"{prefix}-exam-req",
                kind="exam",
                source="examination",
                date=base_date - timedelta(days=59),
                title="Hemograma completo",
                description="<p>Jejum de 8 horas.</p>",
                creator_external_id=creator["ext"],
                raw={"fake": True},
            ),
            EHRClinicalEntry(
                external_id=f"{prefix}-form",
                kind="form_response",
                source="record",
                date=base_date,
                title="Triagem",
                form_answers=[
                    {"label": "Queixa principal", "answer": "Rotina", "fieldType": "textbox"},
                ],
                creator_external_id=creator["ext"],
                creator_name=creator["name"],
                raw={"fake": True},
            ),
        ]

    def list_prescription_models(self) -> list[EHRPrescriptionModel]:
        return [
            EHRPrescriptionModel(
                external_id="fake-pm-1",
                name="Receita padrão",
                content="<p>Uso contínuo conforme orientação.</p>",
                medications=[{"name": "Medicamento A", "dosage": "1x ao dia"}],
            ),
            EHRPrescriptionModel(
                external_id="fake-pm-2",
                name="Receita controlada",
                content="<p>Modelo de receita especial.</p>",
                special_prescription=True,
            ),
        ]

    def get_availability(
        self,
        professional_external_id: str,
        procedure_external_id: str,
        care_unit_external_id: str,
        date: str,
    ) -> EHRAvailability:
        return EHRAvailability(
            date=date,
            has_availability=True,
            times=["09:00", "09:30", "10:00", "14:00", "14:30"],
        )

    # ---------------- arquivos do paciente (RF-PRO-7) ---------------- #
    #
    # Guardados em memória por processo: o suficiente para exercitar a aba
    # (entrar em pasta, subir, renomear, apagar) sem EHR real. Reinicia com o
    # servidor, e isso é de propósito - dado de dev não deve parecer perene.

    _ARQUIVOS: ClassVar[dict] = {}

    def _pastas_iniciais(self, patient_external_id: str) -> dict:
        """A mesma forma da vSaúde: duas pastas do usuário e a de exames."""
        return {
            "": [
                EHRFile(
                    external_id=f"folder-atestado-{patient_external_id}",
                    name="Atestado Médico",
                    is_directory=True,
                ),
                EHRFile(
                    external_id=f"folder-exames-{patient_external_id}",
                    name="Exames",
                    is_directory=True,
                    system=True,
                    read_only=True,
                    can_delete=False,
                ),
            ],
            f"folder-exames-{patient_external_id}": [
                EHRFile(
                    external_id=f"file-pedido-{patient_external_id}",
                    name="Pedido 02/09/2025 162540.pdf",
                    size=243251,
                    mime_type="application/pdf",
                    url="https://exemplo.invalido/fake/pedido.pdf",
                    created_at="2025-09-02T16:25:48Z",
                    read_only=True,
                    can_delete=False,
                ),
            ],
        }

    def _arvore(self, patient_external_id: str) -> dict:
        return self._ARQUIVOS.setdefault(
            patient_external_id, self._pastas_iniciais(patient_external_id)
        )

    def list_files(
        self, patient_external_id: str, folder_external_id: str = ""
    ) -> EHRFolderListing:
        arvore = self._arvore(patient_external_id)
        itens = arvore.get(folder_external_id, [])
        nome = ""
        if folder_external_id:
            nome = next(
                (
                    p.name
                    for p in arvore.get("", [])
                    if p.external_id == folder_external_id
                ),
                "",
            )
        return EHRFolderListing(
            folder_external_id=folder_external_id,
            folder_name=nome,
            folders=[i for i in itens if i.is_directory],
            files=[i for i in itens if not i.is_directory],
        )

    def upload_file(
        self,
        patient_external_id: str,
        folder_external_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> None:
        arvore = self._arvore(patient_external_id)
        arvore.setdefault(folder_external_id, []).append(
            EHRFile(
                external_id=f"file-{len(content)}-{filename}",
                name=filename,
                size=len(content),
                mime_type=content_type,
                url=f"https://exemplo.invalido/fake/{filename}",
                created_at=timezone.now().isoformat(),
            )
        )

    def rename_file(self, file_external_id: str, name: str) -> str:
        for itens in self._ARQUIVOS.values():
            for pasta, lista in itens.items():
                for i, item in enumerate(lista):
                    if item.external_id != file_external_id:
                        continue
                    # Como a vSaúde: a extensão é do servidor, não de quem
                    # digitou.
                    extensao = item.name.rpartition(".")[2]
                    final = f"{name}.{extensao}" if extensao else name
                    lista[i] = replace(item, name=final)
                    return final
        return name

    def delete_file(self, file_external_id: str) -> None:
        for itens in self._ARQUIVOS.values():
            for pasta, lista in itens.items():
                itens[pasta] = [
                    i for i in lista if i.external_id != file_external_id
                ]

    # -------------------- escrita (registro em memória) ------------------- #
    # Estado de escrita fica em dicts DE CLASSE, particionados por clínica:
    # sobrevive entre instâncias no mesmo processo (a task de push cria um
    # adapter novo por operação) e é suficiente p/ dev e testes.

    _created_patients: ClassVar[dict] = {}  # clinic_pk -> {external_id: EHRPatient}
    _deleted_patients: ClassVar[dict] = {}  # clinic_pk -> set[external_id]
    _patient_tags: ClassVar[dict] = {}  # clinic_pk -> {patient_ext: set[str]}
    _extra_tags: ClassVar[dict] = {}  # clinic_pk -> {name: EHRTag}
    _appointments: ClassVar[dict] = {}  # clinic_pk -> {external_id: EHRAppointment}
    _sequences: ClassVar[dict] = {}  # clinic_pk -> int

    # Ação semântica → código cru "gravado" pelo fake (compatível com o
    # mapa da vSaúde p/ facilitar leitura; sem EHRStatusMap p/ 'fake', o
    # confirmador mantém o status otimista - comportamento esperado).
    # PARIDADE com a vSaúde real (RF-AGE-5): waiting grava "9" e
    # "in_progress" NÃO tem rota - fica fora do dict de propósito, para o
    # dev exercitar o caminho local-only + guarda anti-regressão.
    TRANSITION_CODES: ClassVar[dict] = {
        "confirmed": "30",
        "waiting": "9",
        "completed": "81",
        "canceled": "50",
        "no_show": "6",
    }

    @classmethod
    def _next_seq(cls, clinic_pk: int) -> int:
        cls._sequences[clinic_pk] = cls._sequences.get(clinic_pk, 0) + 1
        return cls._sequences[clinic_pk]

    def search_patients(self, keyword: str) -> list[EHRPatient]:
        keyword = (keyword or "").strip().lower()
        pool = [*self._patients, *self._created_patients.get(self.clinic.pk, {}).values()]
        deleted = self._deleted_patients.get(self.clinic.pk, set())
        return [
            p
            for p in pool
            if p.external_id not in deleted
            and keyword
            and (keyword in p.name.lower() or keyword in re.sub(r"\D", "", p.cpf))
        ]

    def create_patient(self, data: dict) -> EHRPatient:
        external_id = f"fake-{self.clinic.pk}-pat-local-{self._next_seq(self.clinic.pk)}"
        patient = EHRPatient(
            external_id=external_id,
            name=data.get("name", ""),
            cpf=data.get("cpf", ""),
            gender=data.get("gender", Gender.UNKNOWN),
            email=data.get("email", ""),
            phone=data.get("phone", ""),
            city=data.get("city", ""),
            state=data.get("state", ""),
            profession=data.get("profession", ""),
            raw={"fake": True, "created_by_push": True},
        )
        self._created_patients.setdefault(self.clinic.pk, {})[external_id] = patient
        return patient

    def update_patient(self, external_id: str, data: dict) -> None:
        created = self._created_patients.setdefault(self.clinic.pk, {})
        base = created.get(external_id) or self.get_patient(external_id)
        if base is None:
            return  # permissivo: dev stub não falha em estado desconhecido
        created[external_id] = EHRPatient(
            external_id=external_id,
            name=data.get("name", base.name),
            cpf=data.get("cpf", base.cpf),
            gender=data.get("gender", base.gender),
            email=data.get("email", base.email),
            phone=data.get("phone", base.phone),
            city=data.get("city", base.city),
            state=data.get("state", base.state),
            profession=data.get("profession", base.profession),
            raw={"fake": True, "updated_by_push": True},
        )

    def delete_patient(self, external_id: str) -> None:
        self._deleted_patients.setdefault(self.clinic.pk, set()).add(external_id)

    def add_patient_tag(self, patient_external_id: str, tag_name: str) -> EHRTag:
        extras = self._extra_tags.setdefault(self.clinic.pk, {})
        known = {
            name: EHRTag(f"fake-tag-{i}", name, value) for i, (name, value) in enumerate(TAGS, 1)
        }
        tag = known.get(tag_name) or extras.get(tag_name)
        if tag is None:
            used = {t.identifier for t in [*known.values(), *extras.values()]}
            bit = 1
            while bit in used:
                bit <<= 1
            tag = EHRTag(external_id=f"fake-tag-x{len(extras) + 1}", name=tag_name, identifier=bit)
            extras[tag_name] = tag
        self._patient_tags.setdefault(self.clinic.pk, {}).setdefault(
            patient_external_id, set()
        ).add(tag_name)
        return tag

    def remove_patient_tag(self, patient_external_id: str, tag_name: str) -> None:
        self._patient_tags.setdefault(self.clinic.pk, {}).setdefault(
            patient_external_id, set()
        ).discard(tag_name)

    def create_appointment(self, data: dict) -> EHRAppointment:
        external_id = f"fake-{self.clinic.pk}-appt-local-{self._next_seq(self.clinic.pk)}"
        appointment = EHRAppointment(
            external_id=external_id,
            patient_external_id=data.get("patient_external_id", ""),
            practitioner_external_id=data.get("practitioner_external_id", ""),
            starts_at=data.get("starts_at"),
            duration_min=data.get("duration_min"),
            care_unit_external_id=data.get("care_unit_external_id", ""),
            procedure_external_id=data.get("procedure_external_id", ""),
            source_status="10",  # agendada pelo profissional
            remotely=bool(data.get("remotely", False)),
            raw={"fake": True, "created_by_push": True},
        )
        self._appointments.setdefault(self.clinic.pk, {})[external_id] = appointment
        return appointment

    def update_appointment(self, external_id: str, data: dict) -> None:
        store = self._appointments.setdefault(self.clinic.pk, {})
        base = store.get(external_id)
        if base is None:
            return
        store[external_id] = EHRAppointment(
            external_id=external_id,
            patient_external_id=base.patient_external_id,
            practitioner_external_id=data.get(
                "practitioner_external_id", base.practitioner_external_id
            ),
            starts_at=data.get("starts_at", base.starts_at),
            duration_min=data.get("duration_min", base.duration_min),
            care_unit_external_id=data.get("care_unit_external_id", base.care_unit_external_id),
            procedure_external_id=data.get("procedure_external_id", base.procedure_external_id),
            source_status=base.source_status,
            remotely=bool(data.get("remotely", base.remotely)),
            raw=base.raw,
        )

    def delete_appointment(self, external_id: str) -> None:
        self._appointments.setdefault(self.clinic.pk, {}).pop(external_id, None)

    def transition_appointment(self, external_id: str, target_status: str) -> bool:
        code = self.TRANSITION_CODES.get(target_status)
        if code is None:
            # Sem rota no provedor (ex.: in_progress) - transição LOCAL-only.
            return False
        store = self._appointments.setdefault(self.clinic.pk, {})
        base = store.get(external_id)
        if base is None:
            return True
        store[external_id] = EHRAppointment(
            external_id=external_id,
            patient_external_id=base.patient_external_id,
            practitioner_external_id=base.practitioner_external_id,
            starts_at=base.starts_at,
            duration_min=base.duration_min,
            care_unit_external_id=base.care_unit_external_id,
            procedure_external_id=base.procedure_external_id,
            source_status=code,
            remotely=base.remotely,
            raw=base.raw,
        )
        return True

    def get_appointment(self, external_id: str) -> EHRAppointment | None:
        return self._appointments.get(self.clinic.pk, {}).get(external_id)
