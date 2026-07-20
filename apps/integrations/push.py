"""
Fila de write-back (§10.2) - nós → EHR, o outro sentido do espelho.

Padrão único de escrita: a mutação salva LOCAL primeiro (a UI responde na
hora) e, se a clínica tem EHR, `enqueue_push` grava uma SyncOperation e
dispara o processamento pós-commit. A task varre as operações PENDING da
clínica EM ORDEM (create antes de update) e cada handler traduz para o
port; quem conhece as rotas do provedor é só o adapter.

Regras de resiliência (RNF-3):
    - Erro transitório (EHR fora/limite): a operação volta a PENDING e a
      varredura PARA - o beat (1 min) retoma; após MAX_ATTEMPTS vira FAILED.
    - Erro permanente: FAILED na hora, `sync_status=FAILED` no registro
      local (visível na UI/painel de sync).
    - O payload guarda só a intenção (op/alvos); os DADOS saem do registro
      local NO MOMENTO do push - duas edições rápidas empurram o estado
      final, idempotente.
"""

import logging
import re

from django.db import transaction

from apps.core.choices import SyncStatus
from apps.integrations.choices import OperationStatus, SyncResource
from apps.integrations.ehr.exceptions import EHRRateLimitedError, EHRUnavailableError
from apps.integrations.ehr.registry import get_ehr_provider
from apps.integrations.models import SyncOperation

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8


def enqueue_push(clinic, resource_type: str, local_id: int, payload: dict):
    """
    Enfileira uma operação de push. Clínica sem EHR ou com a escrita
    DESLIGADA (`ehr_push_enabled=False`, fase de validação só-leitura) →
    None: a mutação local é o fim do fluxo. Dispara o push pós-commit.
    """
    if not clinic.ehr_provider or not clinic.ehr_push_enabled:
        return None
    operation = SyncOperation.objects.create(
        clinic=clinic,
        provider=clinic.ehr_provider,
        resource_type=resource_type,
        local_id=local_id,
        payload=payload,
    )
    from apps.integrations.tasks import push_sync_operations

    transaction.on_commit(lambda: push_sync_operations.delay(clinic.pk))
    return operation


def process_clinic_operations(clinic) -> dict:
    """Processa as PENDING da clínica em ordem de criação."""
    provider = get_ehr_provider(clinic)
    stats = {"succeeded": 0, "failed": 0, "deferred": 0}

    operations = SyncOperation.objects.filter(
        clinic=clinic, status=OperationStatus.PENDING
    ).order_by("created_at", "pk")

    for operation in operations:
        operation.status = OperationStatus.RUNNING
        operation.attempts += 1
        operation.save(update_fields=["status", "attempts", "updated_at"])
        try:
            HANDLERS[operation.resource_type](clinic, provider, operation)
        except (EHRRateLimitedError, EHRUnavailableError) as exc:
            if operation.attempts >= MAX_ATTEMPTS:
                _fail(operation, exc)
                stats["failed"] += 1
                continue
            operation.status = OperationStatus.PENDING
            operation.last_error = str(exc)[:2000]
            operation.save(update_fields=["status", "last_error", "updated_at"])
            stats["deferred"] += 1
            break  # EHR fora do ar: para a varredura; o beat retoma
        except Exception as exc:  # erro permanente → FAILED
            logger.exception(
                "Push %s#%s falhou para %s",
                operation.resource_type,
                operation.local_id,
                clinic.slug,
            )
            _fail(operation, exc)
            stats["failed"] += 1
        else:
            operation.status = OperationStatus.SUCCESS
            operation.last_error = ""
            operation.save(update_fields=["status", "last_error", "updated_at"])
            stats["succeeded"] += 1
    return stats


def _fail(operation, exc) -> None:
    operation.status = OperationStatus.FAILED
    operation.last_error = str(exc)[:2000]
    operation.save(update_fields=["status", "last_error", "updated_at"])
    _mark_local(operation, SyncStatus.FAILED)


