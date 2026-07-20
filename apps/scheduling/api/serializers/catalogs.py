from rest_framework.serializers import CharField, IntegerField, ModelSerializer

from apps.scheduling.models import (
    CareUnit,
    InsuranceCompany,
    InsurancePlan,
    Practitioner,
    PractitionerProcedure,
    Procedure,
)


class PractitionerSerializer(ModelSerializer):
    effective_active_window_days = IntegerField(read_only=True)

    class Meta:
        model = Practitioner
        fields = [
            "id",
            "name",
            "license_number",
            "active_window_days",
            "effective_active_window_days",
            "external_id",
        ]


class CareUnitSerializer(ModelSerializer):
    class Meta:
        model = CareUnit
        fields = ["id", "name", "address", "work_journey", "external_id"]


class PractitionerProcedureSerializer(ModelSerializer):
    """Procedimento oferecido pelo profissional - opções do form Nova consulta."""

    procedure_id = IntegerField(source="procedure.pk", read_only=True)
    name = CharField(source="procedure.name", read_only=True)

    class Meta:
        model = PractitionerProcedure
        fields = [
            "id",
            "procedure_id",
            "name",
            "duration_min",
            "price",
            "description",
            "comments",
            "allow_online",
            "is_active",
        ]


class ProcedureSerializer(ModelSerializer):
    class Meta:
        model = Procedure
        fields = ["id", "name", "duration_min", "remotely", "external_id"]


class InsurancePlanSerializer(ModelSerializer):
    class Meta:
        model = InsurancePlan
        fields = ["id", "name", "external_id"]


class InsuranceCompanySerializer(ModelSerializer):
    plans = InsurancePlanSerializer(many=True, read_only=True)

    class Meta:
        model = InsuranceCompany
        fields = ["id", "name", "external_id", "plans"]
