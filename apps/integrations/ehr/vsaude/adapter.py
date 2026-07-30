"""
Adapter vSaúde → DTOs normalizados, sobre o contrato OFICIAL
(docs/vsaude-swagger.json + payloads reais, calibrado em 09/07/2026).

Mapeamentos reais:
    - Paciente: id (GUID), personalIdentifier → cpf, birthday → birth_date,
      gender int, phone com "+", address {city, state, ...},
      insurance {name}, tags = LISTA de identifiers (não bitmask) → OR;
    - Agenda (ScheduleService/GetAll): POST {from, range, showCanceled...};
      `range` é janela-calendário (3 = mês do `from`) - a janela D-7→D+60 é
      coberta por chamadas mensais com dedup; catálogos vêm EMBUTIDOS
      ({id, name}) em doctor/careUnit/procedure/insuranceCompany;
    - Catálogos: MedicalProcedureService (name em `description`),
      HealthCareUnitService (respeitar isDeleted), InsuranceCompanyService
      com includeInsurancePlans=true.

Regras de tratamento (§6.2): gênero int → choice; telefone → E.164 sem "+";
HTML sanitizado; nomes com colapso de espaços; datas naive → fuso da clínica.
"""

import re
import time
from datetime import date, datetime, timedelta
from typing import ClassVar
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from django.utils import timezone as dj_timezone

from apps.core.html import sanitize_html
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
from apps.integrations.ehr.exceptions import EHRError
from apps.integrations.ehr.vsaude.client import PAGE_SIZE, VSaudeClient
from apps.patients.choices import Gender

GENDER_MAP = {0: Gender.UNKNOWN, 1: Gender.MALE, 2: Gender.FEMALE}
GENDER_REVERSE = {choice: code for code, choice in GENDER_MAP.items()}

# Campos do payload de Update do paciente (PUT full-object) - espelho exato
# do que o app web envia. Montamos o corpo SÓ com estas chaves: o registro
# atual entra de base e os nossos campos sobrepõem (nunca apagar o que não
# gerimos: tipo sanguíneo, mãe/pai, birthInfo...).
# `status` e `insurance` ficam de fora: são definidos explicitamente (status
# só no Update; insurance com default {isCompany:false}) para não vazarem como
# `null` no Create - o dict-base os preencheria com None senão.
PATIENT_PAYLOAD_KEYS = (
    "personalIdentifier",
    "dni",
    "name",
    "gender",
    "birthday",
    "email",
    "phone",
    "bloodType",
    "weight",
    "height",
    "address",
    "comments",
    "birthInfo",
    "mother",
    "father",
    "partner",
    "sponsor",
    "profession",
    "photo",
)

# AppointmentsQueryRange (swagger): Daily=1, Weekly=2, Monthly=3, NowOn=4.
# Monthly retorna o MÊS-CALENDÁRIO que contém o `from`.
RANGE_MONTHLY = 3

# Só consultas médicas entram no espelho (o GetAll pode trazer outros tipos)
APPOINTMENT_DISCRIMINATOR = "MedicalAppointment"

# Pausa entre chamadas mensais da agenda - o backfill percorre muitos meses e
# sem isto estoura o rate limit da vSaúde (429).
APPOINTMENTS_PACE_SECONDS = 0.3

# Algumas clínicas NÃO usam o flag `remotely` da vSaúde: marcam o atendimento
# online por uma "unidade"/procedimento de telemedicina (ex.: "Atendimento
# Online (Telemedicina)", "Retorno Online"). Detectamos pelo nome.
REMOTE_HINTS = ("telemedicina", "teleconsulta", "online", "remoto")


def _looks_remote(*texts) -> bool:
    haystack = " ".join((t or "") for t in texts).lower()
    return any(hint in haystack for hint in REMOTE_HINTS)


def _clean_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()


def _clean_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return ""
    if len(digits) in (10, 11):  # DDD + número, sem DDI
        digits = f"55{digits}"
    return digits


def _clean_license(value) -> str:
    """licenceNumber real é um dict: {'number': 'CRM-PI 6.908', 'agency': 'CRM', 'state': 'PI'}."""
    if isinstance(value, dict):
        value = value.get("number") or ""
    return _clean_name(str(value or ""))


