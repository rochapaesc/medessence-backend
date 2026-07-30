"""
Trava de dado de produção (TEMPORÁRIA — 28/07/2026).

Enquanto o MedEssence é desenvolvido em cima da clínica REAL (Instituto Dr.
Agamenon Bastos, 5k+ pacientes e 10k+ consultas espelhados da vSaúde), um
DELETE acidental não fica só aqui: o write-through traduz para o `Delete` do
EHR e apaga do prontuário de verdade.

Esta trava recusa exclusão de registro que veio do EHR (tem `external_id`).
O que nasceu local continua apagável — é dado nosso.

Desligar quando o ambiente de desenvolvimento deixar de apontar para a
clínica real: `EHR_DATA_GUARD=False` no .env.

⚠️ Isto NÃO substitui o kill-switch do push (`manage.py ehr_push <slug> off`).
São camadas diferentes: a trava impede a intenção; o kill-switch impede a
propagação. Em dúvida, ligue as duas.
"""

from django.conf import settings
from rest_framework.exceptions import PermissionDenied


class EHRDataGuardMixin:
    """
    Recusa `destroy` de registro espelhado do EHR enquanto a trava estiver
    ligada. Aplicar nos viewsets cujo delete propaga para o prontuário.
    """

    def perform_destroy(self, instance):
        if _guard_blocks(instance):
            raise PermissionDenied(
                "Trava de dados de produção ligada: este registro veio do "
                "prontuário eletrônico e não pode ser excluído por aqui: a "
                "exclusão seria propagada para o EHR. Para liberar, defina "
                "EHR_DATA_GUARD=False no ambiente."
            )
        super().perform_destroy(instance)


def _guard_blocks(instance) -> bool:
    if not getattr(settings, "EHR_DATA_GUARD", True):
        return False
    # Só o que veio do EHR: registro local é nosso e continua apagável.
    if not getattr(instance, "external_id", ""):
        return False
    clinic = getattr(instance, "clinic", None)
    return bool(clinic and clinic.ehr_provider)


def guard_is_on(clinic) -> bool:
    """Para o front saber que deve esconder/desabilitar o que exclui."""
    return bool(
        getattr(settings, "EHR_DATA_GUARD", True) and clinic and clinic.ehr_provider
    )
