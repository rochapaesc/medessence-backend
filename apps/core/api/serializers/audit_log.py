from rest_framework.serializers import ModelSerializer, SerializerMethodField

from apps.core.models import AuditLog

# Ações cujo alvo é um paciente — o rótulo da linha vira o nome dele.
PATIENT_RESOURCES = {"Patient"}


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
