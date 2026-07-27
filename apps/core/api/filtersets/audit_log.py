from django_filters.rest_framework import (
    CharFilter,
    DateTimeFromToRangeFilter,
    FilterSet,
    NumberFilter,
)

from apps.core.models import AuditLog


class ActionFilterMixin:
    """Filtro de ação compartilhado pelas duas telas de auditoria."""

    def filter_action(self, queryset, name, value):
        """Aceita uma ação ou lista separada por vírgula: ?action=CREATE,DELETE"""
        actions = [a.strip().upper() for a in value.split(",") if a.strip()]
        if not actions:
            return queryset
        return queryset.filter(action__in=actions)


class MyAccessLogFilterset(ActionFilterMixin, FilterSet):
    """
    Filtros de "Meus acessos" (§15.2): só período e tipo de evento.

    Deliberadamente SEM `user`/`user_email`: nesta tela o usuário é imposto
    pelo viewset. Um filtro de usuário aqui não ampliaria nada (a interseção
    continuaria vazia), mas deixaria dúvida sobre quem manda no recorte - e
    essa dúvida, num endpoint de dado pessoal, já é defeito.
    """

    action = CharFilter(method="filter_action")
    # ?timestamp_after=2026-05-01&timestamp_before=2026-05-14
    timestamp = DateTimeFromToRangeFilter()

    class Meta:
        model = AuditLog
        fields = ["action", "timestamp"]


class AuditLogFilterset(ActionFilterMixin, FilterSet):
    user = NumberFilter(field_name="user_id")
    user_email = CharFilter(field_name="user__email", lookup_expr="iexact")
    action = CharFilter(method="filter_action")
    resource = CharFilter(lookup_expr="iexact")
    resource_id = CharFilter(lookup_expr="exact")
    ip_address = CharFilter(lookup_expr="exact")
    # Intervalo de tempo: ?timestamp_after=2026-05-01&timestamp_before=2026-05-14
    timestamp = DateTimeFromToRangeFilter()

    class Meta:
        model = AuditLog
        fields = [
            "user",
            "user_email",
            "action",
            "resource",
            "resource_id",
            "ip_address",
            "timestamp",
        ]
