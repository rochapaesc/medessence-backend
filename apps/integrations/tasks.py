"""
Jobs de sync (§13) — fan-out por tenant com lock em Redis.

O beat dispara os `schedule_*` (globais), que enfileiram um `sync_clinic`
POR clínica com EHR configurado — `clinic_id` sempre explícito (§3.1).
O lock por (kind, clinic) impede execuções sobrepostas do mesmo pull.
"""

import logging
from contextlib import suppress

from celery import shared_task
from django.core.cache import cache

from apps.integrations.ehr.exceptions import EHRRateLimitedError, EHRUnavailableError

logger = logging.getLogger(__name__)

LOCK_TTL = 60 * 20  # 20 min — maior que qualquer pull razoável


def _acquire_lock(kind: str, clinic_id: int):
    """Lock não-bloqueante via django-redis; sem suporte (testes) → segue sem lock."""
    lock_factory = getattr(cache, "lock", None)
    if lock_factory is None:
        return None
    lock = lock_factory(f"lock:sync:{kind}:{clinic_id}", timeout=LOCK_TTL, blocking_timeout=0)
    return lock if lock.acquire() else False


@shared_task(
    queue="sync",
    autoretry_for=(EHRRateLimitedError, EHRUnavailableError),
    retry_backoff=60,
    retry_backoff_max=60 * 30,
    max_retries=5,
)
def sync_clinic(clinic_id: int, kind: str):
    """Executa um pull (catalogs/patients/appointments) para UMA clínica."""
    from apps.integrations.services import PULLS
    from apps.tenants.models import Clinic

    clinic = Clinic.objects.filter(pk=clinic_id).first()
    if clinic is None or not clinic.ehr_provider:
        return "skipped: clínica sem EHR"

    lock = _acquire_lock(kind, clinic_id)
    if lock is False:
        logger.info("Sync %s de %s já em execução — pulando.", kind, clinic.slug)
        return "skipped: locked"

    try:
        run = PULLS[kind](clinic)
        return {"run_id": run.pk, "stats": run.stats}
    finally:
        if lock:
            with suppress(Exception):
                lock.release()


@shared_task(queue="sync")
def schedule_appointment_syncs():
    """Beat (10 min): pull da agenda de toda clínica integrada."""
    for clinic_id in _integrated_clinic_ids():
        sync_clinic.delay(clinic_id, "appointments")


@shared_task(queue="sync")
def schedule_daily_syncs():
    """Beat (diário, madrugada): catálogos + varredura completa de pacientes."""
    for clinic_id in _integrated_clinic_ids():
        sync_clinic.delay(clinic_id, "catalogs")
        sync_clinic.delay(clinic_id, "patients")


def _integrated_clinic_ids():
    from apps.tenants.models import Clinic

    return Clinic.objects.exclude(ehr_provider="").values_list("pk", flat=True)
