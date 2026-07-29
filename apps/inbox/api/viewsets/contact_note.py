from apps.core.api.viewsets.base import BaseModelViewSet
from apps.core.api.viewsets.scoped import ClinicScopedMixin
from apps.core.mixins import AuditMixin
from apps.inbox.api.serializers import ContactNoteSerializer
from apps.patients.models import ContactNote


class ContactNoteViewSet(AuditMixin, ClinicScopedMixin, BaseModelViewSet):
    """
    Anotações sobre o contato (Bloco C). Filtre por `?contact=<id>`.

    Editável, diferente da mensagem: esta nota nunca saiu do sistema e é
    registro OPERACIONAL da clínica sobre a pessoa ("prefere ser chamada de
    Malu"). Corrigir um nome escrito errado não reescreve história de
    atendimento — e o antes/depois fica na auditoria.
    """

    model = ContactNote
    audit_resource = "ContactNote"
    serializer_class = ContactNoteSerializer
    select_related = ["author", "contact"]
    ordering_fields = ["created_at"]

    def get_queryset(self):
        queryset = super().get_queryset().order_by("-created_at")
        contato = self.request.query_params.get("contact")
        return queryset.filter(contact_id=contato) if contato else queryset

    def clinic_save_kwargs(self) -> dict:
        return {"clinic": self.clinic, "author": self.request.user}

    def perform_update(self, serializer):
        # O AUTOR não muda na edição: a assinatura diz quem registrou aquilo,
        # e trocá-la para quem corrigiu apagaria de quem era a informação.
        super().perform_update(serializer)
