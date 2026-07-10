from django.db.models import Q
from django_filters.rest_framework import (
    BooleanFilter,
    CharFilter,
    FilterSet,
    NumberFilter,
)

from apps.inbox.models import Conversation, Message


class ConversationFilterset(FilterSet):
    """Filtros do inbox (RF-INB-1): aba 'aguardando', não lidas, atribuição, busca."""

    search = CharFilter(method="filter_search")
    unread = BooleanFilter(method="filter_unread")
    assigned_to = NumberFilter(field_name="assigned_to_id")

    class Meta:
        model = Conversation
        fields = ["search", "unread", "needs_agent", "assigned_to", "channel", "patient"]

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