class VSaudeAdapter:
    def __init__(self, clinic, client: VSaudeClient | None = None):
        self.clinic = clinic
        self.client = client or VSaudeClient(clinic)
        try:
            self.tz = ZoneInfo(clinic.timezone)
        except Exception:
            self.tz = dj_timezone.get_default_timezone()

    # ------------------------------ helpers ------------------------------ #

    def _parse_datetime(self, value) -> datetime | None:
        if not value:
            return None
        parsed = dateparser.isoparse(value) if isinstance(value, str) else value
        if dj_timezone.is_naive(parsed):
            parsed = parsed.replace(tzinfo=self.tz)
        return parsed

    def _parse_date(self, value) -> date | None:
        parsed = self._parse_datetime(value)
        return parsed.date() if parsed else None

    @staticmethod
    def _decimal_str(value) -> str:
        """'115' / 115 / '115,5' → '115.5'; lixo → vazio."""
        if value in (None, ""):
            return ""
        try:
            return str(float(str(value).replace(",", ".")))
        except (TypeError, ValueError):
            return ""

    def _normalize_guardians(self, payload: dict) -> dict:
        guardians = {}
        for role in ("mother", "father", "partner", "sponsor"):
            person = payload.get(role) or {}
            if person.get("name"):
                guardians[role] = {
                    "name": _clean_name(person.get("name"))[:200],
                    "phone": _clean_phone(person.get("phoneNumber"))[:20],
                }
        return guardians

    def _normalize_patient(self, payload: dict) -> EHRPatient:
        address = payload.get("address") or {}
        insurance = payload.get("insurance") or {}
        tag_identifiers = payload.get("tags") or []
        bitmask = 0
        for identifier in tag_identifiers:
            bitmask |= int(identifier)
        # Limites dos campos do model - dado real extrapola contrato observado
        return EHRPatient(
            blood_type=(payload.get("bloodType") or "")[:3],
            weight_kg=self._decimal_str(payload.get("weight")),
            height_cm=self._decimal_str(payload.get("height")),
            guardians=self._normalize_guardians(payload),
            emergency_contacts=payload.get("emergencyContacts") or [],
            birth_info=payload.get("birthInfo") or {},
            external_id=str(payload.get("id", ""))[:64],
            name=_clean_name(payload.get("name"))[:200],
            cpf=(payload.get("personalIdentifier") or "")[:32],
            birth_date=self._parse_date(payload.get("birthday")),
            gender=GENDER_MAP.get(payload.get("gender") or 0, Gender.UNKNOWN),
            email=(payload.get("email") or "")[:254],
            phone=_clean_phone(payload.get("phone") or payload.get("phoneNumber"))[:20],
            city=(address.get("city") or "")[:120],
            state=(address.get("state") or "")[:2],
            address=address,
            profession=(payload.get("profession") or "")[:120],
            comments_html=sanitize_html(payload.get("comments")),
            insurance_name=(insurance.get("name") or "")[:120],
            tags_bitmask=bitmask,
            raw=payload,
        )

    def _normalize_appointment(self, payload: dict) -> EHRAppointment:
        doctor = payload.get("doctor") or {}
        patient = payload.get("patient") or {}
        care_unit = payload.get("careUnit") or {}
        procedure = payload.get("procedure") or {}
        insurance_company = payload.get("insuranceCompany") or {}
        insurance_plan = payload.get("insurancePlan") or {}
        return EHRAppointment(
            external_id=str(payload.get("id", ""))[:64],
            patient_external_id=str(patient.get("id", ""))[:64],
            practitioner_external_id=str(doctor.get("id", ""))[:64],
            practitioner_name=_clean_name(doctor.get("name"))[:200],
            practitioner_license=_clean_license(doctor.get("licenceNumber"))[:120],
            starts_at=self._parse_datetime(payload.get("date")),
            duration_min=payload.get("duration"),
            care_unit_external_id=str(care_unit.get("id") or "")[:32],
            care_unit_name=_clean_name(care_unit.get("name"))[:160],
            procedure_external_id=str(procedure.get("id") or "")[:32],
            procedure_name=_clean_name(procedure.get("name"))[:160],
            insurance_company_external_id=str(insurance_company.get("id") or "")[:32],
            insurance_company_name=_clean_name(insurance_company.get("name"))[:160],
            insurance_plan_external_id=str(insurance_plan.get("id") or "")[:32],
            insurance_plan_name=_clean_name(insurance_plan.get("name"))[:160],
            source_status=str(payload.get("status", ""))[:8],
            # MODALIDADE vem do nome da unidade/procedimento (telemedicina/online).
            # O flag `remotely` da vSaúde é IGNORADO: observado como true também em
            # unidades FÍSICAS (parece indicar "agendado remotamente/pelo paciente",
            # não teleconsulta), então não serve para presencial × online.
            remotely=_looks_remote(care_unit.get("name"), procedure.get("name")),
            price=self._decimal_str(payload.get("price")),
            comments_html=sanitize_html(payload.get("comments")),
            raw=payload,
        )

    # ------------------------------- port -------------------------------- #

    def list_patients(self, page: int) -> EHRPage:
        result = (
            self.client.post(
                "PatientService/GetAll",
                {"skipCount": (page - 1) * PAGE_SIZE, "maxResultCount": PAGE_SIZE},
            )
            or {}
        )
        items = result.get("items", []) if isinstance(result, dict) else (result or [])
        total = result.get("totalCount", len(items)) if isinstance(result, dict) else len(items)
        return EHRPage(
            items=[self._normalize_patient(item) for item in items],
            total_count=total,
        )

    def get_patient(self, external_id: str) -> EHRPatient | None:
        payload = self.client.get("PatientService/Get", {"Id": external_id})
        return self._normalize_patient(payload) if payload else None

    def list_tags(self) -> list[EHRTag]:
        result = self.client.get("PatientService/GetTags") or []
        items = result.get("items", result) if isinstance(result, dict) else result
        return [
            EHRTag(
                external_id=str(item.get("id", "")),
                name=_clean_name(item.get("name")),
                identifier=int(item.get("identifier") or 0),
            )
            for item in items
        ]

    def list_appointments(self, start: date, end: date) -> list[EHRAppointment]:
        # `range=Monthly` cobre o mês-calendário do `from`: itera os meses da
        # janela e filtra/deduplica localmente.
        seen: dict[str, EHRAppointment] = {}
        cursor = start.replace(day=1)
        first = True
        while cursor <= end:
            if not first:
                time.sleep(APPOINTMENTS_PACE_SECONDS)
            first = False
            items = (
                self.client.post(
                    "ScheduleService/GetAll",
                    {
                        "from": f"{cursor.isoformat()}T00:00:00Z",
                        "range": RANGE_MONTHLY,
                        "showCanceledAppointments": True,
                    },
                )
                or []
            )
            for payload in items:
                if payload.get("discriminator") != APPOINTMENT_DISCRIMINATOR:
                    continue
                appointment = self._normalize_appointment(payload)
                if appointment.starts_at and start <= appointment.starts_at.date() <= end:
                    seen[appointment.external_id] = appointment
            # próximo mês-calendário
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return list(seen.values())

    def list_procedures(self) -> list[EHRProcedure]:
        items = list(self.client.post_paginated("MedicalProcedureService/GetAll"))
        return [
            EHRProcedure(
                external_id=str(item.get("id", "")),
                name=_clean_name(item.get("description") or item.get("name")),
                duration_min=item.get("duration"),
            )
            for item in items
        ]

    def list_care_units(self) -> list[EHRCareUnit]:
        items = list(self.client.post_paginated("HealthCareUnitService/GetAll"))
        return [
            EHRCareUnit(
                external_id=str(item.get("id", "")),
                name=_clean_name(item.get("name") or item.get("description")),
                address=item.get("address") or {},
                work_journey=item.get("workJourney") or [],
            )
            for item in items
            if not item.get("isDeleted", False)
        ]

    def list_professionals(self) -> list[EHRProfessional]:
        items = list(self.client.post_paginated("HealthProfessionalService/GetAll", {"text": ""}))
        professionals = []
        for item in items:
            user = item.get("user") or {}
            professionals.append(
                EHRProfessional(
                    external_id=str(item.get("id", ""))[:64],
                    name=_clean_name(item.get("name") or user.get("fullName"))[:200],
                    email=(user.get("emailAddress") or "")[:254],
                    phone=_clean_phone(user.get("phoneNumber"))[:20],
                    cpf=(user.get("personalIdentifier") or "")[:32],
                    raw=item,
                )
            )
        return professionals

    def list_professional_procedures(
        self, professional_external_id: str
    ) -> list[EHRProfessionalProcedure]:
        items = list(
            self.client.post_paginated(
                "HealthProfessionalMedicalProcedureService/GetAll",
                {"professionalId": professional_external_id},
            )
        )
        result = []
        for item in items:
            procedure = item.get("medicalProcedure") or {}
            # Preço real vem do medicalProcedure (o item por-profissional
            # costuma vir 0.0) - calibrado com a captura de 20/07/2026.
            price = procedure.get("price") or item.get("price") or 0
            result.append(
                EHRProfessionalProcedure(
                    procedure_external_id=str(item.get("medicalProcedureId") or ""),
                    name=_clean_name(procedure.get("name")),
                    duration_min=item.get("duration") or procedure.get("duration"),
                    price=f"{float(price):.2f}",
                    description=sanitize_html(procedure.get("description")),
                    comments=sanitize_html(item.get("comments")),
                    allow_online=bool(item.get("allowOnlineSchedule", False)),
                    is_active=bool(item.get("isActive", True)),
                )
            )
        return result

    # ------------------ clínico (leitura, docs/vsaude-clinico.md) --------- #

    DISCRIMINATOR_KINDS: ClassVar[dict] = {
        "NoteMedicalRecord": "note",
        "PrescriptionMedicalRecord": "prescription",
        "ExamMedicalRecord": "exam",
        "FormResponseMedicalRecord": "form_response",
    }

    def get_clinical_entries(self, patient_external_id: str) -> list[EHRClinicalEntry]:
        entries = []

        # Fonte 1: prontuário (grupos por data, itens por discriminator).
        result = (
            self.client.post(
                "MedicalRecordEntryService/Get", {"patientId": patient_external_id}
            )
            or {}
        )
        for group in result.get("records") or []:
            for item in group.get("items") or []:
                kind = self.DISCRIMINATOR_KINDS.get(item.get("discriminator") or "")
                if kind is None:
                    continue  # discriminator desconhecido - fica no raw do run
                creator = item.get("creator") or {}
                entries.append(
                    EHRClinicalEntry(
                        external_id=str(item.get("id", ""))[:64],
                        kind=kind,
                        source="record",
                        date=self._parse_datetime(item.get("date") or group.get("date")),
                        text=sanitize_html(item.get("text")) if kind == "note" else "",
                        title=_clean_name(item.get("title"))[:200],
                        description=item.get("description") or "",
                        document_url=(item.get("link") or "")[:500],
                        form_answers=item.get("answers") or [],
                        creator_external_id=str(creator.get("id", ""))[:64],
                        creator_name=_clean_name(creator.get("name"))[:200],
                        creator_license=_clean_license(creator.get("licenceNumber"))[:120],
                        raw=item,
                    )
                )

        # Fonte 2: exames solicitados (descrição; sem link).
        exams = (
            self.client.get(
                "ExaminationService/GetExaminations", {"PatientId": patient_external_id}
            )
            or []
        )
        for item in exams:
            if item.get("isDeleted"):
                continue
            entries.append(
                EHRClinicalEntry(
                    external_id=str(item.get("id", ""))[:64],
                    kind="exam",
                    source="examination",
                    date=self._parse_datetime(item.get("creationTime")),
                    title=_clean_name(item.get("description"))[:200],
                    description=sanitize_html(item.get("examinationDescription")),
                    creator_external_id=str(item.get("doctorId", ""))[:64],
                    raw=item,
                )
            )
        return entries

    def list_prescription_models(self) -> list[EHRPrescriptionModel]:
        items = list(self.client.post_paginated("PrescriptionModelService/GetAll"))
        return [
            EHRPrescriptionModel(
                external_id=str(item.get("id", ""))[:64],
                name=_clean_name(item.get("name"))[:200],
                content=sanitize_html(item.get("content")),
                hint=sanitize_html(item.get("hint")),
                medications=item.get("medications") or [],
                smart=bool(item.get("smart", False)),
                special_prescription=bool(item.get("specialPrescription", False)),
            )
            for item in items
            if not item.get("isDeleted", False)
        ]

    def get_availability(
        self,
        professional_external_id: str,
        procedure_external_id: str,
        care_unit_external_id: str,
        date: str,
    ) -> EHRAvailability:
        def _int_or_none(value):
            try:
                return int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        result = (
            self.client.post(
                "ScheduleService/GetAvailability",
                {
                    "professionalId": professional_external_id,
                    "procedureId": _int_or_none(procedure_external_id),
                    "careUnitId": _int_or_none(care_unit_external_id),
                    "date": f"{date}T00:00:00Z",
                },
            )
            or {}
        )
        return EHRAvailability(
            date=date,
            has_availability=bool(result.get("proposedDateHasAvailability", False)),
            times=result.get("times") or [],
        )

    # ------------------- escrita (write-through, §10.2) ------------------- #

    # Rotas de transição da vSaúde (ScheduleService/<rota>). "Em atendimento"
    # (in_progress) NÃO tem rota NEM código na vSaúde (o "90" do levantamento
    # original não existe - via API ela vai de Waiting direto a Finalize):
    # a transição é LOCAL-only e devolve False (RF-AGE-5).
    TRANSITION_ROUTES: ClassVar[dict] = {
        "confirmed": "Accept",
        "waiting": "Waiting",  # Aguardando atendimento
        "completed": "Finalize",
        "canceled": "Cancel",
        "no_show": "CounterPartDidNotShowUp",
    }

    def _patient_payload(self, data: dict, base: dict | None = None) -> dict:
        """
        Corpo do Create/Update: registro atual de base (PUT full-object) com
        os NOSSOS campos por cima, restrito às chaves que o app web envia.
        """
        payload = {key: (base or {}).get(key) for key in PATIENT_PAYLOAD_KEYS}

        cpf = re.sub(r"\D", "", data.get("cpf") or "")
        if cpf:
            payload["personalIdentifier"] = cpf
            payload["dni"] = cpf
        if data.get("name"):
            payload["name"] = _clean_name(data["name"])
        if data.get("gender"):
            payload["gender"] = GENDER_REVERSE.get(data["gender"], 0)
        if data.get("birth_date"):
            # O app web envia meia-noite de Brasília ("T03:00:00.000Z").
            payload["birthday"] = f"{data['birth_date']}T03:00:00.000Z"
        if data.get("email"):
            payload["email"] = data["email"]
        if data.get("phone"):
            digits = re.sub(r"\D", "", data["phone"])
            payload["phone"] = f"+{digits}" if digits else None
        if data.get("comments_html"):
            payload["comments"] = data["comments_html"]
        if data.get("profession"):
            payload["profession"] = data["profession"]

        address = dict((base or {}).get("address") or {})
        if data.get("address"):
            address.update(data["address"])
        if data.get("city"):
            address["city"] = data["city"]
        if data.get("state"):
            address["state"] = data["state"]
        if address:
            payload["address"] = address

        # Preserva o convênio do registro atual (Update) ou usa o default
        # observado no Create. `setdefault` não serviria: a chave já existiria
        # como None vinda do `base`.
        payload["insurance"] = (base or {}).get("insurance") or {"isCompany": False}
        # NÃO enviar `tags` aqui: o payload capturado de Update não inclui a
        # chave, e as atribuições são geridas à parte (AddTag/RemoveTag). O
        # Create adiciona tags=[] explicitamente.

        # Objetos aninhados NUNCA vão como null: o CreatePatientRequest não os
        # marca nullable e o handler da vSaúde estoura em NRE → 500 "erro
        # interno" genérico (calibração ao vivo, 21/07/2026). O app web envia
        # objetos vazios (ex.: address {country: BR, ...}) - espelha o mínimo.
        for key in ("address", "birthInfo", "mother", "father", "partner", "sponsor"):
            if payload.get(key) is None:
                payload[key] = {}
        return payload

    def search_patients(self, keyword: str) -> list[EHRPatient]:
        result = self.client.post("PatientService/Search", params={"keyword": keyword}) or []
        items = result if isinstance(result, list) else result.get("items", [])
        return [self._normalize_patient(item) for item in items]

    def create_patient(self, data: dict) -> EHRPatient:
        payload = self._patient_payload(data)
        payload["tags"] = []  # só no Create (ver _patient_payload)
        result = self.client.post("PatientService/Create", payload)
        if not result or not result.get("id"):
            raise EHRError("vSaúde: Create de paciente não retornou id.")
        return self._normalize_patient(result)

    def update_patient(self, external_id: str, data: dict) -> None:
        current = self.client.get("PatientService/Get", {"Id": external_id}) or {}
        payload = self._patient_payload(data, base=current)
        payload["id"] = external_id
        payload["status"] = current.get("status", 1)
        self.client.put("PatientService/Update", payload)

    def delete_patient(self, external_id: str) -> None:
        self.client.delete("PatientService/Delete", {"Id": external_id})

    def add_patient_tag(self, patient_external_id: str, tag_name: str) -> EHRTag:
        result = (
            self.client.post(
                "PatientService/AddTag",
                {"patientId": patient_external_id, "tag": tag_name},
            )
            or {}
        )
        return EHRTag(
            external_id=str(result.get("id", "")),
            name=_clean_name(result.get("name") or tag_name),
            identifier=int(result.get("identifier") or 0),
        )

    def remove_patient_tag(self, patient_external_id: str, tag_name: str) -> None:
        # CALIBRADO ao vivo (21/07/2026): RemoveTag é PUT com o mesmo corpo do
        # AddTag - POST e DELETE respondem 405.
        self.client.put(
            "PatientService/RemoveTag",
            {"patientId": patient_external_id, "tag": tag_name},
        )

    def _appointment_payload(self, data: dict) -> dict:
        starts = self._parse_datetime(data.get("starts_at"))
        duration = int(data.get("duration_min") or 30)
        ends = starts + timedelta(minutes=duration) if starts else None

        def _int_or_none(value):
            try:
                return int(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                return None

        # Campos COMUNS a Create e Update (o `price` é exclusivo do Create,
        # conforme os payloads capturados - fica fora daqui).
        return {
            "professionalId": data.get("practitioner_external_id"),
            "procedureId": _int_or_none(data.get("procedure_external_id")),
            "insuranceCompanyId": _int_or_none(data.get("insurance_company_external_id")),
            "insurancePlanId": _int_or_none(data.get("insurance_plan_external_id")),
            "careUnitId": _int_or_none(data.get("care_unit_external_id")),
            "startDate": starts.isoformat() if starts else None,
            "endDate": ends.isoformat() if ends else None,
            "comments": data.get("comments_html") or "",
        }

    def create_appointment(self, data: dict) -> EHRAppointment:
        payload = {
            "patientId": data.get("patient_external_id"),
            **self._appointment_payload(data),
            "price": float(data.get("price") or 0),
            "signedTerms": [],
            "complementaryProcedures": [],
        }
        result = self.client.post("ScheduleService/Create", payload)
        if not result or not result.get("id"):
            raise EHRError("vSaúde: Create de agendamento não retornou id.")
        return self._normalize_appointment(result)

    def update_appointment(self, external_id: str, data: dict) -> None:
        payload = {
            "id": external_id,
            **self._appointment_payload(data),
            "remotely": bool(data.get("remotely", False)),
            "parentRecurrenceId": None,
            "recurrence": None,
            "updateAllRecurrences": False,
        }
        self.client.put("ScheduleService/Update", payload)

    def delete_appointment(self, external_id: str) -> None:
        self.client.delete(
            "ScheduleService/Delete",
            {"Id": external_id, "DeleteAllRecurrences": "false"},
        )

    def transition_appointment(self, external_id: str, target_status: str) -> bool:
        route = self.TRANSITION_ROUTES.get(target_status)
        if route is None:
            # Status sem rota na vSaúde (ex.: "em atendimento") — a mudança fica
            # só LOCAL; nada a empurrar. Não é erro.
            return False
        self.client.post(f"ScheduleService/{route}", {"id": external_id})
        return True

    def get_appointment(self, external_id: str) -> EHRAppointment | None:
        payload = self.client.get("ScheduleService/Get", {"id": external_id})
        return self._normalize_appointment(payload) if payload else None

    def list_insurance_companies(self) -> list[EHRInsuranceCompany]:
        items = list(
            self.client.post_paginated(
                "InsuranceCompanyService/GetAll", {"includeInsurancePlans": True}
            )
        )
        return [
            EHRInsuranceCompany(
                external_id=str(item.get("id", "")),
                name=_clean_name(item.get("name")),
                plans=[
                    EHRInsurancePlan(
                        external_id=str(plan.get("id", "")),
                        name=_clean_name(plan.get("name") or plan.get("description")),
                    )
                    for plan in (item.get("insurancePlans") or [])
                ],
            )
            for item in items
        ]

    # ---------------- arquivos do paciente (RF-PRO-7) ---------------- #

    #: Pastas que a vSaúde cria sozinha, com o nome que a tela mostra.
    #: Calibrado na clínica real em 30/07/2026: são QUATRO, não uma, e
    #: `.exams`/`.prescriptions` guardam o pedido e a receita que o médico
    #: emitiu — some-las esconderia justo o que a ficha existe para mostrar.
    PASTAS_DE_SISTEMA = {
        ".exams": "Exames",
        ".prescriptions": "Receitas",
        ".terms": "Termos assinados",
    }

    #: `.internal` é bagagem do EHR e não é assunto de ninguém na clínica.
    #: ⚠️ Some pelo NOME: no tenant real `isHidden` vem `false` em todas as
    #: pastas de sistema, então filtrar pela flag não esconderia nada.
    PASTAS_OCULTAS = {".internal"}

    def _arquivo(self, item: dict, *, is_directory: bool) -> EHRFile:
        nome = item.get("name") or ""
        sistema = bool(item.get("system"))
        return EHRFile(
            external_id=str(item.get("id") or ""),
            name=self.PASTAS_DE_SISTEMA.get(nome, nome),
            is_directory=is_directory,
            size=int(item.get("size") or 0),
            mime_type=item.get("mimeType") or "",
            # Só o ARQUIVO traz o endereço do blob; na pasta o `path` é o
            # caminho interno do storage e não serve para abrir nada.
            url="" if is_directory else (item.get("path") or ""),
            created_at=item.get("creationTime") or "",
            # Pasta de sistema é read-only mesmo quando o EHR não diz: é ele
            # quem escreve lá, e deixar a recepção mexer criaria conflito.
            read_only=bool(item.get("isReadOnly")) or sistema,
            can_delete=bool(item.get("allowDelete")) and not sistema,
            system=sistema,
            hidden=bool(item.get("isHidden")) or nome in self.PASTAS_OCULTAS,
        )

    def list_files(
        self, patient_external_id: str, folder_external_id: str = ""
    ) -> EHRFolderListing:
        body = {
            "patient": patient_external_id,
            "sorting": "name asc",
            "deletedOnly": False,
        }
        if folder_external_id:
            body["id"] = folder_external_id
        result = self.client.post("FilesService/ListFolder", body) or {}

        pastas = [
            self._arquivo(item, is_directory=True)
            for item in (result.get("folders") or [])
        ]
        arquivos = [
            self._arquivo(item, is_directory=False)
            for item in (result.get("files") or [])
        ]
        nome_cru = result.get("name") or ""
        return EHRFolderListing(
            folder_external_id=str(result.get("id") or ""),
            folder_name=self.PASTAS_DE_SISTEMA.get(nome_cru, nome_cru),
            # O nome traduzido não denuncia mais a origem: quem precisa
            # recusar envio para cá tem que saber agora.
            folder_is_system=nome_cru in self.PASTAS_DE_SISTEMA
            or nome_cru in self.PASTAS_OCULTAS,
            # Oculta some aqui, não na tela: assim nenhum caminho da API
            # devolve a `.internal` por engano.
            folders=[p for p in pastas if not p.hidden],
            files=[a for a in arquivos if not a.hidden],
        )

    def upload_file(
        self,
        patient_external_id: str,
        folder_external_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> None:
        data = {"ownerPatient": patient_external_id}
        if folder_external_id:
            data["parent"] = folder_external_id
        self.client.post_multipart(
            "FilesService/UploadMultiple",
            data=data,
            files=[("files", (filename, content, content_type))],
        )

    def rename_file(self, file_external_id: str, name: str) -> str:
        result = (
            self.client.post(
                "FileManagerService/Rename", {"id": file_external_id, "name": name}
            )
            or {}
        )
        # O servidor devolve o nome COM a extensão que ele preservou.
        return result.get("name") or name

    def delete_file(self, file_external_id: str) -> None:
        # ⚠️ É DELETE, não POST: com POST a vSaúde devolve 405 e a exclusão
        # nunca acontecia (achado ao vivo em 30/07/2026 — a captura da rota
        # dizia POST). Mesma pegadinha do `RemoveTag`, que é PUT.
        # Vai para a LIXEIRA do prontuário, não some: o item reaparece na
        # listagem com `deletedOnly: true`, com `isDeleted` e `deletionTime`.
        self.client.delete("FilesService/Delete", params={"id": file_external_id})
