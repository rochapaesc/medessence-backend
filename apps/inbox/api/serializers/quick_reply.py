import re

from rest_framework.serializers import (
    CharField,
    DictField,
    ListField,
    ModelSerializer,
    Serializer,
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
            # Criado por aqui, e não só sincronizado: é o que permite editar e
            # apagar esta variante depois.
            "meta_template_id",
            # Por que a Meta recusou. A tela mostra para a clínica corrigir em
            # cima do que escreveu, em vez de recomeçar.
            "rejection_reason",
            # ⚠️ Vermelho é o passo ANTES de a Meta pausar o template sozinha,
            # e template pausado para de enviar no meio de um fluxo. A tela
            # avisa enquanto ainda dá para agir.
            "quality_score",
        ]
        read_only_fields = [
            "meta_template_id",
            "rejection_reason",
            "quality_score",
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


class WhatsAppTemplateCreateSerializer(Serializer):
    """
    Criar template e mandar para a revisão da Meta (RF-INB-3.2).

    ⚠️ A validação de VERDADE mora em `template_builder`, e não aqui: ela é a
    mesma para a tela, para o import e para qualquer caminho futuro, e as
    regras da Meta não cabem em `max_length` de campo. O que este serializer
    faz é o formato (tipos e obrigatórios) e a conversa com a Meta.
    """

    name = CharField(max_length=120)
    category = CharField(max_length=30)
    language = CharField(max_length=10, default="pt_BR")
    body = CharField()
    footer = CharField(required=False, allow_blank=True)
    header_format = CharField(required=False, allow_blank=True)
    header_text = CharField(required=False, allow_blank=True)
    buttons = ListField(child=DictField(), required=False)
    #: `{"body": ["Ivanita", "Oeiras"], "header": ["hoje"]}` — o que o revisor
    #: HUMANO da Meta lê para aprovar.
    examples = DictField(required=False)

    def to_representation(self, instance):
        """
        Devolve o template como ele é LIDO, e não os campos de entrada.

        Quem cria recebe a mesma coisa que o GET devolve - inclusive o status
        e o motivo da recusa, que é o que a tela precisa mostrar em seguida. E
        sem isto o `AuditMixin` estoura ao serializar a instância com os
        campos do formulário (`body`, `footer`), que não existem no modelo.
        """
        return WhatsAppTemplateSerializer(instance, context=self.context).data

    def validate(self, attrs):
        from apps.inbox.template_builder import TemplateInvalido, validar

        try:
            validar(dict(attrs))
        except TemplateInvalido as exc:
            # Mensagem de campo, em português, ANTES de gastar uma chamada à
            # Meta - que responderia 400 genérico com motivo opaco.
            raise ValidationError(str(exc)) from exc
        return attrs

    def create(self, validated_data):
        from apps.inbox.models import Channel, WhatsAppTemplate
        from apps.inbox.template_builder import montar_para_a_meta, status_normalizado
        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        clinic = self.context["clinic"]
        if WhatsAppTemplate.objects.filter(
            clinic=clinic,
            name=validated_data["name"],
            language=validated_data["language"],
        ).exists():
            raise ValidationError(
                f'Já existe um template chamado "{validated_data["name"]}" '
                "neste idioma."
            )

        # ⚠️ Nunca o canal de teste (RF-FLW-25.5): criar template por ele
        # "daria certo" no FAKE e nada chegaria à revisão da Meta.
        channel = Channel.objects.filter(clinic=clinic, is_test=False).first()
        if channel is None:
            raise ValidationError(
                "Esta clínica não tem canal de WhatsApp configurado."
            )

        payload = montar_para_a_meta(dict(validated_data))
        comum = {
            "clinic": clinic,
            "name": payload["name"],
            "language": payload["language"],
            "category": payload["category"],
            "components": payload["components"],
        }
        try:
            criado = get_whatsapp_provider(channel).create_template(payload)
        except WhatsAppError as exc:
            # ⚠️ Template recusado NÃO some. Fica como rascunho local com o
            # motivo, para a clínica corrigir em cima do que escreveu em vez
            # de recomeçar do zero (RF-INB-3.2.5).
            return WhatsAppTemplate.objects.create(
                **comum, status="REJECTED", rejection_reason=str(exc)
            )

        return WhatsAppTemplate.objects.create(
            **comum,
            status=status_normalizado(criado.status),
            meta_template_id=criado.id,
        )


#: Editar só faz sentido nestes: PENDING está em revisão e a Meta recusa a
#: edição enquanto ela não termina.
EDITAVEIS = {"APPROVED", "REJECTED", "PAUSED"}

#: Os que NÃO se edita, em português. A mensagem de erro vai para a tela, e
#: `PENDING_DELETION` não diz nada para quem atende no balcão.
STATUS_LEGIVEL = {
    "PENDING": "em revisão",
    "DISABLED": "desativado",
    "IN_APPEAL": "em recurso",
    "PENDING_DELETION": "sendo apagado",
}


def _id_na_meta(provider, template) -> str:
    """
    Descobre o id da variante na Meta quando não o temos guardado.

    ⚠️ Todo template anterior a 13/08/2026 está assim: a sincronização recebia
    o `id` no `get_templates` e o DESCARTAVA. Sem isto, a clínica não
    conseguiria editar justamente os templates que ela já tem.

    Casa por nome E idioma: o mesmo nome existe uma vez por língua, e pegar o
    primeiro editaria a variante errada.

    Vazio significa "a Meta não tem este template" - e quem chama trata isso
    criando, não falhando. Erro de comunicação PROPAGA de propósito: sem saber
    se ele existe lá, criar duplicaria o nome quando a Meta voltasse.
    """
    for achado in provider.list_templates():
        if achado.name == template.name and achado.language == template.language:
            return achado.meta_id
    return ""


class WhatsAppTemplateEditSerializer(WhatsAppTemplateCreateSerializer):
    """
    Reescrever um template que já está na Meta (RF-INB-3.2.7).

    ⚠️ A Meta SUBSTITUI os componentes inteiros, não aplica diferença: o
    formulário volta preenchido e manda tudo de novo, inclusive o que não
    mudou. E `name` e `language` não se alteram - ela não deixa renomear nem
    trocar o idioma de um template existente.
    """

    def validate(self, attrs):
        template = self.instance
        if template.status not in EDITAVEIS:
            # ⚠️ Sem o status CRU na frase. `IN_APPEAL` e `PENDING_DELETION`
            # apareciam em inglês para quem só quer corrigir um texto, e a
            # palavra "revisão" nem descrevia os dois.
            raise ValidationError(
                f"A Meta ainda está resolvendo este template ({STATUS_LEGIVEL.get(template.status, 'aguardando')}) "
                "e não aceita edição enquanto isso. Espere o resultado dela."
            )
        if attrs.get("name") != template.name:
            raise ValidationError("O nome de um template não pode mudar depois de criado.")
        if attrs.get("language") != template.language:
            raise ValidationError("O idioma de um template não pode mudar depois de criado.")
        return super().validate(attrs)

    def _reenviar(self, instance, provider, payload):
        """
        Manda para a Meta um template que nunca chegou a existir lá.

        Mesma regra do `create`: se ela recusar de novo, o texto continua
        salvo com o motivo NOVO, para a clínica corrigir mais uma vez em vez
        de recomeçar.
        """
        from apps.inbox.template_builder import status_normalizado
        from apps.integrations.whatsapp.exceptions import WhatsAppError

        instance.category = payload["category"]
        instance.components = payload["components"]
        try:
            criado = provider.create_template(payload)
        except WhatsAppError as exc:
            instance.status = "REJECTED"
            instance.rejection_reason = str(exc)
        else:
            instance.status = status_normalizado(criado.status)
            instance.meta_template_id = criado.id
            instance.rejection_reason = ""
        instance.save(
            update_fields=[
                "meta_template_id",
                "category",
                "components",
                "status",
                "rejection_reason",
            ]
        )
        return instance

    def update(self, instance, validated_data):
        from apps.inbox.models import Channel
        from apps.inbox.template_builder import montar_para_a_meta
        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        channel = Channel.objects.filter(clinic=instance.clinic).first()
        if channel is None:
            raise ValidationError("Esta clínica não tem canal de WhatsApp configurado.")

        provider = get_whatsapp_provider(channel)
        payload = montar_para_a_meta(dict(validated_data))

        meta_id = instance.meta_template_id
        if not meta_id:
            try:
                meta_id = _id_na_meta(provider, instance)
            except WhatsAppError as exc:
                # Sem saber se ele existe lá, criar duplicaria o nome.
                raise ValidationError(
                    f"Não deu para falar com a Meta: {exc}"
                ) from exc

        if not meta_id:
            # ⚠️ Template que a Meta RECUSOU na criação nunca chegou a existir
            # lá: "reenviar" é CRIAR de novo, e não editar. Recusar aqui era um
            # beco sem saída - a clínica corrigia o texto, o botão dizia
            # "editar e reenviar", e o sistema respondia que o template não
            # existe na Meta. O nome continua livre na conta, justamente
            # porque a criação falhou.
            return self._reenviar(instance, provider, payload)

        try:
            provider.update_template(meta_id, payload)
        except WhatsAppError as exc:
            # ⚠️ O que está no ar na Meta continua o de ANTES: guardar a versão
            # nova aqui faria a tela mostrar um texto que o paciente não vai
            # receber. O motivo fica registrado e o template segue como estava.
            instance.rejection_reason = str(exc)
            instance.save(update_fields=["rejection_reason"])
            raise ValidationError(str(exc)) from exc

        instance.meta_template_id = meta_id
        instance.category = payload["category"]
        instance.components = payload["components"]
        # Toda edição volta para a fila de revisão da Meta, e o motivo da
        # recusa anterior deixa de valer.
        instance.status = "PENDING"
        instance.rejection_reason = ""
        instance.save(
            update_fields=[
                "meta_template_id",
                "category",
                "components",
                "status",
                "rejection_reason",
            ]
        )
        return instance
