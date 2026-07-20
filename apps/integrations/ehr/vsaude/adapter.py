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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from django.utils import timezone as dj_timezone

from apps.core.html import sanitize_html
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
from apps.integrations.ehr.vsaude.client import PAGE_SIZE, VSaudeClient
from apps.patients.choices import Gender

GENDER_MAP = {0: Gender.UNKNOWN, 1: Gender.MALE, 2: Gender.FEMALE}

# AppointmentsQueryRange (swagger): Daily=1, Weekly=2, Monthly=3, NowOn=4.
# Monthly retorna o MÊS-CALENDÁRIO que contém o `from`.
RANGE_MONTHLY = 3

# Só consultas médicas entram no espelho (o GetAll pode trazer outros tipos)
APPOINTMENT_DISCRIMINATOR = "MedicalAppointment"


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

    def _normalize_patient(self, payload: dict) -> EHRPatient:
        address = payload.get("address") or {}
        insurance = payload.get("insurance") or {}
        tag_identifiers = payload.get("tags") or []
        bitmask = 0
        for identifier in tag_identifiers:
            bitmask |= int(identifier)
        # Limites dos campos do model - dado real extrapola contrato observado
        return EHRPatient(
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
            remotely=bool(payload.get("remotely", False)),
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
        while cursor <= end:
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
            )
            for item in items
            if not item.get("isDeleted", False)
        ]

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
