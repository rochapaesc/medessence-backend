"""
Saúde do processamento (apps/core/health.py).

Veio de uma falha real: o worker ficou dez horas com o código antigo em
memória, os anexos subiam e o envio morria, e nada avisava. Cada teste aqui
corresponde a um dos modos de falha que a gente já viu ou pode ver.
"""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.core.health import (
    CHAVE_BATIMENTO,
    assinatura_do_codigo,
    registrar_batimento,
    saude_do_processamento,
)

COUNTERS = "/api/v1/conversations/counters/"


@pytest.fixture(autouse=True)
def cache_limpo():
    cache.delete(CHAVE_BATIMENTO)
    yield
    cache.delete(CHAVE_BATIMENTO)


def _batimento(*, minutos_atras=0, assinatura=None):
    cache.set(
        CHAVE_BATIMENTO,
        {
            "em": (timezone.now() - timedelta(minutes=minutos_atras)).isoformat(),
            "assinatura": assinatura or assinatura_do_codigo(),
        },
        600,
    )


def test_sem_batimento_nenhum_acusa_processamento_fora():
    """Estado de quem nunca viu o worker rodar — ou parou faz tempo demais."""
    saude = saude_do_processamento()

    assert saude["alive"] is False
    assert "fora do ar" in saude["reason"]


def test_batimento_recente_e_da_mesma_versao_e_saude_boa():
    registrar_batimento()

    saude = saude_do_processamento()

    assert saude["alive"] is True
    assert saude["stale_code"] is False
    assert saude["reason"] == ""


def test_batimento_velho_acusa_com_quanto_tempo_de_silencio():
    """"Parou" sozinho não ajuda; "sem sinal há 8 min" diz o tamanho do
    problema."""
    _batimento(minutos_atras=8)

    saude = saude_do_processamento()

    assert saude["alive"] is False
    assert "8 min" in saude["reason"]


def test_worker_vivo_com_codigo_ANTIGO_e_acusado(monkeypatch):
    """
    O caso de 29/07: o worker estava VIVO e ERRADO ao mesmo tempo.

    Um sinal só de "vivo" daria tudo certo e o defeito continuaria invisível —
    é por isso que `alive` e `stale_code` são perguntas separadas.
    """
    _batimento(assinatura="versao-de-ontem")

    saude = saude_do_processamento()

    assert saude["alive"] is True
    assert saude["stale_code"] is True
    assert "versão antiga" in saude["reason"]


def test_worker_fora_NAO_reclama_de_versao():
    """Com o worker fora, dizer "código antigo" mandaria consertar a coisa
    errada — a assinatura guardada é só a última que existiu."""
    _batimento(minutos_atras=30, assinatura="versao-de-ontem")

    saude = saude_do_processamento()

    assert saude["alive"] is False
    assert saude["stale_code"] is False


def test_assinatura_muda_quando_um_arquivo_muda(tmp_path, monkeypatch):
    """A impressão digital tem de reagir a uma edição — senão o worker
    desatualizado passaria batido."""
    import apps.core.health as health

    (tmp_path / "apps").mkdir()
    alvo = tmp_path / "apps" / "x.py"
    alvo.write_text("a = 1")

    monkeypatch.setattr(health.settings, "BASE_DIR", tmp_path)
    health.assinatura_do_codigo.cache_clear()
    antes = health.assinatura_do_codigo()

    alvo.write_text("a = 2")
    health.assinatura_do_codigo.cache_clear()
    depois = health.assinatura_do_codigo()

    assert antes != depois
    health.assinatura_do_codigo.cache_clear()
