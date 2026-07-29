from rest_framework.exceptions import PermissionDenied

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
        self._assert_pode_mexer(serializer.instance, acao="editar")
        # O AUTOR não muda na edição: a assinatura diz quem registrou aquilo,
        # e trocá-la para quem corrigiu apagaria de quem era a informação.
        super().perform_update(serializer)

    def perform_destroy(self, instance):
        self._assert_pode_mexer(instance, acao="excluir")
        super().perform_destroy(instance)

    def _assert_pode_mexer(self, nota, *, acao: str):
        """
        Mexe quem escreveu, ou o gestor — a MESMA regra das mensagens.

        A barreira vive no SERVIDOR: o front mostra o lápis para todo mundo e
        traduz o 403 em aviso, então sem esta guarda qualquer colega
        reescreveria o registro do outro por um PATCH direto.
        """
        from apps.accounts.choices import MembershipRole

        eh_autor = nota.author_id == self.request.user.pk
        eh_gestor = self.membership.role == MembershipRole.MANAGER
        if not (eh_autor or eh_gestor):
            raise PermissionDenied(
                f"Só quem escreveu (ou um gestor) pode {acao} esta nota."
            )
