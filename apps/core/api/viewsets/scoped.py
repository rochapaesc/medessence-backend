"""
Viewsets do plano clínica — escopo por tenant em todo queryset (§3/§3.1).

O contrato:
    - Leitura: `get_queryset()` SEMPRE filtra pela clínica ativa, inclusive
      via lookup indireto (`clinic_lookup = "conversation__clinic"`).
    - Escrita: o campo `clinic` é read_only nos serializers e INJETADO aqui
      do contexto ativo — nunca aceito do payload.
    - O contexto é resolvido uma vez em `initial()` (após autenticação e
      permissões) e fica em `self.clinic` / `self.membership`.
"""

from apps.core.api.permissions import IsClinicMember
from apps.core.api.viewsets.base import (
    BaseCreateDestroyViewSet,
    BaseGenericViewSet,
    BaseModelViewSet,
    BaseReadOnlyModelViewSet,
    ListModelViewSet,
)
from apps.core.context import resolve_active_membership


class ClinicScopedMixin:
    """
    Escopo por clínica para qualquer viewset base.

    `clinic_lookup` aceita caminho indireto quando o modelo não tem FK
    direta para a clínica (ex.: Message → "conversation__clinic").
    """

    clinic_lookup = "clinic"
    permission_classes = [IsClinicMember]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # As permissions (IsClinicMember/por papel) já resolveram e cachearam
        # o membership; aqui só materializamos o atalho para o restante do fluxo.
        self.membership = resolve_active_membership(request)
        self.clinic = self.membership.clinic

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.filter(**{self.clinic_lookup: self.clinic})

    def perform_create(self, serializer):
        # Injeção da clínica ativa no caminho de escrita. Só se aplica a
        # modelos com FK direta; recursos com lookup indireto herdam o escopo
        # da FK pai (validada com assert_object_in_clinic no serializer).
        serializer.save(**self.clinic_save_kwargs())

    def clinic_save_kwargs(self) -> dict:
        if self.clinic_lookup == "clinic":
            return {"clinic": self.clinic}
        return {}


class ClinicScopedModelViewSet(ClinicScopedMixin, BaseModelViewSet):
    """CRUD completo escopado pela clínica ativa."""


class ClinicScopedReadOnlyViewSet(ClinicScopedMixin, BaseReadOnlyModelViewSet):
    """Leitura (list/retrieve) escopada pela clínica ativa."""


class ClinicScopedListViewSet(ClinicScopedMixin, ListModelViewSet):
    """Somente list escopado pela clínica ativa."""


class ClinicScopedCreateDestroyViewSet(ClinicScopedMixin, BaseCreateDestroyViewSet):
    """Criação/leitura/exclusão (sem update) escopado pela clínica ativa."""


__all__ = [
    "BaseGenericViewSet",
    "ClinicScopedCreateDestroyViewSet",
    "ClinicScopedListViewSet",
    "ClinicScopedMixin",
    "ClinicScopedModelViewSet",
    "ClinicScopedReadOnlyViewSet",
]
