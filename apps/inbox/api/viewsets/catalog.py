from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

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
        # Sincronizar é LEITURA: traz da Meta o que já existe lá e não muda
        # nada na conta. A recepção precisa dela para ver se o template que
        # ela quer usar já foi aprovado.
        somente_leitura = self.request.method in (
            "GET",
            "HEAD",
            "OPTIONS",
        ) or self.action == "sincronizar"
        classes = [IsClinicMember] if somente_leitura else [IsClinicManager]
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
            channel = Channel.objects.filter(
                clinic=instance.clinic, is_test=False
            ).first()
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
        """
        Só o catálogo da CONTA que a clínica usa hoje (RF-INB-3.3).

        ⚠️ A clínica que troca de número ou de app passa a usar outra conta da
        Meta, e o catálogo antigo continuava aqui: a tela mostrava templates de
        duas contas lado a lado, sem como saber qual era de qual. Escolher um
        da conta velha é envio recusado na frente do paciente.
        """
        from apps.inbox.template_scope import conta_da_clinica

        clinic_id = self.clinic.pk
        return (
            super()
            .get_queryset()
            .filter(waba_id=conta_da_clinica(clinic_id))
            .order_by("name")
        )

    @action(detail=False, methods=["post"], url_path="sincronizar")
    def sincronizar(self, request):
        """
        Busca na Meta o estado atual dos templates da clínica (RF-INB-3.2.9).

        ⚠️ A aprovação é revisão HUMANA e o veredito não vem por evento: o
        beat só passa de 6 em 6 horas, então um template aprovado em dois
        minutos podia ficar "em revisão" na tela por horas - e recarregar a
        página não adiantava, porque ela lê o nosso banco e não a Meta.

        Síncrono de propósito: quem clicou está esperando ver o status mudar.
        Enfileirar devolveria "ok" sem nada ter mudado na tela.
        """
        from apps.inbox.tasks import sincronizar_templates_da_clinica
        from apps.integrations.whatsapp.exceptions import WhatsAppError

        try:
            quantos = sincronizar_templates_da_clinica(self.clinic)
        except WhatsAppError as exc:
            raise ValidationError(f"Não deu para falar com a Meta: {exc}") from exc

        # O alvo é a CLÍNICA: a sincronização mexe na lista inteira, e prender
        # o registro a um template só esconderia os outros que mudaram.
        self.log_operation(
            self.clinic,
            "template.sync",
            resource="Clinic",
            templates=quantos,
        )
        return Response({"templates": quantos})

