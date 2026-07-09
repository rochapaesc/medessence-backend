from rest_framework.serializers import IntegerField, ModelSerializer

from apps.scheduling.models import (
    CareUnit,
    InsuranceCompany,
    InsurancePlan,
    Practitioner,
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
        fields = ["id", "name", "external_id"]


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
