"""
Auditoria do admin do Django (§15, 20/08/2026).

O admin é a porta do OPERADOR: é por ali que uma clínica nasce, que alguém
ganha vínculo de gestor e que uma conta é ligada ou desligada. Nada disso
passava pela API, então nada disso aparecia na auditoria do produto, e a
pergunta "quem deu acesso a esta pessoa" ficava sem resposta.

O Django tem o `LogEntry` dele, mas ele mora em outra tabela, com outro
vocabulário e sem clínica: a tela de auditoria não o lê, e o gestor não tem
como abrir o admin para conferir.

⚠️ Só os modelos que decidem ACESSO e TENANT usam este mixin. Auditar todo o
admin encheria o log de manutenção de catálogo, e o ruído é o que faz um
registro de auditoria deixar de ser lido.
"""

from apps.core.audit import log_action, snapshot_instance
from apps.core.models.audit_log import AuditAction


class AuditedAdminMixin:
    """Registra no AuditLog o que o operador salva e apaga pelo admin."""

    # Sobrescreva quando o nome do modelo não for o rótulo desejado.
    audit_resource: str = ""

    # De onde sai a clínica do registro. Vazio = evento sem tenant (uma conta
    # é global); "self" quando o próprio objeto É a clínica.
    audit_clinic_field: str = ""

    def _audit_resource(self, obj) -> str:
        return self.audit_resource or obj.__class__.__name__

    def _audit_clinic(self, obj):
        if not self.audit_clinic_field:
            return None
        if self.audit_clinic_field == "self":
            return obj
        return getattr(obj, self.audit_clinic_field, None)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        log_action(
            user=request.user,
            action=AuditAction.UPDATE if change else AuditAction.CREATE,
            resource=self._audit_resource(obj),
            resource_id=obj.pk,
            # `changed_data` é a mesma régua do AuditMixin da API: QUAIS
            # campos mudaram, nunca os valores.
            payload={
                "changed_fields": list(form.changed_data),
                "origem": "admin",
            },
            request=request,
            clinic=self._audit_clinic(obj),
        )

    def delete_model(self, request, obj):
        antes = snapshot_instance(obj)
        clinic = self._audit_clinic(obj)
        resource = self._audit_resource(obj)
        resource_id = obj.pk

        super().delete_model(request, obj)

        log_action(
            user=request.user,
            action=AuditAction.DELETE,
            resource=resource,
            resource_id=resource_id,
            payload={"before": antes, "origem": "admin"},
            request=request,
            clinic=clinic,
        )

    def delete_queryset(self, request, queryset):
        """
        A exclusão em massa da lista não passa por `delete_model` (o Django
        chama o `delete()` do queryset direto). Sem isto, apagar dez vínculos
        de uma vez não deixaria linha nenhuma.
        """
        alvos = [
            (obj.pk, self._audit_resource(obj), self._audit_clinic(obj), snapshot_instance(obj))
            for obj in queryset
        ]

        super().delete_queryset(request, queryset)

        for resource_id, resource, clinic, antes in alvos:
            log_action(
                user=request.user,
                action=AuditAction.DELETE,
                resource=resource,
                resource_id=resource_id,
                payload={"before": antes, "origem": "admin"},
                request=request,
                clinic=clinic,
            )
