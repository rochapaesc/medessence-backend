from django.core.exceptions import ValidationError
from django.db.models import BooleanField, CharField, Q, TextField, UniqueConstraint

from apps.core.models import TenantScopedModel


class HttpDestination(TenantScopedModel):
    """
    Destino permitido para o nó de requisição HTTP (RF-FLW-16.1 item a).

    ⚠️ **A URL NÃO se digita no nó.** Este cadastro existe justamente para
    isso: se o autor do fluxo pudesse escrever o endereço, cada nó novo seria
    uma chance de mandar dado de paciente para fora, e não haveria uma lista
    para o operador auditar. Aqui o gestor cadastra o destino UMA vez, a cerca
    é aplicada na hora, e o nó só aponta para esta linha.

    É mais apertado do que a referência do ramo
    (`references/wacrm/src/lib/webhooks/`), que aceita qualquer URL pública:
    lá o corpo é evento de CRM, aqui é dado de saúde.

    O `secret` é o que o destino usa para saber que a chamada veio de nós.
    Vai no cabeçalho, nunca na URL.
    """

    name = CharField(
        verbose_name="Nome",
        max_length=80,
        help_text="Como este destino aparece na lista do nó. Ex.: 'ERP da recepção'.",
    )
    url = TextField(
        verbose_name="Endereço",
        help_text="Precisa ser https, sem usuário e senha embutidos, e apontar para fora da rede interna.",
    )
    secret = CharField(
        verbose_name="Segredo",
        max_length=200,
        blank=True,
        default="",
        help_text=(
            "Enviado no cabeçalho `X-MedEssence-Secret` para o destino "
            "confirmar que a chamada é nossa. Em branco, nada é enviado."
        ),
    )
    is_active = BooleanField(
        verbose_name="Ativo",
        default=True,
        help_text="Desligado, os nós que apontam para ele param de chamar e seguem pela saída de falha.",
    )

    class Meta:
        verbose_name = "Destino de requisição"
        verbose_name_plural = "Destinos de requisição"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_http_destination_name",
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        """
        A cerca roda no CADASTRO, para o gestor descobrir o problema enquanto
        está olhando o formulário.

        ⚠️ Isto **não** dispensa a checagem no disparo: o nome cadastrado hoje
        apontando para fora pode apontar para dentro amanhã, sem ninguém
        tocar aqui. Ver `apps.core.ssrf.check_public_url`.
        """
        super().clean()
        from apps.core.ssrf import BlockedDestination, check_public_url

        try:
            check_public_url(self.url)
        except BlockedDestination as erro:
            raise ValidationError({"url": str(erro)}) from erro
