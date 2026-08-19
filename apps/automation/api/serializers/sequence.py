from rest_framework.serializers import (
    CharField,
    IntegerField,
    ModelSerializer,
    SerializerMethodField,
)

from apps.automation.models import Sequence, SequenceDispatch, SequenceEnrollment, SequenceStep
from apps.automation.sequences import saidas_padrao


class SequenceStepSerializer(ModelSerializer):
    flow_name = CharField(source="flow.name", read_only=True)
    flow_published = SerializerMethodField()
    # O passo pode ABRIR conversa fora da janela de 24h? Toda conversa que a
    # sequência inicia sai de template (RF-SEQ-5.3), e as telas onde a
    # campanha nasce precisam desta resposta por passo.
    abre_com_modelo = SerializerMethodField()
    #: Anotados pelo viewset do painel (RF-SEQ-11). Ausentes na listagem.
    parados = IntegerField(read_only=True, required=False)
    saidos = IntegerField(read_only=True, required=False)
    pulados = IntegerField(read_only=True, required=False)

    class Meta:
        model = SequenceStep
        fields = [
            "id",
            "order",
            "name",
            "offset_days",
            "send_time",
            "flow",
            "flow_name",
            "flow_published",
            "abre_com_modelo",
            "expire_hours",
            "is_active",
            "parados",
            "saidos",
            "pulados",
        ]
        # RF-SEQ-2.2: a posição é CONSEQUÊNCIA do prazo, não escolha de quem
        # chama. Deixá-la editável seria reabrir a porta para `order` e
        # `offset_days` discordarem, que é o defeito que a regra corrigiu.
        read_only_fields = ["order"]
        # O fluxo é opcional no payload porque o atalho do aviso (RF-SEQ-1.2)
        # o CRIA no servidor: quem manda `aviso` não tem fluxo para mandar.
        extra_kwargs = {"flow": {"required": False}}

    def get_flow_published(self, obj) -> bool:
        """A tela precisa avisar ANTES: passo com fluxo em rascunho não dispara."""
        return obj.flow.current_version_id is not None

    def get_abre_com_modelo(self, obj) -> bool:
        from apps.automation.sequences import abre_com_modelo

        return abre_com_modelo(obj.flow)


class SequenceSerializer(ModelSerializer):
    steps = SequenceStepSerializer(many=True, read_only=True)
    active_enrollments = IntegerField(read_only=True)

    class Meta:
        model = Sequence
        fields = [
            "id",
            "name",
            "is_active",
            "is_marketing",
            "enroll_on_appointment",
            "conversion_days",
            "exit_on_reply",
            "exit_on_appointment",
            "steps",
            "active_enrollments",
        ]

    def create(self, validated_data):
        """
        As duas saídas nascem pelo TIPO da trilha (RF-SEQ-6.2).

        O padrão mora aqui e não no `default` do campo porque depende de outro
        campo do mesmo objeto. Quem mandar o valor explícito vence: isto é
        padrão de nascimento, não regra de negócio.
        """
        padrao = saidas_padrao(
            is_marketing=validated_data.get("is_marketing", True),
            enroll_on_appointment=validated_data.get("enroll_on_appointment", False),
        )
        for campo, valor in padrao.items():
            validated_data.setdefault(campo, valor)
        return super().create(validated_data)


class ProximoDisparoSerializer(ModelSerializer):
    """
    Uma linha da tabela de próximos disparos (RF-SEQ-11).

    Traz o nome de quem vai receber e, quando há algo segurando, o motivo e
    desde quando. É o "segurando há X" da tela, e sem ele a pessoa veria uma
    hora que já passou sem nenhuma explicação.
    """

    quem = SerializerMethodField()
    step_name = SerializerMethodField()

    class Meta:
        model = SequenceEnrollment
        fields = [
            "id",
            "quem",
            "current_step",
            "step_name",
            "next_dispatch_at",
            "held_since",
            "hold_reason",
        ]

    def get_quem(self, obj) -> str:
        if obj.patient_id and obj.patient:
            return obj.patient.name
        return obj.contact.display_name or obj.contact.wa_id

    def get_step_name(self, obj) -> str:
        return str(obj.current_step) if obj.current_step_id else ""


class SequenceEnrollmentSerializer(ModelSerializer):
    sequence_name = CharField(source="sequence.name", read_only=True)
    current_step_name = SerializerMethodField()

    class Meta:
        model = SequenceEnrollment
        fields = [
            "id",
            "sequence",
            "sequence_name",
            "contact",
            "patient",
            "appointment",
            "anchor_at",
            "source",
            "status",
            "end_reason",
            "current_step",
            "current_step_name",
            "next_dispatch_at",
            "created_at",
        ]
        read_only_fields = ["status", "end_reason", "current_step", "next_dispatch_at"]

    def get_current_step_name(self, obj) -> str:
        return str(obj.current_step) if obj.current_step_id else ""


class SequenceDispatchSerializer(ModelSerializer):
    step_name = SerializerMethodField()

    class Meta:
        model = SequenceDispatch
        fields = [
            "id",
            "step",
            "step_name",
            "scheduled_for",
            "resolved_at",
            "status",
            "skip_reason",
            "flow_run",
        ]

    def get_step_name(self, obj) -> str:
        return str(obj.step)
