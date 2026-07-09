from django.db.models import DateTimeField, Manager, Model
from django.utils import timezone

from apps.core.managers import ActiveManager
from apps.core.querysets import SoftDeleteQuerySet


class BaseModel(Model):
    created_at = DateTimeField(verbose_name="Criado em", auto_now_add=True)
    updated_at = DateTimeField(verbose_name="Atualizado em", auto_now=True)
    deleted_at = DateTimeField(verbose_name="Deletado em", null=True, blank=True)

    objects = ActiveManager()  # Gerenciador padrão retorna apenas registros ativos
    # Use all_objects para acessar todos os registros, incluindo os soft-deletados
    all_objects = Manager.from_queryset(SoftDeleteQuerySet)()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])

    def hard_delete(self, using=None, keep_parents=False):
        super().delete(using=using, keep_parents=keep_parents)

    def restore(self):
        """Reverte o soft delete: marca o registro como ativo novamente."""
        self.deleted_at = None
        self.save(update_fields=["deleted_at"])
