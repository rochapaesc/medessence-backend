from rest_framework.serializers import CharField, ModelSerializer, ValidationError

from apps.scheduling.models import Appointment


class AppointmentReadSerializer(ModelSerializer):
    patient_name = CharField(source="patient.name", read_only=True)
    practitioner_name = CharField(source="practitioner.name", read_only=True)
    care_unit_name = CharField(source="care_unit.name", read_only=True, default=None)
    procedure_name = CharField(source="procedure.name", read_only=True, default=None)

    class Meta:
        model = Appointment
        fields = [
            "id",
            "patient",
            "patient_name",
            "practitioner",
            "practitioner_name",
            "care_unit",
            "care_unit_name",
            "procedure",
            "procedure_name",
            "insurance_company",
            "insurance_plan",
            "starts_at",
            "duration_min",
            "status",
            "source_status",
            "remotely",
            "sync_status",
            "external_id",
        ]


class AppointmentWriteSerializer(ModelSerializer):
    """
    Criação/edição local (RF-AGE-2). As FKs são validadas contra a clínica
    ativa pelo viewset escopado; o write-through (`ScheduleService/Create` +
    pending_sync) entra na fase do adapter.
    """

    class Meta:
        model = Appointment
        fields = [
            "id",
            "patient",
            "practitioner",
            "care_unit",
            "procedure",
            "insurance_company",
            "insurance_plan",
            "starts_at",
            "duration_min",
            "status",
            "remotely",
        ]

    def validate(self, attrs):
        plan = attrs.get("insurance_plan") or (self.instance and self.instance.insurance_plan)
        company = attrs.get("insurance_company") or (
            self.instance and self.instance.insurance_company
        )
        if plan and (company is None or plan.company_id != company.pk):
            raise ValidationError(
                {"insurance_plan": "O plano informado não pertence ao convênio selecionado."}
            )
        return attrs
