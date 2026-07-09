from django.db.models import Manager

from apps.core.querysets.querysets import SoftDeleteQuerySet


class ActiveManager(Manager):
    """
    Manager padrão para modelos que utilizam soft delete.

    Retorna apenas registros ativos (deleted_at IS NULL).
    Substitui o comportamento padrão do Django para evitar que
    registros soft-deletados apareçam nas consultas comuns.

    Exemplo:
        Model.objects.all()  -> retorna apenas registros ativos
    """

    def get_queryset(self):
        """
        Retorna um SoftDeleteQuerySet filtrado apenas com registros ativos.
        """
        return SoftDeleteQuerySet(self.model, using=self._db).alive()
