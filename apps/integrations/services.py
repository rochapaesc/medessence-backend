"""
Motor de pull (§10.2) - EHR → nós.

Cada pull é idempotente (upsert por `(clinic, external_id)`) e grava um
`SyncRun` com estatísticas, visível no admin e no futuro painel de sync.
O provedor entra pelo registry - este módulo não conhece vSaúde.

Propriedade por campo (§10.1): demográficos e agenda têm o EHR como dono -
o pull SOBRESCREVE o local. Tags são bidirecionais: o diff só toca nas
atribuições espelhadas (origin=EHR), preservando as locais.
"""

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.integrations.choices import SyncRunKind
from apps.integrations.ehr.registry import get_ehr_provider
from apps.integrations.models import SyncRun
from apps.patients.choices import TagOrigin, TagSyncScope
from apps.patients.models import (
    ClinicalEntry,
    ClinicalOrigin,
    Contact,
    Patient,
    PatientContact,
    PatientTag,
    PrescriptionModel,
    Tag,
)
from apps.scheduling.choices import PRE_ATTENDANCE_STATUSES, AppointmentStatus
from apps.scheduling.models import (
    Appointment,
    CareUnit,
    EHRStatusMap,
    InsuranceCompany,
    InsurancePlan,
    Practitioner,
    PractitionerProcedure,
    Procedure,
)

logger = logging.getLogger(__name__)

APPOINTMENTS_WINDOW_PAST = timedelta(days=7)  # janela deslizante D-7 → D+60
APPOINTMENTS_WINDOW_FUTURE = timedelta(days=60)
# Piso do backfill da agenda: até quão para trás o histórico é trazido. Como o
# backfill agora é RESUMÍVEL (um pedaço por sync, marca-d'água em Clinic), pode
# ser generoso sem estourar o rate limit - cada execução recua só BACKFILL_CHUNK.
APPOINTMENTS_BACKFILL = timedelta(days=365 * 3)
BACKFILL_CHUNK = timedelta(days=90)  # quanto a agenda recua a cada sincronização


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
            lambda item: {
                "name": item.name,
                "address": item.address,
                "work_journey": item.work_journey,
            },
        )
        stats["insurances"] = _upsert_insurances(clinic, provider.list_insurance_companies())
        stats["professionals"] = _upsert_professionals(clinic, provider)
        stats["prescription_models"] = _upsert_prescription_models(clinic, provider)
        return stats

    return run_sync(clinic, SyncRunKind.CATALOGS, _execute)


def _upsert_prescription_models(clinic, provider) -> dict:
    created = updated = 0
    models = provider.list_prescription_models()
    for model in models:
        _, was_created = PrescriptionModel.all_objects.update_or_create(
            clinic=clinic,
            external_id=model.external_id,
            defaults={
                "name": model.name,
                "content": model.content,
                "hint": model.hint,
                "medications": model.medications,
                "smart": model.smart,
                "special_prescription": model.special_prescription,
                "origin": ClinicalOrigin.EHR,
                "deleted_at": None,
            },
        )
        created += was_created
        updated += not was_created
    return {"fetched": len(models), "created": created, "updated": updated}


def _upsert_professionals(clinic, provider) -> dict:
    """
    Pull dedicado de profissionais (inclui quem nunca atendeu) + os
    procedimentos que cada um OFERECE (duração/preço → form Nova consulta).
    O CRM não vem neste GetAll - o pull da agenda mantém o license_number.
    """
    created = updated = offers = 0
    professionals = provider.list_professionals()
    for professional in professionals:
        record, was_created = Practitioner.objects.update_or_create(
            clinic=clinic,
            external_id=professional.external_id,
            defaults={"name": professional.name},
        )
        created += was_created
        updated += not was_created

        for offer in provider.list_professional_procedures(professional.external_id):
            procedure = _by_external(Procedure, clinic, offer.procedure_external_id)
            if procedure is None:
                procedure, _ = Procedure.objects.update_or_create(
                    clinic=clinic,
                    external_id=offer.procedure_external_id,
                    defaults={"name": offer.name},
                )
            PractitionerProcedure.objects.update_or_create(
                clinic=clinic,
                practitioner=record,
                procedure=procedure,
                defaults={
                    "duration_min": offer.duration_min,
                    "price": offer.price or None,
                    "description": offer.description,
                    "comments": offer.comments,
                    "allow_online": offer.allow_online,
                    "is_active": offer.is_active,
                },
            )
            offers += 1
    return {
        "fetched": len(professionals),
        "created": created,
        "updated": updated,
        "offers": offers,
    }


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
    LOCAL_ONLY aqui - as atribuições existentes são preservadas.
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
        seen_external_ids = set()
        page = 1
        while True:
            result = provider.list_patients(page)
            if not result.items:
                break
            for ehr_patient in result.items:
                _, was_created = upsert_patient(clinic, ehr_patient)
                seen_external_ids.add(ehr_patient.external_id)
                stats["fetched"] += 1
                stats["created"] += was_created
                stats["updated"] += not was_created
            if stats["fetched"] >= result.total_count:
                break
            page += 1

        # Diff de varredura (delete bidirecional, §10.1): espelhado que NÃO
        # veio na varredura completa foi apagado no EHR → soft delete local.
        # Só roda se a varredura terminou sem erro (estamos aqui) e trouxe
        # algo (uma resposta vazia de API instável não pode apagar a base).
        if seen_external_ids:
            from apps.patients.choices import PatientSource

            removed = 0
            missing = (
                Patient.objects.filter(clinic=clinic, source=PatientSource.EHR)
                .exclude(external_id="")
                .exclude(external_id__in=seen_external_ids)
            )
            for patient in missing:
                patient.delete()  # soft - reaparecer no EHR restaura (upsert)
                removed += 1
            stats["removed"] = removed
        return stats

    return run_sync(clinic, SyncRunKind.PATIENTS_FULL, _execute)


