import re

from rest_framework.serializers import ModelSerializer, ValidationError

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
    class Meta:
        model = WhatsAppTemplate
        fields = ["id", "name", "language", "category", "status", "components"]
