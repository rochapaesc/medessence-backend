from apps.automation.api.serializers import HttpDestinationSerializer
from apps.automation.models import HttpDestination
from apps.core.mixins import AuditMixin
from apps.core.api.permissions import IsClinicManager
from apps.core.api.viewsets import ClinicScopedModelViewSet


class HttpDestinationViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Destinos permitidos para o nó "Chamar sistema externo" (RF-FLW-16.1).

    **Só o gestor, inclusive para LER.** Diferente das sequências, onde a
    recepção lê para poder inscrever alguém: aqui a listagem carrega o
    endereço dos sistemas internos da clínica, que é reconhecimento de rede
    para quem quiser usá-lo assim. Quem monta fluxo é o gestor, e é ele quem
    precisa da lista.

    A cerca contra SSRF mora no serializer e no `clean()` do modelo. O
    viewset não relaxa nenhuma das duas.
    """

    model = HttpDestination
    audit_resource = "HttpDestination"
    serializer_class = HttpDestinationSerializer
    permission_classes = [IsClinicManager]
    search_fields = ["name", "url"]
    ordering_fields = ["name"]
