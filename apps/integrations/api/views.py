"""
API de integrações - sincronização com o EHR (§13).

- POST /sync/ehr/  : dispara um pull sob demanda (complementa o beat).
- GET  /sync/ehr/  : estado da última sincronização por tipo (para o front
                     mostrar "sincronizado há X", progresso e resultado).

O botão do front enfileira os mesmos tasks `sync_clinic` da clínica ativa; o
lock por (kind, clinic) em Redis evita execuções sobrepostas com o agendado.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsClinicMember
from apps.core.context import resolve_active_membership
from apps.integrations.choices import SyncRunKind
from apps.integrations.models import SyncRun

# Tipos disparados pelo botão (task kind → SyncRun.kind persistido).
SYNC_KINDS = ["catalogs", "patients", "appointments"]
STATUS_KINDS = [
    SyncRunKind.CATALOGS,
    SyncRunKind.PATIENTS_FULL,
    SyncRunKind.APPOINTMENTS,
]


def _run_state(run: SyncRun | None) -> str:
    if run is None:
        return "never"
    if run.error:
        return "failed"
    if run.finished_at:
        return "success"
    if run.started_at:
        return "running"
    return "pending"


def _serialize(kind, run: SyncRun | None) -> dict:
    return {
        "kind": kind.value,
        "label": kind.label,
        "state": _run_state(run),
        "started_at": run.started_at if run else None,
        "finished_at": run.finished_at if run else None,
        "stats": run.stats if run else {},
        "error": (run.error if run else "") or "",
    }


class EHRSyncView(APIView):
    permission_classes = [IsClinicMember]

    def post(self, request):
        """Dispara um pull sob demanda (catalogs → patients → appointments)."""
        clinic = resolve_active_membership(request).clinic
        if not clinic.ehr_provider:
            return Response(
                {"detail": "Esta clínica não tem um EHR configurado."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from apps.integrations.tasks import sync_clinic

        for kind in SYNC_KINDS:
            sync_clinic.delay(clinic.id, kind)

        return Response(
            {"detail": "Sincronização com o EHR iniciada.", "kinds": SYNC_KINDS},
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request):
        """Última execução por tipo + resumo (para o front acompanhar)."""
        clinic = resolve_active_membership(request).clinic

        runs = []
        latest_by_kind = {}
        for kind in STATUS_KINDS:
            run = (
                SyncRun.objects.filter(clinic=clinic, kind=kind)
                .order_by("-started_at")
                .first()
            )
            latest_by_kind[kind] = run
            runs.append(_serialize(kind, run))

        finished = [r.finished_at for r in latest_by_kind.values() if r and r.finished_at]
        running = any(_run_state(r) == "running" for r in latest_by_kind.values())

        return Response(
            {
                "ehr_configured": bool(clinic.ehr_provider),
                "running": running,
                "last_synced_at": max(finished) if finished else None,
                "runs": runs,
            }
        )