def upsert_patient(clinic, ehr_patient) -> tuple[Patient, bool]:
    """Upsert por (clinic, external_id) - o EHR é o dono dos demográficos."""
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
            "blood_type": ehr_patient.blood_type,
            "weight_kg": ehr_patient.weight_kg or None,
            "height_cm": ehr_patient.height_cm or None,
            "guardians": ehr_patient.guardians,
            "emergency_contacts": ehr_patient.emergency_contacts,
            "birth_info": ehr_patient.birth_info,
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
    """
    Vincula o Contact pelo telefone (RF-PAC-7): N pacientes por número.

    A resolução do número e a regra do principal vivem em `patients.vinculos`
    — antes estavam copiadas aqui, e este caminho criava o contato pela grafia
    CRUA do EHR: um número sem o nono dígito virava um segundo contato do
    mesmo celular (§6.2).
    """
    from apps.patients.vinculos import contato_do_numero, vincular

    if not patient.phone:
        return
    contact, _ = contato_do_numero(
        clinic, patient.phone, display_name=patient.name.split(" ")[0]
    )
    if contact is not None:
        vincular(patient, contact)


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

    # Janelas desta execução. Chamada manual (start/end) = janela única. Automática
    # = janela deslizante D-7→D+60 (sempre) + UM pedaço de backfill resumível:
    # recua BACKFILL_CHUNK a partir da marca-d'água e grava o avanço, até o piso
    # APPOINTMENTS_BACKFILL. Assim o histórico entra aos poucos, sem rajada de 429.
    backfill_to = None
    if start is not None or end is not None:
        windows = [
            (
                start or (today - APPOINTMENTS_WINDOW_PAST),
                end or (today + APPOINTMENTS_WINDOW_FUTURE),
            )
        ]
    else:
        windows = [
            (today - APPOINTMENTS_WINDOW_PAST, today + APPOINTMENTS_WINDOW_FUTURE)
        ]
        floor = today - APPOINTMENTS_BACKFILL
        watermark = clinic.appointments_backfilled_until or (
            today - APPOINTMENTS_WINDOW_PAST
        )
        if watermark > floor:
            chunk_start = max(floor, watermark - BACKFILL_CHUNK)
            windows.append((chunk_start, watermark))
            backfill_to = chunk_start

    def _execute():
        status_map = {
            entry.source_status: entry.status
            for entry in EHRStatusMap.objects.filter(provider=clinic.ehr_provider)
        }
        # Guarda anti-regressão (RF-AGE-5): "Em atendimento" é LOCAL-only e o
        # EHR nunca fica sabendo dele - o upsert não pode devolver essas
        # consultas para aguardando/confirmada/agendada. Conjunto minúsculo:
        # só as consultas EM ATENDIMENTO agora.
        in_progress_ids = set(
            Appointment.all_objects.filter(
                clinic=clinic, status=AppointmentStatus.IN_PROGRESS
            ).values_list("external_id", flat=True)
        )
        stats = {
            "fetched": 0,
            "created": 0,
            "updated": 0,
            "patients_fetched": 0,
            "unmapped_statuses": [],
        }

        def _iter_windows():
            # Dedup por external_id: as janelas podem se tocar no boundary
            # (chunk termina onde a janela deslizante começa).
            seen: set[str] = set()
            for w_start, w_end in windows:
                for appt in provider.list_appointments(w_start, w_end):
                    if appt.external_id in seen:
                        continue
                    seen.add(appt.external_id)
                    yield appt

        for ehr_appointment in _iter_windows():
            stats["fetched"] += 1
            patient = _resolve_patient(clinic, provider, ehr_appointment.patient_external_id, stats)
            if patient is None:
                continue

            if ehr_appointment.practitioner_name:
                # A vSaúde embute o profissional na agenda - o pull mantém
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
                status = AppointmentStatus.SCHEDULED  # sem mapa → calibrar via admin
            if (
                ehr_appointment.external_id in in_progress_ids
                and status in PRE_ATTENDANCE_STATUSES
            ):
                status = AppointmentStatus.IN_PROGRESS

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
                    "price": ehr_appointment.price or None,
                    "comments_html": ehr_appointment.comments_html,
                    "raw_payload": ehr_appointment.raw,
                    "deleted_at": None,
                },
            )
            stats["created"] += was_created
            stats["updated"] += not was_created

        if backfill_to is not None:
            clinic.appointments_backfilled_until = backfill_to
            clinic.save(
                update_fields=["appointments_backfilled_until", "updated_at"]
            )
            stats["backfilled_until"] = backfill_to.isoformat()

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
    (vSaúde), faz upsert - catálogo se mantém vivo mesmo sem pull dedicado.
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
# Espelho clínico (docs/vsaude-clinico.md) - somente leitura (§10.1)
# --------------------------------------------------------------------- #

