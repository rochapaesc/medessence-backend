from rest_framework.serializers import ModelSerializer, SerializerMethodField, ValidationError

from apps.automation.models import HttpDestination


class HttpDestinationSerializer(ModelSerializer):
    """
    O cadastro de destinos permitidos (RF-FLW-16.1 item a).

    ⚠️ O `secret` é **de ida só**: ele é escrito e nunca devolvido. Devolvê-lo
    faria a tela de configuração virar um jeito de ler o segredo de qualquer
    integração da clínica, para qualquer gestor que abrisse a listagem. Em
    lugar dele sai `tem_segredo`, que é o que a tela precisa saber.
    """

    tem_segredo = SerializerMethodField()

    class Meta:
        model = HttpDestination
        fields = ["id", "name", "url", "secret", "tem_segredo", "is_active"]
        extra_kwargs = {"secret": {"write_only": True, "required": False}}

    def get_tem_segredo(self, obj) -> bool:
        return bool(obj.secret)

    def validate_url(self, value):
        """
        A cerca, no momento em que o gestor ainda está olhando o formulário.

        Aqui, e não só no `clean()` do modelo, porque o DRF não chama
        `full_clean` sozinho - sem isto a URL entraria pelo endpoint sem
        passar por checagem nenhuma, que é o furo clássico de validar só no
        admin do Django.
        """
        from apps.core.ssrf import BlockedDestination, check_public_url

        try:
            check_public_url(value)
        except BlockedDestination as erro:
            raise ValidationError(str(erro)) from erro
        return value.strip()
