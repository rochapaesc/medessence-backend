"""
API da central de notificações - o sino da topbar.

- GET  /notifications/          : o feed, ordenado por severidade.
- GET  /notifications/counters/ : só os contadores (é o que a topbar consulta).
- POST /notifications/read/     : marca tudo como lido.

O feed é derivado de `Appointment` e `SyncRun` a cada leitura - ver
`apps.notifications.services` para a regra de não-lida e o recorte por bloco.
"""

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsClinicMember
from apps.core.context import resolve_active_membership
from apps.notifications.services import build_feed, get_read_at, mark_read


class NotificationsView(APIView):
    permission_classes = [IsClinicMember]

    def get(self, request):
        clinic = resolve_active_membership(request).clinic
        now = timezone.now()

        feed = build_feed(clinic, now=now)
        read_at = get_read_at(clinic, request.user)

        return Response(
            {
                "unread_count": feed.unread_count(read_at),
                "read_at": read_at,
                "truncated": feed.truncated,
                # Total real por bloco: o cabeçalho de um bloco fechado mostra
                # esse número, que pode ser maior que os itens da lista.
                "counts": feed.counts,
                "results": [item.as_dict(read_at) for item in feed.items],
            }
        )


class NotificationsCountersView(APIView):
    """
    Contadores em endpoint dedicado (RNF-5), como `/conversations/counters/`.

    Reusa a mesma derivação do feed de propósito: um COUNT paralelo ficaria mais
    barato, mas abriria espaço para o badge discordar da lista.
    """

    permission_classes = [IsClinicMember]

    def get(self, request):
        clinic = resolve_active_membership(request).clinic
        now = timezone.now()

        feed = build_feed(clinic, now=now)
        read_at = get_read_at(clinic, request.user)

        return Response(
            {
                "unread": feed.unread_count(read_at),
                "total": sum(feed.counts.values()),
                "by_kind": feed.counts,
            }
        )


class NotificationsReadView(APIView):
    permission_classes = [IsClinicMember]

    def post(self, request):
        clinic = resolve_active_membership(request).clinic
        read_at = mark_read(clinic, request.user)

        # Necessariamente zero: todo `occurred_at` é <= agora (invariante travada
        # em `services._item`) e a marca d'água acabou de ir para agora.
        return Response({"unread_count": 0, "read_at": read_at})