def _mark_local(operation, sync_status: str) -> None:
    """Reflete o desfecho no registro local (selo da UI)."""
    from apps.patients.models import Patient
    from apps.scheduling.models import Appointment

    model = {
        SyncResource.PATIENT: Patient,
        SyncResource.PATIENT_TAGS: Patient,
        SyncResource.APPOINTMENT: Appointment,
    }.get(operation.resource_type)
    if model is None:
        return
    model.all_objects.filter(clinic=operation.clinic, pk=operation.local_id).update(
        sync_status=sync_status
    )


# --------------------------------------------------------------------- #
# Handlers - payload carrega a INTENÇÃO; os dados saem do registro local.
# --------------------------------------------------------------------- #


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _patient_data(patient) -> dict:
    """Formato NOSSO (chaves do model) - o adapter desnormaliza."""
    return {
        "name": patient.name,
        "cpf": patient.cpf,
        "birth_date": patient.birth_date.isoformat() if patient.birth_date else None,
        "gender": patient.gender,
        "email": patient.email,
        "phone": patient.phone,
        "city": patient.city,
        "state": patient.state,
        "address": patient.address or {},
        "profession": patient.profession,
        "comments_html": patient.comments_html,
    }


def _push_patient(clinic, provider, operation) -> None:
    from apps.patients.models import Patient

    payload = operation.payload or {}
    if payload.get("op") == "delete":
        external_id = payload.get("external_id") or ""
        if external_id:
            provider.delete_patient(external_id)
        return

    patient = Patient.objects.filter(clinic=clinic, pk=operation.local_id).first()
    if patient is None:
        return  # apagado localmente antes do push - a op de delete cuida do EHR

    data = _patient_data(patient)
    if not patient.external_id:
        # Dedupe por CPF antes de criar (§10.2): existe lá → vincula.
        match = None
        cpf = _digits(patient.cpf)
        if cpf:
            for candidate in provider.search_patients(cpf):
                if _digits(candidate.cpf) == cpf:
                    match = candidate
                    break
        if match is not None:
            patient.external_id = match.external_id
            provider.update_patient(patient.external_id, data)  # aplica nossas edições
        else:
            created = provider.create_patient(data)
            patient.external_id = created.external_id
        patient.sync_status = SyncStatus.SYNCED
        patient.save(update_fields=["external_id", "sync_status", "updated_at"])
    else:
        provider.update_patient(patient.external_id, data)
        if patient.sync_status != SyncStatus.SYNCED:
            patient.sync_status = SyncStatus.SYNCED
            patient.save(update_fields=["sync_status", "updated_at"])


def _push_patient_tags(clinic, provider, operation) -> None:
    """
    Diff de atribuições (§10.3): estado local desejado vs. estado atual no
    EHR (decodificado pelo catálogo) → AddTag/RemoveTag. Tags cujo
    identifier não conhecemos NÃO são tocadas.
    """
    from apps.patients.choices import TagSyncScope
    from apps.patients.models import Patient, Tag

    patient = Patient.objects.filter(clinic=clinic, pk=operation.local_id).first()
    if patient is None:
        return
    if not patient.external_id:
        # A op de create do paciente vem antes na fila; sem external_id aqui
        # significa que ela falhou - falha esta também (fica visível).
        raise ValueError("Paciente ainda sem external_id no EHR - crie antes das tags.")

    wanted = {
        assignment.tag.name for assignment in patient.patient_tags.select_related("tag").all()
    }

    current = set()
    fetched = provider.get_patient(patient.external_id)
    if fetched is not None:
        catalog = {
            int(tag.identifier): tag.name
            for tag in Tag.objects.filter(clinic=clinic, identifier__isnull=False)
        }
        for identifier, name in catalog.items():
            if fetched.tags_bitmask & identifier:
                current.add(name)

    for name in sorted(wanted - current):
        ehr_tag = provider.add_patient_tag(patient.external_id, name)
        if ehr_tag.identifier:
            # AddTag cria a tag por nome no EHR - promove a local a SYNCED.
            Tag.objects.filter(clinic=clinic, name=name).update(
                identifier=ehr_tag.identifier,
                external_id=ehr_tag.external_id,
                sync_scope=TagSyncScope.SYNCED,
            )
    for name in sorted(current - wanted):
        provider.remove_patient_tag(patient.external_id, name)

    if patient.sync_status != SyncStatus.SYNCED:
        patient.sync_status = SyncStatus.SYNCED
        patient.save(update_fields=["sync_status", "updated_at"])


