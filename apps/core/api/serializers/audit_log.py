from rest_framework.serializers import ModelSerializer

from apps.core.models import AuditLog


class AuditLogReadSerializer(ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "user",
            "action",
            "resource",
            "resource_id",
            "ip_address",
            "timestamp",
        ]


class AuditLogDetailSerializer(ModelSerializer):
    class Meta:
        model = AuditLog
        fields = [
            "id",
            "user",
            "action",
            "resource",
            "resource_id",
            "payload",
            "ip_address",
            "timestamp",
        ]
