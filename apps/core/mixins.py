from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.api.permissions import IsPlatformAdmin
from apps.core.audit import log_action, sanitize_payload, snapshot_instance
from apps.core.exceptions import ConflictException


class AuditMixin:
    """
    Mixin para ViewSets que registra automaticamente as ações de criação,
    atualização e exclusão no log de auditoria.

    Coopera com a cadeia de `perform_*` (chama `super()`), então compõe com
    o escopo por clínica — a injeção da clínica ativa no create continua
    acontecendo no viewset escopado.

    Uso:
        class PatientViewSet(AuditMixin, ClinicScopedModelViewSet):
            audit_resource = "Patient"
    """

    audit_resource: str = ""

    def get_audit_resource(self) -> str:
        if not self.audit_resource:
            raise NotImplementedError(f"{self.__class__.__name__} deve definir `audit_resource`.")
        return self.audit_resource

    def get_audit_clinic(self):
        """Clínica do contexto ativo, quando o viewset é escopado."""
        return getattr(self, "clinic", None)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        instance = serializer.instance
        log_action(
            user=self.request.user,
            action="CREATE",
            resource=self.get_audit_resource(),
            resource_id=instance.pk,
            payload={"after": sanitize_payload(dict(serializer.data))},
            request=self.request,
            clinic=self.get_audit_clinic(),
        )

    def perform_update(self, serializer):
        # Snapshot ANTES do save — apenas dos campos que serão alterados
        changed_fields = list(serializer.validated_data.keys())
        before = snapshot_instance(serializer.instance, fields=changed_fields)

        super().perform_update(serializer)
        instance = serializer.instance

        log_action(
            user=self.request.user,
            action="UPDATE",
            resource=self.get_audit_resource(),
            resource_id=instance.pk,
            payload={
                "before": before,
                "after": sanitize_payload(dict(serializer.data)),
                "changed_fields": changed_fields,
            },
            request=self.request,
            clinic=self.get_audit_clinic(),
        )

    def perform_destroy(self, instance):
        # Snapshot completo ANTES de deletar — depois o objeto pode ficar inacessível
        before = snapshot_instance(instance)
        resource_id = instance.pk

        super().perform_destroy(instance)  # soft delete via BaseModel

        log_action(
            user=self.request.user,
            action="DELETE",
            resource=self.get_audit_resource(),
            resource_id=resource_id,
            payload={"before": before},
            request=self.request,
            clinic=self.get_audit_clinic(),
        )


class ProtectRelationsMixin:
    """
    Bloqueia o `destroy` (soft-delete) quando a instância ainda tem vínculos
    ativos (não-deletados). O RESTRICT do banco só protege hard-delete; como
    aqui o delete é soft (UPDATE em deleted_at), a checagem precisa ser no app.

    Uso:
        class TagViewSet(ProtectRelationsMixin, AuditMixin, ClinicScopedModelViewSet):
            protect_on_delete = {"patient_tags": "atribuição"}  # related_name -> rótulo

    Desativar continua liberado — só o excluir é bloqueado.
    """

    # related_name (reverso) -> rótulo legível no singular
    protect_on_delete = {}

    def perform_destroy(self, instance):
        blockers = []
        for related_name, label in self.protect_on_delete.items():
            # O manager reverso usa o ActiveManager → conta só os não-deletados.
            count = getattr(instance, related_name).count()
            if count:
                blockers.append(f"{count} {label}(s)")
        if blockers:
            raise ConflictException(
                f"Não é possível excluir: há {', '.join(blockers)} vinculado(s). "
                "Desative em vez de excluir, ou remova os vínculos primeiro."
            )
        super().perform_destroy(instance)


class SoftDeleteMixin:
    """
    Adiciona POST /<id>/restore/ para reverter soft delete.

    Requer:
        - O modelo deve herdar de BaseModel (ter `all_objects` e `restore()`).
        - O viewset DEVE também usar AuditMixin (para definir `audit_resource`).

    Permissão configurável por `restore_permission_classes` (default: admin da
    plataforma; em recursos de clínica, use `[IsClinicManager]`). Em viewsets
    escopados a busca respeita a clínica ativa — sem restore cross-tenant.
    """

    restore_permission_classes = [IsPlatformAdmin]

    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, pk=None):
        # O BaseGenericViewSet sobrescreve get_permissions e ignora o
        # `permission_classes` do decorator, então conferimos aqui manualmente.
        for permission_class in self.restore_permission_classes:
            if not permission_class().has_permission(request, self):
                raise PermissionDenied("Você não tem permissão para restaurar registros.")

        if not hasattr(self.model, "all_objects"):
            raise ValidationError("Este recurso não suporta restauração.")

        queryset = self.model.all_objects.all()
        clinic = getattr(self, "clinic", None)
        if clinic is not None:
            queryset = queryset.filter(**{getattr(self, "clinic_lookup", "clinic"): clinic})

        try:
            instance = queryset.get(pk=pk)
        except self.model.DoesNotExist as exc:
            raise NotFound("Registro não encontrado.") from exc

        if instance.deleted_at is None:
            raise ValidationError("Este registro não está deletado.")

        before = snapshot_instance(instance)
        instance.restore()

        log_action(
            user=request.user,
            action="RESTORE",
            resource=getattr(self, "audit_resource", self.model.__name__),
            resource_id=instance.pk,
            payload={"before": before, "after": snapshot_instance(instance)},
            request=request,
            clinic=clinic,
        )

        serializer = self.get_serializer(instance)
        return Response(serializer.data)
