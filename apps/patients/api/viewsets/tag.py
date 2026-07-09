from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin, ProtectRelationsMixin
from apps.patients.api.serializers import TagSerializer
from apps.patients.models import Tag


class TagViewSet(ProtectRelationsMixin, AuditMixin, ClinicScopedModelViewSet):
    """
    Catálogo de tags (RF-PAC-5). Excluir tag com atribuições é bloqueado
    (remova as atribuições primeiro); excluir é do gestor.
    """

    model = Tag
    audit_resource = "Tag"
    serializer_class = TagSerializer
    protect_on_delete = {"assignments": "atribuição"}
    ordering_fields = ["name", "created_at"]

    action_permission_classes = {
        "destroy": [IsClinicManager],
        "list": [IsClinicMember],
        "retrieve": [IsClinicMember],
        "create": [IsClinicMember],
        "update": [IsClinicMember],
        "partial_update": [IsClinicMember],
    }

    def get_queryset(self):
        return super().get_queryset().order_by("name")
