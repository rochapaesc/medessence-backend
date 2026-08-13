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
        ]
        read_only_fields = ["meta_template_id", "rejection_reason"]

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

        channel = Channel.objects.filter(clinic=clinic).first()
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


#: Editar só faz sentido nestes: PENDING está em revisão (a Meta recusa a
#: edição), e o que nunca foi para lá se CRIA, não se edita.
EDITAVEIS = {"APPROVED", "REJECTED", "PAUSED"}


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
        if not template.meta_template_id:
            raise ValidationError(
                "Este template nunca chegou a ser enviado para a Meta. "
                "Crie um novo em vez de editar este."
            )
        if template.status not in EDITAVEIS:
            raise ValidationError(
                f"Template em revisão ({template.status}) não pode ser editado. "
                "Espere o resultado da Meta."
            )
        if attrs.get("name") != template.name:
            raise ValidationError("O nome de um template não pode mudar depois de criado.")
        if attrs.get("language") != template.language:
            raise ValidationError("O idioma de um template não pode mudar depois de criado.")
        return super().validate(attrs)

    def update(self, instance, validated_data):
        from apps.inbox.models import Channel
        from apps.inbox.template_builder import montar_para_a_meta
        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        channel = Channel.objects.filter(clinic=instance.clinic).first()
        if channel is None:
            raise ValidationError("Esta clínica não tem canal de WhatsApp configurado.")

        payload = montar_para_a_meta(dict(validated_data))
        try:
            get_whatsapp_provider(channel).update_template(
                instance.meta_template_id, payload
            )
        except WhatsAppError as exc:
            # ⚠️ O que está no ar na Meta continua o de ANTES: guardar a versão
            # nova aqui faria a tela mostrar um texto que o paciente não vai
            # receber. O motivo fica registrado e o template segue como estava.
            instance.rejection_reason = str(exc)
            instance.save(update_fields=["rejection_reason"])
            raise ValidationError(str(exc)) from exc

        instance.category = payload["category"]
        instance.components = payload["components"]
        # Toda edição volta para a fila de revisão da Meta, e o motivo da
        # recusa anterior deixa de valer.
        instance.status = "PENDING"
        instance.rejection_reason = ""
        instance.save(
            update_fields=["category", "components", "status", "rejection_reason"]
        )
        return instance
