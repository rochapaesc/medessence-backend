from django.db.models import Q
from django_filters.rest_framework import (
    BooleanFilter,
    CharFilter,
    FilterSet,
    NumberFilter,
)

from apps.inbox.models import Conversation, Message


class ConversationFilterset(FilterSet):
    """
    Filtros do inbox (RF-INB-1 + RF-ATD-1): fila por status, não lidas,
    atribuição, busca.

    `status` aceita lista: `?status=waiting,open` — a fila padrão do front é
    "o que está vivo", e Resolvidas entram por FILTRO explícito (RF-ATD-1.1),
    não por aba.
    """

    search = CharFilter(method="filter_search")
    unread = BooleanFilter(method="filter_unread")
    assigned_to = NumberFilter(field_name="assigned_to_id")
    status = CharFilter(method="filter_status")
    label = CharFilter(method="filter_label")

    class Meta:
        model = Conversation
        fields = [
            "search",
            "unread",
            "status",
            "label",
            "priority",
            "attended_by",
            "assigned_to",
            "channel",
            "patient",
        ]

    def filter_status(self, queryset, name, value):
        valores = [v.strip() for v in value.split(",") if v.strip()]
        return queryset.filter(status__in=valores) if valores else queryset

    def filter_label(self, queryset, name, value):
        """
        Filtra por etiqueta (RF-ATD-9.3) - sem isto, classificar não serviria
        para nada. Aceita lista: `?label=3,7` traz quem tem QUALQUER uma das
        duas (é assim que se procura "reclamação ou orçamento"); exigir as duas
        juntas devolveria quase sempre vazio.
        """
        ids = [v.strip() for v in value.split(",") if v.strip().isdigit()]
        return queryset.filter(labels__id__in=ids).distinct() if ids else queryset

    def filter_search(self, queryset, name, value):
        return queryset.filter(
            Q(contact__wa_id__icontains=value)
            | Q(contact__display_name__icontains=value)
            | Q(patient__name__icontains=value)
        )

    def filter_unread(self, queryset, name, value):
        if value:
            return queryset.filter(unread_count__gt=0)
        return queryset.filter(unread_count=0)


class MessageFilterset(FilterSet):
    """Thread de uma conversa (RF-INB-2)."""

    class Meta:
        model = Message
        fields = ["conversation", "direction", "sender_kind", "kind"]
