from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin
from apps.patients.api.serializers.clinical import (
    ClinicalEntrySerializer,
    ClinicalEntryWriteSerializer,
    PrescriptionModelSerializer,
)
from apps.patients.models import ClinicalEntry, ClinicalOrigin, Patient, PrescriptionModel


class ClinicalEntryViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Linha do tempo clínica. Origem EHR = espelho READ-ONLY (o pull mantém);
    origem local = CRUD da clínica. `?patient=<id>` filtra; `POST /sync/`
    puxa o prontuário do paciente no EHR sob demanda (abrir a ficha).
    """

    model = ClinicalEntry
    audit_resource = "ClinicalEntry"
    serializer_class = ClinicalEntrySerializer
    select_related = ["practitioner"]
    ordering_fields = ["date", "created_at"]
    filterset_fields = ["patient", "kind", "origin"]

    action_serializer_classes = {
        "create": ClinicalEntryWriteSerializer,
        "update": ClinicalEntryWriteSerializer,
        "partial_update": ClinicalEntryWriteSerializer,
    }

    def get_queryset(self):
        return super().get_queryset().order_by("-date")

    def _block_ehr_mirror(self, instance):
        if instance.origin == ClinicalOrigin.EHR:
            raise PermissionDenied("Registro espelhado do prontuário (EHR) - somente leitura aqui.")

    def perform_update(self, serializer):
        self._block_ehr_mirror(serializer.instance)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._block_ehr_mirror(instance)
        super().perform_destroy(instance)

    @action(detail=False, methods=["post"], url_path="sync")
    def sync(self, request):
        """Pull do prontuário de UM paciente no EHR (sob demanda, síncrono)."""
        patient_id = request.data.get("patient")
        if not patient_id:
            raise ValidationError({"patient": "Informe o paciente."})
        patient = Patient.objects.filter(clinic=self.clinic, pk=patient_id).first()
        if patient is None:
            raise ValidationError({"patient": "Paciente não encontrado."})
        if not self.clinic.ehr_provider:
            return Response({"detail": "Clínica sem EHR - nada a sincronizar.", "synced": False})
        if not patient.external_id:
            return Response({"detail": "Paciente ainda não vinculado ao EHR.", "synced": False})

        from apps.integrations.services import pull_medical_records

        run = pull_medical_records(self.clinic, patient=patient)
        return Response(
            {"synced": True, "stats": run.stats},
            status=status.HTTP_200_OK,
        )


class PrescriptionModelViewSet(AuditMixin, ClinicScopedModelViewSet):
    """Modelos de prescrição - espelho EHR read-only + modelos locais."""

    model = PrescriptionModel
    audit_resource = "PrescriptionModel"
    serializer_class = PrescriptionModelSerializer
    ordering_fields = ["name"]

    def get_queryset(self):
        return super().get_queryset().order_by("name")

    def _block_ehr_mirror(self, instance):
        if instance.origin == ClinicalOrigin.EHR:
            raise PermissionDenied("Modelo espelhado do EHR - somente leitura aqui.")

    def perform_update(self, serializer):
        self._block_ehr_mirror(serializer.instance)
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._block_ehr_mirror(instance)
        super().perform_destroy(instance)
