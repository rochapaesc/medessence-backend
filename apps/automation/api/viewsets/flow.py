from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.status import HTTP_400_BAD_REQUEST

from apps.automation.api.serializers import FlowRunSerializer, FlowSerializer, FlowVersionSerializer
from apps.automation.choices import FlowRunStatus, FlowStatus
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowRun
from apps.core.api.permissions import IsClinicManager
from apps.core.api.viewsets import ClinicScopedModelViewSet, ClinicScopedReadOnlyViewSet
from apps.core.mixins import AuditMixin


class FlowViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Fluxos de atendimento (§4.3.2).

    Só o GESTOR, e para ler também: um fluxo mal montado responde no lugar da
    clínica para todo paciente que escrever. Não é catálogo como as etiquetas,
    onde o atendente precisa escolher.
    """

    model = Flow
    audit_resource = "Flow"
    serializer_class = FlowSerializer
    permission_classes = [IsClinicManager]
    ordering_fields = ["priority", "name"]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("current_version")
            .annotate(
                runs_active=Count(
                    "runs",
                    filter=Q(runs__status=FlowRunStatus.ACTIVE, runs__deleted_at__isnull=True),
                    distinct=True,
                )
            )
            .order_by("priority", "name")
        )

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        """
        Publica o fluxo (RF-FLW-3/4).

        A validação é a PORTA daqui: rascunho salva quebrado de propósito,
        porque montar um fluxo é trabalho de várias sessões, mas ativo tem
        paciente do outro lado. Os problemas voltam em frases de gente, para
        o gestor consertar sem precisar entender de grafo.
        """
        flow = self.get_object()
        version = flow.current_version

        if not version:
            return Response(
                {"detail": "O fluxo ainda não tem desenho.", "problems": ["O fluxo está vazio."]},
                status=HTTP_400_BAD_REQUEST,
            )

        problems = validate_graph(version.graph or {})
        if problems:
            return Response(
                {"detail": "O fluxo tem pendências.", "problems": problems},
                status=HTTP_400_BAD_REQUEST,
            )

        agora = timezone.now()
        flow.status = FlowStatus.ACTIVE
        flow.activated_at = agora
        version.published_at = version.published_at or agora
        version.save(update_fields=["published_at", "updated_at"])
        flow.save(update_fields=["status", "activated_at", "updated_at"])
        return Response(self.get_serializer(self.get_queryset().get(pk=flow.pk)).data)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        """
        Volta para rascunho. As execuções em voo NÃO são interrompidas: elas
        seguem na versão em que começaram até terminar ou cair no timeout -
        cortar no meio deixaria o paciente falando sozinho.
        """
        flow = self.get_object()
        flow.status = FlowStatus.DRAFT
        flow.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(self.get_queryset().get(pk=flow.pk)).data)

    @action(detail=True, methods=["get"], url_path="versions")
    def versions(self, request, pk=None):
        flow = self.get_object()
        return Response(FlowVersionSerializer(flow.versions.order_by("-number"), many=True).data)


class FlowRunViewSet(ClinicScopedReadOnlyViewSet):
    """
    Execuções (RF-FLW-12). Somente leitura: quem move a execução é o motor.

    Existe para o gestor responder "em que pergunta as pessoas desistem", que
    é a única métrica que faz alguém melhorar um fluxo depois de montado.
    """

    model = FlowRun
    serializer_class = FlowRunSerializer
    permission_classes = [IsClinicManager]

    def get_queryset(self):
        queryset = super().get_queryset().select_related("flow", "contact").order_by("-created_at")
        flow = self.request.query_params.get("flow")
        if flow:
            queryset = queryset.filter(flow_id=flow)
        status = self.request.query_params.get("status")
        if status:
            queryset = queryset.filter(status__in=status.split(","))
        return queryset
