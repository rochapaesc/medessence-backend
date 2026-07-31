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
from apps.patients.models import (
    ClinicalEntry,
    ClinicalEntryKind,
    ClinicalOrigin,
    Patient,
    PrescriptionModel,
)
from apps.patients.partner_scope import eh_parceiro, pacientes_do_parceiro


class ClinicalEntryViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Linha do tempo clínica. Origem EHR = espelho READ-ONLY (o pull mantém);
    origem local = CRUD da clínica. `?patient=<id>` filtra; `POST /sync/`
    puxa o prontuário do paciente no EHR sob demanda (abrir a ficha).
    """

    #: O parceiro (RF-PAR-6) vê a linha do tempo, e só ela: nada de criar,
    #: editar ou apagar registro clínico.
    partner_allowed = {"list", "sync"}

    #: O que o parceiro pode ler. O recorte é do SERVIDOR: esconder nota e
    #: formulário só na tela deixaria o dado a um query param de distância.
    KINDS_DO_PARCEIRO = (ClinicalEntryKind.PRESCRIPTION, ClinicalEntryKind.EXAM)

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
        queryset = super().get_queryset().order_by("-date")
        if eh_parceiro(self.membership):
            # Três cercas, e nenhuma é redundante: o TIPO (só o que o médico
            # emitiu), o ESCOPO (só paciente atendido) e o PACIENTE
            # OBRIGATÓRIO. Sem a última, `/clinical-entries/` sem filtro
            # entregava o prontuário da clínica inteira, paginado - eram
            # 5.142 registros num pedido só.
            if self.action == "list" and not self.request.query_params.get("patient"):
                raise ValidationError(
                    {"patient": "Informe o paciente para ver o prontuário."}
                )
            queryset = queryset.filter(
                kind__in=self.KINDS_DO_PARCEIRO,
                patient__in=pacientes_do_parceiro(self.clinic),
            )
        return queryset

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
        pacientes = Patient.objects.filter(clinic=self.clinic)
        if eh_parceiro(self.membership):
            # Mesmo escopo da leitura: senão o parceiro mandava o servidor
            # buscar no EHR o prontuário de qualquer paciente da clínica.
            pacientes = pacientes.filter(pk__in=pacientes_do_parceiro(self.clinic))
        patient = pacientes.filter(pk=patient_id).first()
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
