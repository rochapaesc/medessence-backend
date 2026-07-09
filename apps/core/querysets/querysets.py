from django.db.models import QuerySet
from django.utils import timezone


class SoftDeleteQuerySet(QuerySet):
    """
    QuerySet customizado para implementar soft delete.

    Sobrescreve o método delete() para evitar exclusão física
    dos registros, marcando-os com timestamp em `deleted_at`.

    Fornece métodos auxiliares para:
        - hard delete (exclusão permanente)
        - filtrar registros ativos
        - filtrar registros deletados
    """

    def delete(self):
        """
        Executa soft delete em massa.

        Em vez de remover registros do banco,
        atualiza o campo `deleted_at` com o timestamp atual.

        Exemplo:
            Model.objects.filter(...).delete()
        """
        return super().update(deleted_at=timezone.now())

    def hard_delete(self):
        """
        Executa exclusão permanente no banco de dados.

        Deve ser usado com cautela.

        Exemplo:
            Model.all_objects.filter(...).hard_delete()
        """
        return super().delete()

    def alive(self):
        """
        Retorna apenas registros ativos (não deletados).
        """
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        """
        Retorna apenas registros soft-deletados.
        """
        return self.filter(deleted_at__isnull=False)

    def restore(self):
        """Reverte soft delete em massa.

        Exemplo:
            Model.all_objects.filter(...).restore()
        """
        return super().update(deleted_at=None)
