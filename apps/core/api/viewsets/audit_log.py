import csv

from django.db.models import Count
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api.filtersets import AuditLogFilterset
from apps.core.api.permissions import IsClinicManager
from apps.core.api.serializers import AuditLogDetailSerializer, AuditLogReadSerializer
from apps.core.api.viewsets.scoped import ClinicScopedReadOnlyViewSet
from apps.core.audit import log_action
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction

# Teto do CSV: a auditoria de um período costuma caber com folga; o limite
# existe para um export não derrubar o processo.
EXPORT_LIMIT = 20_000


class AuditLogViewSet(ClinicScopedReadOnlyViewSet):
    """
    Somente leitura - logs são imutáveis por design.

    Plano clínica: o gestor consulta a auditoria da PRÓPRIA clínica
    (o escopo vem do contexto ativo; logs globais, com clinic nula,
    não aparecem aqui - pertencem ao plano plataforma, F5).

    Consultar a auditoria também deixa rastro: quem abre esta tela enxerga os
    acessos de todo mundo, e sem registrar isso o gestor seria o único ponto
    cego do sistema.
    """

    model = AuditLog
    filterset_class = AuditLogFilterset
    serializer_class = AuditLogReadSerializer
    permission_classes = [IsClinicManager]
    ordering_fields = ["timestamp", "action", "resource"]
    search_fields = ["user__email", "user__first_name", "user__last_name"]

    action_serializer_classes = {
        "list": AuditLogReadSerializer,
        "retrieve": AuditLogDetailSerializer,
    }

    def get_queryset(self):
        return super().get_queryset().select_related("user").order_by("-timestamp")

    # ---------------- rótulos resolvidos em lote (sem N+1) ---------------- #

    def get_serializer(self, *args, **kwargs):
        if kwargs.get("many") and args:
            self._prefetch_labels(args[0])
        return super().get_serializer(*args, **kwargs)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["patient_names"] = getattr(self, "_patient_names", {})
        context["user_roles"] = getattr(self, "_user_roles", {})
        return context

    def _prefetch_labels(self, logs) -> None:
        """Duas queries por página: nomes de pacientes e papéis dos usuários."""
        from apps.accounts.models import Membership
        from apps.patients.models import Patient

        logs = list(logs)

        patient_ids = {
            log.resource_id
            for log in logs
            if log.resource == "Patient" and str(log.resource_id).isdigit()
        }
        self._patient_names = (
            {
                str(pk): name
                for pk, name in Patient.all_objects.filter(
                    clinic=self.clinic, pk__in=patient_ids
                ).values_list("pk", "name")
            }
            if patient_ids
            else {}
        )

        user_ids = {log.user_id for log in logs if log.user_id}
        self._user_roles = (
            dict(
                Membership.objects.filter(
                    clinic=self.clinic, user_id__in=user_ids
                ).values_list("user_id", "role")
            )
            if user_ids
            else {}
        )

    # ----------------------------- consultas ----------------------------- #

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        self._log_consulta("list")
        return response

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """Resumo do período filtrado — o que a tela mostra antes da lista."""
        queryset = self.filter_queryset(self.get_queryset())

        by_action = {
            row["action"]: row["total"]
            for row in queryset.order_by().values("action").annotate(total=Count("id"))
        }
        documentos = queryset.filter(action=AuditAction.READ_CPF)

        self._log_consulta("summary")
        return Response(
            {
                "documents_seen": {
                    "total": by_action.get(AuditAction.READ_CPF, 0),
                    "viewers": documentos.values("user_id").distinct().count(),
                    "patients": documentos.values("resource_id").distinct().count(),
                },
                "updates": by_action.get(AuditAction.UPDATE, 0),
                "creates": by_action.get(AuditAction.CREATE, 0),
                "deletes": by_action.get(AuditAction.DELETE, 0),
                "failed_logins": by_action.get(AuditAction.LOGIN_FAILED, 0),
                "by_action": by_action,
            }
        )

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """
        CSV do período filtrado — é como se responde a um pedido de titular
        ("quem acessou meus dados?") sem ninguém rodar SQL.
        """
        logs = list(self.filter_queryset(self.get_queryset())[:EXPORT_LIMIT])
        self._prefetch_labels(logs)

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        stamp = timezone.localtime().strftime("%Y-%m-%d")
        response["Content-Disposition"] = (
            f'attachment; filename="auditoria-{self.clinic.slug}-{stamp}.csv"'
        )
        response.write("﻿")  # BOM: o Excel abre os acentos certos

        writer = csv.writer(response, delimiter=";")
        writer.writerow(
            [
                "Data e hora",
                "Usuário",
                "E-mail",
                "Papel",
                "Evento",
                "Recurso",
                "Sobre",
                "Origem",
            ]
        )
        for log in logs:
            writer.writerow(
                [
                    timezone.localtime(log.timestamp).strftime("%d/%m/%Y %H:%M:%S"),
                    (log.user.get_full_name() or log.user.email) if log.user else "",
                    log.user.email if log.user else "",
                    self._user_roles.get(log.user_id, ""),
                    log.get_action_display(),
                    log.resource,
                    self._patient_names.get(str(log.resource_id), log.resource_id),
                    log.ip_address or "",
                ]
            )

        self._log_consulta("export", extra={"rows": len(logs)})
        return response

    def _log_consulta(self, kind: str, extra: dict | None = None) -> None:
        """
        Registra que alguém consultou a auditoria, com os filtros usados. É o
        que impede o acesso do gestor de ser o único sem rastro.
        """
        payload = {"view": kind, "filters": dict(self.request.query_params.items())}
        if extra:
            payload.update(extra)
        log_action(
            user=self.request.user,
            action=AuditAction.READ,
            resource="AuditLog",
            resource_id="",
            payload=payload,
            request=self.request,
            clinic=self.clinic,
        )
