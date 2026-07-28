from django.db.models import Count, Q

from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin
from apps.inbox.api.serializers import ConversationLabelSerializer
from apps.inbox.models import ConversationLabel


class ConversationLabelViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Catálogo de assuntos (RF-ATD-9). LER é de todo mundo - o atendente precisa
    escolher; ESCREVER é só do gestor (RF-ATD-9.1), que é o que mantém o
    catálogo pequeno e a métrica por assunto viva.
    """

    model = ConversationLabel
    audit_resource = "ConversationLabel"
    serializer_class = ConversationLabelSerializer
    ordering_fields = ["name"]

    def get_permissions(self):
        classes = [IsClinicMember] if self.request.method in ("GET", "HEAD", "OPTIONS") else [
            IsClinicManager
        ]
        return [c() for c in classes]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .annotate(
                usage_count=Count(
                    "conversations",
                    filter=Q(conversations__deleted_at__isnull=True),
                    distinct=True,
                )
            )
            .order_by("name")
        )

    def perform_destroy(self, instance):
        """
        Excluir APOSENTA (RF-ATD-9): a etiqueta some da escolha e continua nas
        conversas que já a têm. Apagar de verdade reescreveria o passado - a
        conversa que foi uma reclamação continua tendo sido.
        """
        if instance.is_active:
            instance.is_active = False
            instance.save(update_fields=["is_active", "updated_at"])
