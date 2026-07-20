"""
API de integrações - disparo manual do sync com o EHR (§13).

Complementa, sem substituir, o agendamento automático do beat: o botão do
front enfileira os mesmos tasks `sync_clinic` da clínica ativa. O lock por
(kind, clinic) em Redis evita execuções sobrepostas com o pull agendado.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsClinicMember
from apps.core.context import resolve_active_membership

SYNC_KINDS = ["catalogs", "patients", "appointments"]


class EHRSyncView(APIView):
    """POST dispara um pull sob demanda (catalogs → patients → appointments)."""

    permission_classes = [IsClinicMember]

    def post(self, request):
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
