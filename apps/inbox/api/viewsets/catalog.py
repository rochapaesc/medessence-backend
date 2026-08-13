from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework.exceptions import ValidationError

from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.core.api.viewsets import (
    ClinicScopedCreateListViewSet,
    ClinicScopedModelViewSet,
)
from apps.core.mixins import AuditMixin
from apps.inbox.api.serializers import (
    QuickReplySerializer,
    WhatsAppTemplateCreateSerializer,
    WhatsAppTemplateEditSerializer,
    WhatsAppTemplateSerializer,
)
from apps.inbox.models import QuickReply, WhatsAppTemplate


class QuickReplyViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    CRUD de respostas rápidas (RF-INB-8). LER é de todo mundo — é a recepção
    que usa; ESCREVER é só do gestor, como o catálogo de assuntos (RF-ATD-9.1).
    """

    model = QuickReply
    audit_resource = "QuickReply"
    serializer_class = QuickReplySerializer
    ordering_fields = ["label"]

    def get_permissions(self):
        classes = (
            [IsClinicMember]
            if self.request.method in ("GET", "HEAD", "OPTIONS")
            else [IsClinicManager]
        )
        return [c() for c in classes]

    def get_queryset(self):
        """
        Busca NO SERVIDOR, com a ordenação de relevância do Chatwoot: atalho
        que COMEÇA com o termo, depois atalho que contém, depois rótulo, e por
        último o texto. Quem digita `/pre` quer `/preparo` antes de uma
        resposta qualquer que mencione "preparo" no meio.
        """
        queryset = super().get_queryset()
        termo = (self.request.query_params.get("search") or "").strip().lstrip("/")
        if not termo:
            return queryset.order_by("label")
        relevancia = Case(
            When(shortcut__istartswith=termo, then=Value(0)),
            When(shortcut__icontains=termo, then=Value(1)),
            When(label__icontains=termo, then=Value(2)),
            default=Value(3),
            output_field=IntegerField(),
        )
        return (
            queryset.filter(
                Q(shortcut__icontains=termo)
                | Q(label__icontains=termo)
                | Q(body__icontains=termo)
            )
            .annotate(relevancia=relevancia)
            .order_by("relevancia", "label")
        )


class WhatsAppTemplateViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Templates da clínica: ler (RF-INB-3), criar, editar e apagar (RF-INB-3.2).

    Ler é de todo mundo, porque é a recepção que escolhe o template na hora de
    responder. Escrever é só do gestor: o que sai daqui vai para a conta da
    clínica na Meta e passa por revisão humana.

    ⚠️ Apagar chega na Meta e é IRREVERSÍVEL. O template some da conta, e todo
    fluxo ou campanha que apontava para ele para de enviar.
    """

    model = WhatsAppTemplate
    audit_resource = "WhatsAppTemplate"
    serializer_class = WhatsAppTemplateSerializer
    ordering_fields = ["name"]

    def get_permissions(self):
        classes = (
            [IsClinicMember]
            if self.request.method in ("GET", "HEAD", "OPTIONS")
            else [IsClinicManager]
        )
        return [c() for c in classes]

    def get_serializer_class(self):
        if self.action == "create":
            return WhatsAppTemplateCreateSerializer
        if self.action in ("update", "partial_update"):
            return WhatsAppTemplateEditSerializer
        return WhatsAppTemplateSerializer

    def perform_destroy(self, instance):
        """
        Apaga na Meta ANTES de apagar aqui (RF-INB-3.2.8).

        ⚠️ A ordem importa: apagar o nosso primeiro e falhar lá deixaria um
        template órfão na conta da clínica, com o nome ocupado e sem nada por
        aqui que aponte para ele.

        Template que nunca chegou à Meta pula essa chamada: ele só existe
        aqui.
        """
        from apps.inbox.models import Channel
        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        if instance.meta_template_id:
            channel = Channel.objects.filter(clinic=instance.clinic).first()
            if channel is None:
                raise ValidationError(
                    "Esta clínica não tem canal de WhatsApp configurado, então "
                    "não dá para apagar o template na Meta."
                )
            try:
                # O id vai junto de propósito: sem ele a Meta apaga TODAS as
                # variantes de idioma com este nome.
                get_whatsapp_provider(channel).delete_template(
                    instance.name, instance.meta_template_id
                )
            except WhatsAppError as exc:
                raise ValidationError(
                    f"A Meta não apagou o template: {exc}"
                ) from exc
        super().perform_destroy(instance)

    def get_queryset(self):
        return super().get_queryset().order_by("name")
