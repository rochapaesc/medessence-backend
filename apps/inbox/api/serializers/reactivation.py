from rest_framework.serializers import (
    CharField,
    ChoiceField,
    IntegerField,
    Serializer,
    ValidationError,
)

from apps.inbox.choices import VariableSource
from apps.inbox.reactivation import variaveis_do_template
from apps.inbox.template_scope import templates_da_clinica


class VariableBindingSerializer(Serializer):
    """De onde sai UMA variável do template (RF-REA-2.3)."""

    source = ChoiceField(choices=VariableSource.choices)
    value = CharField(required=False, allow_blank=True, max_length=200)

    def validate(self, attrs):
        if attrs["source"] == VariableSource.FIXED and not (attrs.get("value") or "").strip():
            # Texto fixo vazio sai como buraco no meio da frase para as 1.891
            # pessoas, e ninguém percebe até alguém responder perguntando.
            raise ValidationError({"value": "Texto fixo não pode ficar em branco."})
        return attrs


class ReactivationMessageSerializer(Serializer):
    """
    A escolha do template e o mapa das variáveis.

    ⚠️ Valida o mapa CONTRA o template escolhido: variável que o template não
    pede é recusada, e variável que ele pede e o mapa não cobre também. Aceitar
    um mapa incompleto adiaria o erro para a hora do disparo, que é quando
    ninguém está olhando a tela.
    """

    template = IntegerField(allow_null=True, required=False)

    def validate(self, attrs):
        clinic = self.context["clinic"]
        template_id = attrs.get("template")

        if template_id is None:
            return {"template": None, "variables": {}}

        # Escopado à CONTA atual (RF-INB-3.3): a configuração é persistente e
        # sai depois para a fila inteira, então apontar para template da conta
        # antiga seria mil envios recusados pela Meta.
        template = templates_da_clinica(clinic.pk).filter(pk=template_id).first()
        if template is None:
            raise ValidationError({"template": "Template não encontrado nesta clínica."})

        # ⚠️ Só APROVADO. A tela já oferece apenas esses, mas a configuração
        # aqui é PERSISTENTE e sai depois para a fila inteira: guardar um
        # template que a Meta não aceita adiaria a recusa para a hora do
        # disparo, quando ninguém está olhando. É a mesma régua do broadcast
        # do wacrm, que esconde o que não é APPROVED em vez de deixar a pessoa
        # escolher algo que vai falhar.
        if template.status != "APPROVED":
            raise ValidationError(
                {
                    "template": (
                        "A Meta ainda não aprovou este template, então ele não "
                        "pode sair para a fila. Escolha um aprovado."
                    )
                }
            )

        pedidas = variaveis_do_template(template)
        recebidas = self.initial_data.get("variables") or {}
        if not isinstance(recebidas, dict):
            raise ValidationError({"variables": "Envie um objeto com uma entrada por variável."})

        sobrando = sorted(set(recebidas) - set(pedidas), key=str)
        if sobrando:
            raise ValidationError(
                {"variables": f"O template não usa: {', '.join(sobrando)}."}
            )
        faltando = [chave for chave in pedidas if chave not in recebidas]
        if faltando:
            raise ValidationError(
                {"variables": f"Falta dizer o que recebe: {', '.join(faltando)}."}
            )

        mapa = {}
        for chave in pedidas:
            binding = VariableBindingSerializer(data=recebidas[chave])
            binding.is_valid(raise_exception=True)
            mapa[chave] = dict(binding.validated_data)

        return {"template": template, "variables": mapa}
