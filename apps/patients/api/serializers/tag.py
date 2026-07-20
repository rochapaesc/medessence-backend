from rest_framework.serializers import ModelSerializer

from apps.patients.models import Tag


class TagSerializer(ModelSerializer):
    """
    Catálogo de tags da clínica. `sync_scope`/`identifier`/`external_id` são
    controlados pelo motor de sync (fase do adapter) - read-only na API.
    """

    class Meta:
        model = Tag
        fields = ["id", "name", "color", "sync_scope", "external_id", "identifier"]
        read_only_fields = ["sync_scope", "external_id", "identifier"]


class TagSummarySerializer(ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "color"]
