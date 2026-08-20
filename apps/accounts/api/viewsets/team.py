"""
Equipe da clínica (§4.12, RF-EQP-1..9).

A tela que tira o fornecedor do meio: até aqui, admitir alguém era
`manage.py membership` ou o Django admin, os dois fora do alcance da clínica.
"""

from django.db.models import Count, Q
from django_filters.rest_framework import CharFilter, FilterSet
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.accounts.api.serializers.team import (
    TeamMemberCreateSerializer,
    TeamMemberSerializer,
    TeamMemberUpdateSerializer,
)
from apps.accounts.models import Membership
from apps.accounts.team import (
    create_member,
    deactivate_member,
    reactivate_member,
    reset_member_password,
    update_member,
)
from apps.core.api.permissions import IsClinicManager
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.context import assert_object_in_clinic
from apps.inbox.choices import ConversationStatus


class TeamMemberFilter(FilterSet):
    # ⚠️ `SearchFilter` do DRF não existe neste projeto: declarar
    # `search_fields` num viewset daqui não faz nada (a busca da Auditoria
    # ficou inerte assim por uma sessão inteira). Busca é sempre filterset.
    search = CharFilter(method="filter_search")

    class Meta:
        model = Membership
        fields = ["role", "is_active"]

    def filter_search(self, queryset, name, value):
        value = (value or "").strip()
        if not value:
            return queryset
        return queryset.filter(
            Q(user__first_name__icontains=value)
            | Q(user__last_name__icontains=value)
            | Q(user__email__icontains=value)
        )


@extend_schema(tags=["team"])
class TeamViewSet(ClinicScopedModelViewSet):
    """
    Membros da clínica ativa. Só o gestor, nas duas camadas de sempre.

    A autorização de verdade mora em `apps.accounts.team`, e não aqui: esta
    camada só traduz payload e erro.
    """

    model = Membership
    serializer_class = TeamMemberSerializer
    permission_classes = [IsClinicManager]
    filterset_class = TeamMemberFilter
    select_related = ["user", "practitioner", "clinic"]
    ordering_fields = ["user__first_name", "role"]
    http_method_names = ["get", "post", "patch", "options", "head"]

    def get_serializer_class(self):
        if self.action == "create":
            return TeamMemberCreateSerializer
        if self.action in ("update", "partial_update"):
            return TeamMemberUpdateSerializer
        return TeamMemberSerializer

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .annotate(
                open_conversations=Count(
                    "user__assigned_conversations",
                    filter=Q(
                        user__assigned_conversations__clinic=self.clinic,
                        user__assigned_conversations__deleted_at__isnull=True,
                        user__assigned_conversations__status__in=[
                            ConversationStatus.OPEN,
                            ConversationStatus.SNOOZED,
                        ],
                    ),
                    distinct=True,
                )
            )
            # Inativo por último: continua na lista para poder ser reativado,
            # mas não disputa a atenção com quem trabalha hoje.
            .order_by("-is_active", "user__first_name", "user__email")
        )

    def _practitioner(self, serializer, default=...):
        """Carteira do payload, sempre revalidada contra a clínica ativa (§3)."""
        if "practitioner" not in serializer.validated_data:
            return default
        practitioner = serializer.validated_data["practitioner"]
        if practitioner is not None:
            assert_object_in_clinic(practitioner, self.clinic, label="practitioner")
        return practitioner

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        membership, temporary_password = create_member(
            clinic=self.clinic,
            actor=request.user,
            email=serializer.validated_data["email"],
            name=serializer.validated_data.get("name", ""),
            role=serializer.validated_data["role"],
            practitioner=self._practitioner(serializer, default=None),
            request=request,
        )

        body = TeamMemberSerializer(
            self.get_queryset().get(pk=membership.pk), context=self.get_serializer_context()
        ).data
        # ⚠️ A senha temporária viaja UMA vez, aqui, e não é gravada em lugar
        # nenhum (RF-EQP-2.2). Vem nula quando a pessoa já tinha conta: nesse
        # caso ela entra com a senha que já usa na outra clínica.
        body["temporary_password"] = temporary_password
        return Response(body, status=201)

    def update(self, request, *args, **kwargs):
        membership = self.get_object()
        serializer = self.get_serializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        update_member(
            membership=membership,
            actor=request.user,
            name=serializer.validated_data.get("name"),
            role=serializer.validated_data.get("role"),
            practitioner=self._practitioner(serializer),
            request=request,
        )
        return Response(self._body(membership.pk))

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        membership = self.get_object()
        liberadas = deactivate_member(membership=membership, actor=request.user, request=request)
        body = self._body(membership.pk)
        # Quantas conversas voltaram para a fila: a tela promete o número no
        # diálogo e precisa confirmar o que de fato aconteceu.
        body["released_conversations"] = liberadas
        return Response(body)

    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, pk=None):
        membership = self.get_object()
        reactivate_member(membership=membership, actor=request.user, request=request)
        return Response(self._body(membership.pk))

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        membership = self.get_object()
        temporary_password = reset_member_password(
            membership=membership, actor=request.user, request=request
        )
        body = self._body(membership.pk)
        body["temporary_password"] = temporary_password
        return Response(body)

    def _body(self, pk) -> dict:
        return TeamMemberSerializer(
            self.get_queryset().get(pk=pk), context=self.get_serializer_context()
        ).data
