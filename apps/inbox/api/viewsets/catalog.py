from django.db.models import Case, IntegerField, Q, Value, When

from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.core.api.viewsets import ClinicScopedModelViewSet, ClinicScopedReadOnlyViewSet
from apps.core.mixins import AuditMixin
from apps.inbox.api.serializers import QuickReplySerializer, WhatsAppTemplateSerializer
from apps.inbox.models import QuickReply, WhatsAppTemplate


class QuickReplyViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    CRUD de respostas rápidas (RF-INB-8). LER é de todo mundo — é a recepção
    que usa; ESCREVER é só do gestor, como o catálogo de assuntos (RF-ATD-9.1).
    """

    model = QuickReply
    audit_resource = "QuickReply"
    serializer_class = QuickReplySerializer
    ordering_fields = ["label"]

    def get_permissions(self):
        classes = (
            [IsClinicMember]
            if self.request.method in ("GET", "HEAD", "OPTIONS")
            else [IsClinicManager]
        )
        return [c() for c in classes]

    def get_queryset(self):
        """
        Busca NO SERVIDOR, com a ordenação de relevância do Chatwoot: atalho
        que COMEÇA com o termo, depois atalho que contém, depois rótulo, e por
        último o texto. Quem digita `/pre` quer `/preparo` antes de uma
        resposta qualquer que mencione "preparo" no meio.
        """
        queryset = super().get_queryset()
        termo = (self.request.query_params.get("search") or "").strip().lstrip("/")
        if not termo:
            return queryset.order_by("label")
        relevancia = Case(
            When(shortcut__istartswith=termo, then=Value(0)),
            When(shortcut__icontains=termo, then=Value(1)),
            When(label__icontains=termo, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
        return (
            queryset.filter(
                Q(shortcut__icontains=termo)
                | Q(label__icontains=termo)
                | Q(body__icontains=termo)
            )
            .annotate(relevancia=relevancia)
            .order_by("relevancia", "label")
        )


class WhatsAppTemplateViewSet(ClinicScopedReadOnlyViewSet):
    """Templates aprovados (RF-INB-3) - read-only; populados pelo beat na Fatia B."""

    model = WhatsAppTemplate
    serializer_class = WhatsAppTemplateSerializer
    ordering_fields = ["name"]

    def get_queryset(self):
        return super().get_queryset().order_by("name")