def _appointment_data(appointment) -> dict:
    from apps.scheduling.models import PractitionerProcedure

    price = ""
    offer = PractitionerProcedure.objects.filter(
        practitioner=appointment.practitioner, procedure=appointment.procedure
    ).first()
    if offer and offer.price is not None:
        price = str(offer.price)
    return {
        "patient_external_id": appointment.patient.external_id,
        "practitioner_external_id": appointment.practitioner.external_id,
        "procedure_external_id": (
            appointment.procedure.external_id if appointment.procedure else ""
        ),
        "care_unit_external_id": (
            appointment.care_unit.external_id if appointment.care_unit else ""
        ),
        "insurance_company_external_id": (
            appointment.insurance_company.external_id if appointment.insurance_company else ""
        ),
        "insurance_plan_external_id": (
            appointment.insurance_plan.external_id if appointment.insurance_plan else ""
        ),
        "starts_at": appointment.starts_at.isoformat(),
        "duration_min": appointment.duration_min,
        "price": price,
        "remotely": appointment.remotely,
    }


def _map_status(clinic, source_status: str) -> str | None:
    from apps.scheduling.models import EHRStatusMap

    entry = EHRStatusMap.objects.filter(
        provider=clinic.ehr_provider, source_status=source_status
    ).first()
    return entry.status if entry else None


def _push_appointment(clinic, provider, operation) -> None:
    from apps.scheduling.models import Appointment

    payload = operation.payload or {}
    action = payload.get("op") or "update"

    if action == "delete":
        external_id = payload.get("external_id") or ""
        if external_id:
            provider.delete_appointment(external_id)
        return

    appointment = (
        Appointment.objects.filter(clinic=clinic, pk=operation.local_id)
        .select_related(
            "patient",
            "practitioner",
            "procedure",
            "care_unit",
            "insurance_company",
            "insurance_plan",
        )
        .first()
    )
    if appointment is None:
        return

    if not appointment.patient.external_id:
        raise ValueError("Paciente da consulta ainda sem external_id no EHR.")

    update_fields = ["sync_status", "updated_at"]
    if not appointment.external_id:
        created = provider.create_appointment(_appointment_data(appointment))
        appointment.external_id = created.external_id
        appointment.source_status = created.source_status
        update_fields += ["external_id", "source_status"]
    elif action == "transition":
        target = payload.get("target_status") or ""
        provider.transition_appointment(appointment.external_id, target)
        confirmed = provider.get_appointment(appointment.external_id)
        if confirmed is not None:
            appointment.source_status = confirmed.source_status
            update_fields.append("source_status")
            mapped = _map_status(clinic, confirmed.source_status)
            if mapped:  # sem mapa → mantém o status otimista local
                appointment.status = mapped
                update_fields.append("status")
    else:
        provider.update_appointment(appointment.external_id, _appointment_data(appointment))

    appointment.sync_status = SyncStatus.SYNCED
    appointment.save(update_fields=update_fields)


HANDLERS = {
    SyncResource.PATIENT: _push_patient,
    SyncResource.PATIENT_TAGS: _push_patient_tags,
    SyncResource.APPOINTMENT: _push_appointment,
}