MEDICAL_RECORDS_SWEEP_LIMIT = 200  # varredura de fundo: prioriza os mais quentes


def pull_medical_records(clinic, patient=None, patients=None) -> SyncRun:
    """
    Linha do tempo clínica: sob demanda (`patient`) ao abrir a ficha,
    `patients` para um lote dirigido (a conferência da tela Parceiros,
    RF-PAR-4), ou varredura priorizando pacientes com consulta recente/futura
    (limite por execução - a fila anda a cada rodada).
    """
    provider = get_ehr_provider(clinic)

    def _execute():
        stats = {"patients": 0, "fetched": 0, "created": 0, "updated": 0}
        if patient is not None:
            targets = [patient]
        elif patients is not None:
            targets = [p for p in patients if p.external_id]
            # Quem foi conferido fica no run: é o que deixa a tela Parceiros
            # saber que um PERÍODO NOVO ainda não foi coberto, sem derrubar a
            # trava de reentrada dos 5 minutos.
            stats["patient_ids"] = [p.pk for p in targets]
        else:
            cutoff = timezone.now() - timedelta(days=90)
            targets = list(
                Patient.objects.filter(clinic=clinic)
                .exclude(external_id="")
                # consulta recente OU retorno marcado - os "mais quentes"
                .filter(Q(last_appointment_at__gte=cutoff) | Q(next_appointment_at__isnull=False))
                .order_by("-last_appointment_at")[:MEDICAL_RECORDS_SWEEP_LIMIT]
            )

        for target in targets:
            entries = provider.get_clinical_entries(target.external_id)
            stats["patients"] += 1
            for entry in entries:
                practitioner = None
                if entry.creator_external_id:
                    practitioner = Practitioner.objects.filter(
                        clinic=clinic, external_id=entry.creator_external_id
                    ).first()
                _, was_created = ClinicalEntry.all_objects.update_or_create(
                    clinic=clinic,
                    source=entry.source,
                    external_id=entry.external_id,
                    defaults={
                        "patient": target,
                        "practitioner": practitioner,
                        "kind": entry.kind,
                        "origin": ClinicalOrigin.EHR,
                        "date": entry.date or timezone.now(),
                        "text": entry.text,
                        "title": entry.title,
                        "description": entry.description,
                        "document_url": entry.document_url,
                        "form_answers": entry.form_answers,
                        "creator_name": entry.creator_name,
                        "creator_license": entry.creator_license,
                        "raw_payload": entry.raw,
                        "deleted_at": None,
                    },
                )
                stats["fetched"] += 1
                stats["created"] += was_created
                stats["updated"] += not was_created
        return stats

    return run_sync(clinic, SyncRunKind.MEDICAL_RECORDS, _execute)


# --------------------------------------------------------------------- #

PULLS = {
    "catalogs": pull_catalogs,
    "patients": pull_patients,
    "appointments": pull_appointments,
    "medical_records": pull_medical_records,
}


def pull_all(clinic) -> list[SyncRun]:
    """Ordem importa: catálogos antes (tags p/ bitmask), agenda por último."""
    return [pull_catalogs(clinic), pull_patients(clinic), pull_appointments(clinic)]
