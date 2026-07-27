"""
Expurgo do log de auditoria (§15) — retenção de 5 anos.

Apagar log é irreversível e o registro é a prova de quem acessou o quê: o
comando é ENSAIO por padrão, e o teste existe principalmente para garantir
que ninguém apague nada sem pedir.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction


def _evento(clinic, dias_atras: int):
    log = AuditLog.objects.create(
        clinic=clinic, action=AuditAction.CREATE, resource="Patient", resource_id="1"
    )
    AuditLog.objects.filter(pk=log.pk).update(
        timestamp=timezone.now() - timedelta(days=dias_atras)
    )
    return log


@pytest.fixture
def logs(db, clinic_a, clinic_b):
    return {
        "recente": _evento(clinic_a, 30),
        "antigo": _evento(clinic_a, 365 * 6),
        "antigo_outra_clinica": _evento(clinic_b, 365 * 6),
    }


def _run(**kwargs):
    out = StringIO()
    call_command("purge_audit_logs", stdout=out, **kwargs)
    return out.getvalue()


def test_ensaio_nao_apaga_nada(logs):
    saida = _run()

    assert AuditLog.objects.count() == 3
    assert "Ensaio" in saida
    assert "--apply" in saida


def test_apply_apaga_so_o_que_passou_da_janela(logs):
    _run(apply=True)

    restantes = set(AuditLog.objects.values_list("pk", flat=True))
    assert logs["recente"].pk in restantes
    assert logs["antigo"].pk not in restantes
    assert logs["antigo_outra_clinica"].pk not in restantes


def test_escopo_por_clinica(logs, clinic_a):
    _run(apply=True, clinic=clinic_a.slug)

    restantes = set(AuditLog.objects.values_list("pk", flat=True))
    assert logs["antigo"].pk not in restantes
    assert logs["antigo_outra_clinica"].pk in restantes, "a outra clínica não foi tocada"


def test_janela_menor_alcanca_mais_eventos(logs):
    """--years 1 leva junto o de 30 dias? Não: 30 dias ainda está dentro."""
    _run(apply=True, years=1)

    assert logs["recente"].pk in set(AuditLog.objects.values_list("pk", flat=True))


def test_recusa_janela_invalida(logs):
    with pytest.raises(CommandError):
        _run(years=0)
    assert AuditLog.objects.count() == 3


def test_clinica_inexistente_e_erro(logs):
    with pytest.raises(CommandError):
        _run(clinic="nao-existe", apply=True)
    assert AuditLog.objects.count() == 3
