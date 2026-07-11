from django.db.models import Count, Prefetch, Q
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api.permissions import IsClinicManager
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin, SoftDeleteMixin
from apps.patients.api.filtersets import PatientFilterset
from apps.patients.api.serializers import (
    PatientDetailSerializer,
    PatientReadSerializer,
    PatientWriteSerializer,
)
from apps.patients.models import Patient, PatientTag, Tag


class PatientViewSet(AuditMixin, SoftDeleteMixin, ClinicScopedModelViewSet):
    """
    CRM de pacientes (RF-PAC-1..7), escopado pela clínica ativa.
    Busca server-side (?search=) e filtros por tag/cidade/status/profissional.
    """

    model = Patient
    audit_resource = "Patient"
    filterset_class = PatientFilterset
    serializer_class = PatientReadSerializer
    restore_permission_classes = [IsClinicManager]
    ordering_fields = ["name", "last_appointment_at", "created_at"]

    action_serializer_classes = {
        "list": PatientReadSerializer,
        "retrieve": PatientDetailSerializer,
        "create": PatientWriteSerializer,
        "update": PatientWriteSerializer,
        "partial_update": PatientWriteSerializer,
    }

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .prefetch_related(
                Prefetch(
                    "patient_tags",
                    queryset=PatientTag.objects.select_related("tag"),
                )
            )
            .order_by("name")
        )

    @action(detail=False, methods=["get"], url_path="counters")
    def counters(self, request):
        """
        Contadores por status (RF-PAC-2) — endpoint dedicado (RNF-5).

        Sem parâmetros: janela da clínica, carteira inteira.
        Com ?practitioner=<id>: carteira do profissional (pacientes que já
        consultaram com ele), na janela efetiva dele.
        """
        from rest_framework.exceptions import ValidationError

        from apps.scheduling.models import Practitioner

        from apps.patients.api.windows import parse_window

        queryset = self.get_queryset()
        override = parse_window(request)
        practitioner = None
        practitioner_id = request.query_params.get("practitioner")
        if practitioner_id:
            practitioner = Practitioner.objects.filter(
                clinic=self.clinic, pk=practitioner_id
            ).first()
            if practitioner is None:
                raise ValidationError({"practitioner": "Profissional não encontrado."})
            queryset = queryset.filter(appointments__practitioner=practitioner).distinct()
            window_days = practitioner.effective_active_window_days
        else:
            window_days = self.clinic.active_window_days
        if override:
            window_days = override

        return Response(queryset.status_counters(window_days, practitioner))

    @action(detail=False, methods=["get"], url_path="distribution")
    def distribution(self, request):
        """
        Agregações reais para o dashboard (RF-DSH): pacientes por tag e por
        cidade (os 6 maiores de cada). Cada item traz `count` (carteira toda)
        e `active_count` (só ativos na janela da clínica) — o front alterna
        entre "Todos" e "Ativos". Read-only, escopo da clínica ativa.

        Nota: acquisição ("novos no mês") não é derivável de `created_at` —
        na base sincronizada do EHR ele reflete a data de importação, não o
        cadastro real. Fica de fora até haver um campo de origem confiável.
        """
        from apps.patients.api.windows import parse_window
        from apps.patients.models.patient import active_cutoff

        from django.utils import timezone

        base = self.get_queryset().order_by()  # limpa ordenação p/ agregação
        window_days = parse_window(request) or self.clinic.active_window_days
        now = timezone.now()
        cutoff = active_cutoff(window_days, now)
        alive_assignment = Q(assignments__deleted_at__isnull=True) & Q(
            assignments__patient__deleted_at__isnull=True
        )
        # Ativo = compareceu na janela OU tem consulta futura agendada.
        active_patient = Q(assignments__patient__last_appointment_at__gte=cutoff) | Q(
            assignments__patient__next_appointment_at__gte=now
        )

        # Pacientes (vivos) por tag da clínica — total e ativos.
        tags = (
            Tag.objects.filter(clinic=self.clinic, deleted_at__isnull=True)
            .annotate(
                patients=Count(
                    "assignments__patient", filter=alive_assignment, distinct=True
                ),
                active=Count(
                    "assignments__patient",
                    filter=alive_assignment & active_patient,
                    distinct=True,
                ),
            )
            .filter(patients__gt=0)
            .order_by("-patients", "name")
        )
        by_tag = [
            {
                "name": t.name,
                "color": t.color,
                "count": t.patients,
                "active_count": t.active,
            }
            for t in tags[:6]
        ]

        # Pacientes por cidade (ignora cidade vazia) — total e ativos.
        cities = (
            base.exclude(city="")
            .values("city")
            .annotate(
                count=Count("id"),
                active=Count(
                    "id",
                    filter=Q(last_appointment_at__gte=cutoff)
                    | Q(next_appointment_at__gte=now),
                ),
            )
            .order_by("-count", "city")
        )
        by_city = [
            {"city": c["city"], "count": c["count"], "active_count": c["active"]}
            for c in cities[:6]
        ]

        return Response({"by_tag": by_tag, "by_city": by_city})
