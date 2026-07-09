from apps.core.api.filtersets import AuditLogFilterset
from apps.core.api.permissions import IsClinicManager
from apps.core.api.serializers import AuditLogDetailSerializer, AuditLogReadSerializer
from apps.core.api.viewsets.scoped import ClinicScopedReadOnlyViewSet
from apps.core.models import AuditLog


class AuditLogViewSet(ClinicScopedReadOnlyViewSet):
    """
    Somente leitura — logs são imutáveis por design.

    Plano clínica: o gestor consulta a auditoria da PRÓPRIA clínica
    (o escopo vem do contexto ativo; logs globais, com clinic nula,
    não aparecem aqui — pertencem ao plano plataforma, F5).
    """

    model = AuditLog
    filterset_class = AuditLogFilterset
    serializer_class = AuditLogReadSerializer
    permission_classes = [IsClinicManager]
    ordering_fields = ["timestamp", "action", "resource"]

    action_serializer_classes = {
        "list": AuditLogReadSerializer,
        "retrieve": AuditLogDetailSerializer,
    }

    def get_queryset(self):
        return super().get_queryset().select_related("user").order_by("-timestamp")
