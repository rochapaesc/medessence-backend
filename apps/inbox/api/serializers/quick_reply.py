import re

from rest_framework.serializers import (
    ModelSerializer,
    SerializerMethodField,
    ValidationError,
)

from apps.inbox.models import QuickReply, WhatsAppTemplate

# O que pode virar atalho: letras sem acento, números, hífen e sublinhado.
# Acento e espaço quebrariam a digitação rápida, que é o ponto do recurso.
ATALHO_VALIDO = re.compile(r"^[a-z0-9_-]+$")


class QuickReplySerializer(ModelSerializer):
    class Meta:
        model = QuickReply
        fields = ["id", "label", "shortcut", "body"]

    def validate_shortcut(self, valor: str) -> str:
        """Normaliza antes de validar: quem digita "/Bom Dia" quer `bomdia`, e
        recusar por causa da barra ou da maiúscula seria implicância."""
        atalho = (valor or "").strip().lstrip("/").lower().replace(" ", "")
        if not atalho:
            return ""
        if not ATALHO_VALIDO.match(atalho):
            raise ValidationError(
                "Use apenas letras sem acento, números, hífen ou sublinhado."
            )
        # A clínica ativa vem no contexto pelo ClinicScopedMixin.
        clinic = self.context.get("clinic")
        if clinic is None:
            return atalho
        existe = QuickReply.objects.filter(clinic=clinic, shortcut=atalho)
        if self.instance is not None:
            existe = existe.exclude(pk=self.instance.pk)
        if existe.exists():
            raise ValidationError(f"Já existe uma resposta rápida com o atalho /{atalho}.")
        return atalho


class WhatsAppTemplateSerializer(ModelSerializer):
    #: As variáveis que este template pede, qualificadas por componente.
    #:
    #: ⚠️ Vêm do SERVIDOR e não são recalculadas na tela. A regra tem casos
    #: que não se adivinha - botão de URL sem variável não pede parâmetro,
    #: COPY_CODE pede sempre, o índice conta os botões que vêm antes - e
    #: mantê-la em dois lugares é como o front e o backend começam a discordar
    #: sobre o que falta preencher.
    variables = SerializerMethodField()

    #: O rótulo de cada uma, para a tela dizer ONDE o campo cai
    #: (`final do link do botão "Acessar"`, `cabeçalho`).
    variable_labels = SerializerMethodField()

    #: Só para variável de botão de URL: o endereço aprovado com o `{{n}}` no
    #: lugar, para a tela mostrar o link se formando embaixo do campo. É o que
    #: evita a pessoa colar a URL inteira num lugar que só quer o final dela.
    variable_url_templates = SerializerMethodField()

    class Meta:
        model = WhatsAppTemplate
        fields = [
            "id",
            "name",
            "language",
            "category",
            "status",
            "components",
            "variables",
            "variable_labels",
            "variable_url_templates",
        ]

    def get_variables(self, obj) -> list[str]:
        from apps.inbox.template_vars import variaveis_do_template

        return variaveis_do_template(obj)

    def get_variable_labels(self, obj) -> dict:
        from apps.inbox.template_vars import rotulo_da_variavel, variaveis_do_template

        return {
            chave: rotulo_da_variavel(obj, chave)
            for chave in variaveis_do_template(obj)
        }

    def get_variable_url_templates(self, obj) -> dict:
        from apps.inbox.template_vars import modelo_do_link, variaveis_do_template

        modelos = {
            chave: modelo_do_link(obj, chave) for chave in variaveis_do_template(obj)
        }
        return {chave: url for chave, url in modelos.items() if url}
