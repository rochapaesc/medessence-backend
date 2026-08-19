"""
API da mensagem de resgate (RF-REA-2.2/2.3/2.4).

  GET /reactivation-message/  : template escolhido, mapa das variáveis, a
                                lista de templates aprovados e a prévia.
  PUT /reactivation-message/  : troca o template e o mapa, juntos.

O disparo NÃO mora aqui: ele é o RF-REA-2, bloqueado enquanto a conta da Meta
não tiver um template de resgate da clínica.
"""

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsClinicManager
from apps.core.context import resolve_active_membership
from apps.inbox.api.serializers.reactivation import ReactivationMessageSerializer
from apps.inbox.choices import VariableSource
from apps.inbox.models import ReactivationMessage
from apps.inbox.reactivation import (
    corpo_do_template,
    previa,
    valor_da_variavel,
    variaveis_do_template,
)
from apps.inbox.template_vars import modelo_do_link, rotulo_da_variavel
from apps.inbox.template_scope import templates_da_clinica
from apps.patients.models import Patient


class ReactivationMessageView(APIView):
    """
    Só o gestor, mesma régua do horário de funcionamento: quem escolhe o
    template decide o que 1.891 pessoas vão receber.
    """

    permission_classes = [IsClinicManager]
    serializer_class = ReactivationMessageSerializer

    def get(self, request):
        clinic = resolve_active_membership(request).clinic
        return Response(self._payload(clinic))

    def put(self, request):
        clinic = resolve_active_membership(request).clinic
        serializer = self.serializer_class(
            data=request.data, context={"clinic": clinic}
        )
        serializer.is_valid(raise_exception=True)
        dados = serializer.validated_data

        mensagem, _ = ReactivationMessage.objects.get_or_create(clinic=clinic)
        mensagem.template = dados["template"]
        mensagem.variables = dados["variables"]
        mensagem.save(update_fields=["template", "variables", "updated_at"])
        return Response(self._payload(clinic))

    def _payload(self, clinic) -> dict:
        mensagem = ReactivationMessage.objects.filter(clinic=clinic).first()
        exemplo = self._paciente_exemplo(clinic)
        return {
            "template": mensagem.template_id if mensagem else None,
            "variables": (mensagem.variables if mensagem else None) or {},
            "available_templates": [
                {
                    "id": template.pk,
                    "name": template.name,
                    "language": template.language,
                    "category": template.category,
                    "body": corpo_do_template(template),
                    # ⚠️ Os componentes CRUS, como a Meta os guarda. A tela
                    # desenha o template como a mensagem que ele é (cabeçalho
                    # em negrito, rodapé apagado, botões azuis embaixo), e sem
                    # eles a campanha voltaria a listar só o corpo em cinza:
                    # quem escolhe o template da fila deixa de ver metade do
                    # que 1.891 pessoas vão receber.
                    "components": template.components or [],
                    "variables": variaveis_do_template(template),
                    # ⚠️ Os MESMOS três campos do serializer do Inbox. A
                    # campanha é o terceiro lugar que manda template, e sem
                    # eles a tela desenha só os `{{n}}` do corpo: o link do
                    # botão e a mídia do cabeçalho ficam sem onde preencher, e
                    # o disparo inteiro é recusado pela Meta.
                    "variable_labels": {
                        chave: rotulo_da_variavel(template, chave)
                        for chave in variaveis_do_template(template)
                    },
                    "variable_url_templates": {
                        chave: url
                        for chave in variaveis_do_template(template)
                        if (url := modelo_do_link(template, chave))
                    },
                }
                for template in templates_da_clinica(clinic.pk)
                .filter(status="APPROVED")
                .order_by("name")
            ],
            "preview": previa(mensagem, exemplo, clinic) if exemplo else "",
            "preview_patient": exemplo.name if exemplo else "",
            # Os valores JÁ resolvidos do paciente de exemplo, um por fonte.
            #
            # ⚠️ Vão para o front para que a prévia do drawer responda enquanto
            # a pessoa troca as fontes, SEM uma ida ao servidor por tecla. E
            # vão resolvidos daqui (e não crus) para que os dois lados montem
            # a mesma frase: se o front capitalizasse por conta própria, a
            # prévia do rascunho sairia diferente da mensagem que o servidor
            # manda depois de salvar.
            "preview_sources": (
                {
                    fonte.value: valor_da_variavel(
                        {"source": fonte.value}, exemplo, clinic
                    )
                    for fonte in VariableSource
                    if fonte != VariableSource.FIXED
                }
                if exemplo
                else {}
            ),
        }

    @staticmethod
    def _paciente_exemplo(clinic):
        """
        O primeiro da fila de resgate, não um paciente qualquer.

        A prévia existe para mostrar como a mensagem CHEGA, e é na fila que
        moram os casos que quebram: nome em caixa alta do prontuário, nome
        composto comprido, cidade em branco.
        """
        return (
            Patient.objects.filter(clinic=clinic)
            .to_reactivate(clinic.active_window_days)
            .order_by("last_appointment_at")
            .first()
        )
