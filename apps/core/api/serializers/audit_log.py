from rest_framework.serializers import ModelSerializer, SerializerMethodField

from apps.core.audit import describe_operation
from apps.core.models import AuditLog

# Ações cujo alvo é um paciente — o rótulo da linha vira o nome dele.
PATIENT_RESOURCES = {"Patient"}

# O nome do modelo é de quem escreve código; a auditoria é lida por gestor.
# Sem isto a coluna "Sobre" mostra "Conversation #482", que não diz nada para
# quem está tentando entender o que houve na clínica.
RESOURCE_LABELS = {
    "Conversation": "Conversa",
    "Message": "Mensagem",
    "Contact": "Contato",
    "Patient": "Paciente",
    "PatientFile": "Arquivo do paciente",
    "ClinicalDocument": "Documento clínico",
    "Appointment": "Agendamento",
    "User": "Usuário",
    "Membership": "Vínculo com a clínica",
    "Clinic": "Clínica",
    "ClinicBusinessHours": "Horário de atendimento",
    "Channel": "Canal do WhatsApp",
    "WhatsAppTemplate": "Template do WhatsApp",
    "Flow": "Fluxo",
    "Sequence": "Sequência",
    "SequenceEnrollment": "Inscrição em sequência",
    "Label": "Assunto",
    "QuickReply": "Resposta rápida",
    "HttpDestination": "Sistema externo",
    "SyncRun": "Sincronização com o prontuário",
}


class AuditLogReadSerializer(ModelSerializer):
    """
    Linha da auditoria: legível sem precisar abrir outra tela.

    Papel do usuário e nome do paciente chegam resolvidos EM LOTE pelo viewset
    (mapas no contexto) — são centenas de linhas por página, e resolver uma a
    uma traria o N+1 junto.
    """

    user = SerializerMethodField()
    action_display = SerializerMethodField()
    resource_label = SerializerMethodField()
    resource_display = SerializerMethodField()
    operation_label = SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "user",
            "action",
            "action_display",
            "resource",
            "resource_id",
            "resource_label",
            "resource_display",
            "operation_label",
            "ip_address",
            "timestamp",
        ]

    def get_user(self, obj):
        if obj.user_id is None:
            # Conta apagada, ou evento sem usuário (login falho com e-mail
            # que não existe). A auditoria não perde a linha por isso.
            return None
        roles = self.context.get("user_roles") or {}
        return {
            "id": obj.user_id,
            "name": obj.user.get_full_name() or obj.user.email,
            "email": obj.user.email,
            "role": roles.get(obj.user_id, ""),
        }

    def get_action_display(self, obj):
        return obj.get_action_display()

    def get_resource_label(self, obj):
        """Nome do paciente, quando o alvo é um. Vazio quando não se aplica."""
        if obj.resource not in PATIENT_RESOURCES:
            return ""
        names = self.context.get("patient_names") or {}
        return names.get(str(obj.resource_id), "")

    def get_resource_display(self, obj):
        """O recurso em português. Cai no nome técnico quando não há tradução:
        linha sem rótulo seria pior que linha com nome de modelo."""
        return RESOURCE_LABELS.get(obj.resource, obj.resource)

    def get_operation_label(self, obj):
        """A frase da ação de tela, quando o evento é uma. Vazia no resto."""
        return describe_operation(obj.payload)


class AuditLogDetailSerializer(AuditLogReadSerializer):
    """
    Detalhe do evento. `changed_fields` diz QUAIS campos mudaram; os valores
    ficam fora de propósito — a auditoria não pode virar um segundo lugar onde
    o dado pessoal mora (a ficha é a fonte).
    """

    changed_fields = SerializerMethodField()

    class Meta(AuditLogReadSerializer.Meta):
        fields = [*AuditLogReadSerializer.Meta.fields, "changed_fields"]

    def get_changed_fields(self, obj):
        payload = obj.payload or {}
        changed = payload.get("changed_fields")
        return list(changed) if isinstance(changed, list) else []


class MyAccessLogSerializer(AuditLogDetailSerializer):
    """
    Linha de "Meus acessos" (§15.2).

    Sem o campo `user`: nesta tela quem agiu é sempre o requisitante. Um
    serializer que não TEM o campo não tem como devolver terceiro - a garantia
    fica na estrutura, não numa checagem que alguém pode remover depois.

    (`user = None` é o modo do DRF de retirar um campo herdado.)
    """

    user = None

    class Meta(AuditLogDetailSerializer.Meta):
        fields = [f for f in AuditLogDetailSerializer.Meta.fields if f != "user"]
