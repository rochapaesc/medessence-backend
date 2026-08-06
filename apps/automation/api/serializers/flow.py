from rest_framework.serializers import (
    CharField,
    IntegerField,
    JSONField,
    ModelSerializer,
    SerializerMethodField,
)

from apps.automation.choices import FlowNodeType
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowRun, FlowVersion


def grafo_inicial():
    """
    O desenho de um fluxo recém-criado: só o ponto de entrada.

    Um fluxo tem UM início, e é por isso que ele não aparece no cardápio de
    criar passo. Ele precisa então vir de fábrica, senão a tela nasce vazia e
    sem saída.
    """
    return {
        "entry_node": "inicio",
        "nodes": [
            {
                "id": "inicio",
                "type": FlowNodeType.START,
                "label": "Início",
                "position": {"x": 240, "y": 240},
                "config": {},
            }
        ],
        "edges": [],
    }


class FlowVersionSerializer(ModelSerializer):
    problems = SerializerMethodField()

    class Meta:
        model = FlowVersion
        fields = ["id", "number", "graph", "published_at", "created_at", "problems"]
        read_only_fields = ["number", "published_at", "created_at"]

    def get_problems(self, obj) -> list[str]:
        """
        O que impede ativar (RF-FLW-4). Vai junto com a versão para a tela
        poder mostrar a lista enquanto o gestor monta, e não só quando ele
        tenta ativar e leva um erro.
        """
        return validate_graph(obj.graph or {})


class FlowSerializer(ModelSerializer):
    """O fluxo na lista e no formulário. O DESENHO não vem aqui - vem na versão."""

    graph = JSONField(write_only=True, required=False)
    current_version_number = IntegerField(source="current_version.number", read_only=True)
    runs_active = SerializerMethodField()
    can_activate = SerializerMethodField()

    class Meta:
        model = Flow
        fields = [
            "id",
            "name",
            "status",
            "trigger",
            "trigger_config",
            "only_outside_hours",
            "priority",
            "fallback",
            "current_version_number",
            "activated_at",
            "graph",
            "runs_active",
            "can_activate",
        ]
        read_only_fields = ["status", "activated_at"]

    def get_runs_active(self, obj) -> int:
        """Quantos pacientes estão DENTRO do fluxo agora - o número que faz o
        gestor pensar duas vezes antes de mexer no desenho."""
        return getattr(obj, "runs_active", 0)

    def get_can_activate(self, obj) -> bool:
        version = obj.current_version
        return bool(version) and not validate_graph(version.graph or {})

    def create(self, validated_data):
        """
        Fluxo novo nasce em rascunho, com a versão 1 e COM o nó de início.

        Nascer com o grafo vazio deixava o gestor numa tela em branco sem por
        onde começar: o início é um só por fluxo, então ele não está no
        cardápio de criar, e sem ele posto aqui não havia como pôr o primeiro
        passo. Foi assim que apareceu um fluxo com zero passos no banco.
        """
        graph = validated_data.pop("graph", None)
        flow = super().create(validated_data)
        version = FlowVersion.objects.create(flow=flow, number=1, graph=graph or grafo_inicial())
        flow.current_version = version
        flow.save(update_fields=["current_version"])
        return flow

    def update(self, instance, validated_data):
        """
        Mexer no desenho cria uma VERSÃO NOVA, nunca reescreve a que está no
        ar (RF-FLW-1.1) - as execuções em voo continuam na versão em que
        começaram. Mudar só a política (nome, prioridade, fallback) não versiona.
        """
        graph = validated_data.pop("graph", None)
        flow = super().update(instance, validated_data)
        if graph is not None:
            ultima = flow.versions.order_by("-number").first()
            nova = FlowVersion.objects.create(
                flow=flow, number=(ultima.number if ultima else 0) + 1, graph=graph
            )
            flow.current_version = nova
            flow.save(update_fields=["current_version"])
        return flow


class FlowRunSerializer(ModelSerializer):
    """
    Uma execução, como o gestor a vê: quem, em que ponto parou e como acabou.
    """

    contact_name = CharField(source="contact.display_name", read_only=True, default="")
    flow_name = CharField(source="flow.name", read_only=True)
    started_at = CharField(source="created_at", read_only=True)

    class Meta:
        model = FlowRun
        fields = [
            "id",
            "flow",
            "flow_name",
            "contact",
            "contact_name",
            "conversation",
            "status",
            "current_node",
            "vars",
            "started_at",
            "ended_at",
            "end_reason",
        ]
        read_only_fields = fields
