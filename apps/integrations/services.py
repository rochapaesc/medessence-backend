"""
Motor de pull (§10.2) — EHR → nós.

Cada pull é idempotente (upsert por `(clinic, external_id)`) e grava um
`SyncRun` com estatísticas, visível no admin e no futuro painel de sync.
O provedor entra pelo registry — este módulo não conhece vSaúde.

Propriedade por campo (§10.1): demográficos e agenda têm o EHR como dono —
o pull SOBRESCREVE o local. Tags são bidirecionais: o diff só toca nas
atribuições espelhadas (origin=EHR), preservando as locais.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from apps.integrations.choices import SyncRunKind
from apps.integrations.ehr.registry import get_ehr_provider
from apps.integrations.models import SyncRun
from apps.patients.choices import TagOrigin, TagSyncScope
from apps.patients.models import Contact, Patient, PatientContact, PatientTag, Tag
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import (
    Appointment,
    CareUnit,
    EHRStatusMap,
    InsuranceCompany,
    InsurancePlan,
    Practitioner,
    Procedure,
)

logger = logging.getLogger(__name__)

APPOINTMENTS_WINDOW_PAST = timedelta(days=7)  # janela deslizante D-7 → D+60
APPOINTMENTS_WINDOW_FUTURE = timedelta(days=60)


def run_sync(clinic, kind: str, executor) -> SyncRun:
    """Envolve um pull com o registro de SyncRun (início/fim/stats/erro)."""
    run = SyncRun.objects.create(clinic=clinic, kind=kind, started_at=timezone.now())
    try:
        stats = executor() or {}
        run.stats = stats
    except Exception as exc:
        run.error = str(exc)[:2000]
        logger.exception("Sync %s falhou para %s", kind, clinic.slug)
        raise
    finally:
        run.finished_at = timezone.now()
        run.save(update_fields=["stats", "error", "finished_at", "updated_at"])
    return run


# --------------------------------------------------------------------- #
# Catálogos (tags, procedimentos, unidades, convênios)
# --------------------------------------------------------------------- #


def pull_catalogs(clinic) -> SyncRun:
    provider = get_ehr_provider(clinic)

    def _execute():
        stats = {}
        stats["tags"] = _upsert_tags(clinic, provider.list_tags())
        stats["procedures"] = _upsert_simple(
            clinic,
            Procedure,
            provider.list_procedures(),
            lambda item: {
                "name": item.name,
                "duration_min": item.duration_min,
                "remotely": item.remotely,
            },
        )
        stats["care_units"] = _upsert_simple(
            clinic,
            CareUnit,
            provider.list_care_units(),
            lambda item: {"name": item.name},
        )
        stats["insurances"] = _upsert_insurances(clinic, provider.list_insurance_companies())
        return stats

    return run_sync(clinic, SyncRunKind.CATALOGS, _execute)


def _upsert_simple(clinic, model, items, defaults_fn) -> dict:
    created = updated = 0
    for item in items:
        _, was_created = model.objects.update_or_create(
            clinic=clinic,
            external_id=item.external_id,
            defaults=defaults_fn(item),
        )
        created += was_created
        updated += not was_created
    return {"fetched": len(items), "created": created, "updated": updated}


def _upsert_tags(clinic, ehr_tags) -> dict:
    """
    Upsert do catálogo por `identifier` (§10.3). Tag removida no EHR vira
    LOCAL_ONLY aqui — as atribuições existentes são preservadas.
    """
    created = updated = 0
    seen_identifiers = set()
    for ehr_tag in ehr_tags:
        seen_identifiers.add(ehr_tag.identifier)
        tag = Tag.objects.filter(clinic=clinic, identifier=ehr_tag.identifier).first()
        if tag is None:
            # Tag local com o mesmo nome? Vincula em vez de duplicar.
            tag = Tag.objects.filter(clinic=clinic, name=ehr_tag.name).first()
        if tag is None:
            Tag.objects.create(
                clinic=clinic,
                name=ehr_tag.name,
                external_id=ehr_tag.external_id,
                identifier=ehr_tag.identifier,
                sync_scope=TagSyncScope.SYNCED,
            )
            created += 1
        else:
            tag.name = ehr_tag.name
            tag.external_id = ehr_tag.external_id
            tag.identifier = ehr_tag.identifier
            tag.sync_scope = TagSyncScope.SYNCED
            tag.save(
                update_fields=["name", "external_id", "identifier", "sync_scope", "updated_at"]
            )
            updated += 1

    demoted = (
        Tag.objects.filter(clinic=clinic, sync_scope=TagSyncScope.SYNCED)
        .exclude(identifier__in=seen_identifiers)
        .update(sync_scope=TagSyncScope.LOCAL_ONLY)
    )
    return {
        "fetched": len(ehr_tags),
        "created": created,
        "updated": updated,
        "demoted_local_only": demoted,
    }


def _upsert_insurances(clinic, companies) -> dict:
    created = updated = 0
    for company in companies:
        obj, was_created = InsuranceCompany.objects.update_or_create(
            clinic=clinic,
            external_id=company.external_id,
            defaults={"name": company.name},
        )
        created += was_created
        updated += not was_created
        for plan in company.plans:
            InsurancePlan.objects.update_or_create(
                clinic=clinic,
                external_id=plan.external_id,
                defaults={"name": plan.name, "company": obj},
            )
    return {"fetched": len(companies), "created": created, "updated": updated}


# --------------------------------------------------------------------- #
# Pacientes
# --------------------------------------------------------------------- #


def pull_patients(clinic) -> SyncRun:
    provider = get_ehr_provider(clinic)

    def _execute():
        stats = {"fetched": 0, "created": 0, "updated": 0}
        page = 1
        while True:
            result = provider.list_patients(page)
            if not result.items:
                break
            for ehr_patient in result.items:
                _, was_created = upsert_patient(clinic, ehr_patient)
                stats["fetched"] += 1
                stats["created"] += was_created
                stats["updated"] += not was_created
            if stats["fetched"] >= result.total_count:
                break
            page += 1
        return stats

    return run_sync(clinic, SyncRunKind.PATIENTS_FULL, _execute)


def upsert_patient(clinic, ehr_patient) -> tuple[Patient, bool]:
    """Upsert por (clinic, external_id) — o EHR é o dono dos demográficos."""
    from apps.patients.choices import PatientSource

    patient, created = Patient.all_objects.update_or_create(
        clinic=clinic,
        external_id=ehr_patient.external_id,
        defaults={
            "name": ehr_patient.name,
            "cpf": ehr_patient.cpf,
            "birth_date": ehr_patient.birth_date,
            "gender": ehr_patient.gender,
            "email": ehr_patient.email,
            "phone": ehr_patient.phone,
            "city": ehr_patient.city,
            "state": ehr_patient.state,
            "address": ehr_patient.address,
            "profession": ehr_patient.profession,
            "comments_html": ehr_patient.comments_html,
            "insurance_name": ehr_patient.insurance_name,
            "source": PatientSource.EHR,
            "raw_payload": ehr_patient.raw,
            "deleted_at": None,  # reaparecer no EHR restaura o espelho
        },
    )
    _link_contact(clinic, patient)
    _sync_ehr_tag_assignments(clinic, patient, ehr_patient.tags_bitmask)
    return patient, created


def _link_contact(clinic, patient) -> None:
    """Vincula o Contact pelo telefone (RF-PAC-7): N pacientes por número."""
    if not patient.phone:
        return
    contact, _ = Contact.objects.get_or_create(
        clinic=clinic,
        wa_id=patient.phone,
        defaults={"display_name": patient.name.split(" ")[0]},
    )
    already = PatientContact.objects.filter(patient=patient, contact=contact).exists()
    if not already:
        has_primary = PatientContact.objects.filter(contact=contact, is_primary=True).exists()
        PatientContact.objects.create(
            patient=patient,
            contact=contact,
            is_primary=not has_primary,  # primeiro paciente do número vira o principal
        )


def _sync_ehr_tag_assignments(clinic, patient, bitmask: int) -> None:
    """
    Diff entre o bitmask recebido e as PatientTag(origin=EHR) (§10.3):
    cria/remove espelhadas, sem tocar nas locais nem nas de tags LOCAL_ONLY.
    """
    catalog = {
        int(tag.identifier): tag
        for tag in Tag.objects.filter(clinic=clinic, identifier__isnull=False)
    }
    wanted_tag_ids = {tag.pk for identifier, tag in catalog.items() if bitmask & identifier}

    assignments = {pt.tag_id: pt for pt in patient.patient_tags.select_related("tag")}

    for tag_id in wanted_tag_ids - set(assignments):
        PatientTag.objects.create(patient=patient, tag_id=tag_id, origin=TagOrigin.EHR)

    for tag_id, assignment in assignments.items():
        if (
            assignment.origin == TagOrigin.EHR
            and tag_id not in wanted_tag_ids
            and assignment.tag.sync_scope != TagSyncScope.LOCAL_ONLY
        ):
            assignment.delete()  # soft


# --------------------------------------------------------------------- #
# Agenda
# --------------------------------------------------------------------- #


def pull_appointments(clinic, start=None, end=None) -> SyncRun:
    provider = get_ehr_provider(clinic)
    today = timezone.now().date()
    start = start or (today - APPOINTMENTS_WINDOW_PAST)
    end = end or (today + APPOINTMENTS_WINDOW_FUTURE)

    def _execute():
        status_map = {
            entry.source_status: entry.status
            for entry in EHRStatusMap.objects.filter(provider=clinic.ehr_provider)
        }
        stats = {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "patients_fetched": 0,
            "unmapped_statuses": [],
        }

        for ehr_appointment in provider.list_appointments(start, end):
            stats["fetched"] += 1
            patient = _resolve_patient(clinic, provider, ehr_appointment.patient_external_id, stats)
            if patient is None:
                continue

            if ehr_appointment.practitioner_name:
                # A vSaúde embute o profissional na agenda — o pull mantém
                # nome/registro atualizados (EHR é o dono).
                practitioner, _ = Practitioner.objects.update_or_create(
                    clinic=clinic,
                    external_id=ehr_appointment.practitioner_external_id,
                    defaults={
                        "name": ehr_appointment.practitioner_name,
                        "license_number": ehr_appointment.practitioner_license,
                    },
                )
            else:
                practitioner, _ = Practitioner.objects.get_or_create(
                    clinic=clinic,
                    external_id=ehr_appointment.practitioner_external_id,
                    defaults={
                        "name": f"Profissional {ehr_appointment.practitioner_external_id[:8]}"
                    },
                )

            status = status_map.get(ehr_appointment.source_status)
            if status is None:
                if ehr_appointment.source_status not in stats["unmapped_statuses"]:
                    stats["unmapped_statuses"].append(ehr_appointment.source_status)
                status = AppointmentStatus.SCHEDULED  # P4: calibrar EHRStatusMap

            duration = ehr_appointment.duration_min or 30
            if not ehr_appointment.duration_min and ehr_appointment.ends_at:
                delta = ehr_appointment.ends_at - ehr_appointment.starts_at
                duration = max(int(delta.total_seconds() // 60), 5)

            _, was_created = Appointment.all_objects.update_or_create(
                clinic=clinic,
                external_id=ehr_appointment.external_id,
                defaults={
                    "patient": patient,
                    "practitioner": practitioner,
                    "care_unit": _catalog_from_embed(
                        CareUnit,
                        clinic,
                        ehr_appointment.care_unit_external_id,
                        ehr_appointment.care_unit_name,
                    ),
                    "procedure": _catalog_from_embed(
                        Procedure,
                        clinic,
                        ehr_appointment.procedure_external_id,
                        ehr_appointment.procedure_name,
                    ),
                    "insurance_company": _catalog_from_embed(
                        InsuranceCompany,
                        clinic,
                        ehr_appointment.insurance_company_external_id,
                        ehr_appointment.insurance_company_name,
                    ),
                    "insurance_plan": _insurance_plan_from_embed(clinic, ehr_appointment),
                    "starts_at": ehr_appointment.starts_at,
                    "duration_min": duration,
                    "status": status,
                    "source_status": ehr_appointment.source_status,
                    "remotely": ehr_appointment.remotely,
                    "raw_payload": ehr_appointment.raw,
                    "deleted_at": None,
                },
            )
            stats["created"] += was_created
            stats["updated"] += not was_created

        return stats

    return run_sync(clinic, SyncRunKind.APPOINTMENTS, _execute)


def _resolve_patient(clinic, provider, external_id, stats):
    """Consulta referencia paciente que ainda não temos → refresh pontual."""
    patient = Patient.objects.filter(clinic=clinic, external_id=external_id).first()
    if patient is not None:
        return patient
    ehr_patient = provider.get_patient(external_id)
    if ehr_patient is None:
        stats.setdefault("missing_patients", []).append(external_id)
        return None
    patient, _ = upsert_patient(clinic, ehr_patient)
    stats["patients_fetched"] += 1
    return patient


def _by_external(model, clinic, external_id):
    if not external_id:
        return None
    return model.objects.filter(clinic=clinic, external_id=external_id).first()


def _catalog_from_embed(model, clinic, external_id, name):
    """
    Resolve item de catálogo referenciado na agenda. Com nome embutido
    (vSaúde), faz upsert — catálogo se mantém vivo mesmo sem pull dedicado.
    """
    if not external_id:
        return None
    if name:
        obj, _ = model.objects.update_or_create(
            clinic=clinic, external_id=external_id, defaults={"name": name}
        )
        return obj
    return _by_external(model, clinic, external_id)


def _insurance_plan_from_embed(clinic, ehr_appointment):
    if not ehr_appointment.insurance_plan_external_id:
        return None
    company = _catalog_from_embed(
        InsuranceCompany,
        clinic,
        ehr_appointment.insurance_company_external_id,
        ehr_appointment.insurance_company_name,
    )
    if ehr_appointment.insurance_plan_name and company is not None:
        plan, _ = InsurancePlan.objects.update_or_create(
            clinic=clinic,
            external_id=ehr_appointment.insurance_plan_external_id,
            defaults={"name": ehr_appointment.insurance_plan_name, "company": company},
        )
        return plan
    return _by_external(InsurancePlan, clinic, ehr_appointment.insurance_plan_external_id)


# --------------------------------------------------------------------- #

PULLS = {
    "catalogs": pull_catalogs,
    "patients": pull_patients,
    "appointments": pull_appointments,
}


def pull_all(clinic) -> list[SyncRun]:
    """Ordem importa: catálogos antes (tags p/ bitmask), agenda por último."""
    return [pull_catalogs(clinic), pull_patients(clinic), pull_appointments(clinic)]
