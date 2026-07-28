"""
`manage.py wa_channel` — cadastro do canal WhatsApp da clínica.

O que se protege aqui é o manuseio do SEGREDO: o token entra por variável de
ambiente (nunca por argumento, que vazaria no `ps` e no histórico do shell) e
o comando não pode imprimi-lo de volta em nenhuma saída.
"""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel

TOKEN = "EAAG-token-secreto-de-teste-123456"
PHONE_ID = "109876543210987"
WABA_ID = "222333444555666"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("WA_TOKEN", TOKEN)
    monkeypatch.setenv("WA_PHONE_ID", PHONE_ID)
    monkeypatch.setenv("WA_WABA_ID", WABA_ID)


def _run(*args, **kwargs):
    out = StringIO()
    call_command("wa_channel", *args, stdout=out, **kwargs)
    return out.getvalue()


def test_cria_canal_meta_com_token_cifrado(db, clinic_a, env):
    saida = _run(clinic_a.slug, "--display-number", "+55 85 99999-0000")

    channel = Channel.objects.get(clinic=clinic_a)
    assert channel.provider == WhatsAppProviderKind.META
    assert channel.phone_number_id == PHONE_ID
    assert channel.waba_id == WABA_ID
    assert channel.display_number == "+55 85 99999-0000"
    assert channel.credentials["access_token"] == TOKEN
    assert "criado" in saida.lower()


def test_nunca_imprime_o_token(db, clinic_a, env):
    """O terminal fica no histórico e em log de CI — o segredo não vai junto."""
    saida = _run(clinic_a.slug) + _run(clinic_a.slug, "--show")

    assert TOKEN not in saida
    assert TOKEN[-4:] in saida, "mas mostra o fim, para conferir que é o valor certo"


def test_atualiza_canal_existente_sem_duplicar(db, clinic_a, env, monkeypatch):
    _run(clinic_a.slug)
    monkeypatch.setenv("WA_TOKEN", "token-rotacionado-99999")

    _run(clinic_a.slug)

    # Constraint: um canal por clínica. Rotacionar token não pode criar outro.
    assert Channel.objects.filter(clinic=clinic_a).count() == 1
    assert Channel.objects.get(clinic=clinic_a).credentials["access_token"] == (
        "token-rotacionado-99999"
    )


def test_sem_variaveis_de_ambiente_recusa_e_ensina(db, clinic_a, monkeypatch):
    for nome in ("WA_TOKEN", "WA_PHONE_ID", "WA_WABA_ID"):
        monkeypatch.delenv(nome, raising=False)

    with pytest.raises(CommandError) as exc:
        _run(clinic_a.slug)

    assert "WA_TOKEN" in str(exc.value)
    assert Channel.objects.filter(clinic=clinic_a).count() == 0


def test_telefone_no_lugar_do_phone_id_e_recusado(db, clinic_a, env, monkeypatch):
    """Erro comum de quem copia da tela errada do painel."""
    monkeypatch.setenv("WA_PHONE_ID", "+55 85 99999-0000")

    with pytest.raises(CommandError, match="phone_number_id"):
        _run(clinic_a.slug)

    assert Channel.objects.filter(clinic=clinic_a).count() == 0


def test_clinica_inexistente_recusa(db, env):
    with pytest.raises(CommandError, match="não existe"):
        _run("clinica-que-nao-existe")


def test_show_em_clinica_sem_canal_avisa(db, clinic_a):
    saida = _run(clinic_a.slug, "--show")
    assert "ainda não tem canal" in saida


def test_verify_sem_canal_recusa(db, clinic_a):
    with pytest.raises(CommandError, match="não tem canal"):
        _run(clinic_a.slug, "--verify")


def test_chamada_ao_pywa_bate_com_a_assinatura_real():
    """
    Contrato com a biblioteca: `phone_id` é KEYWORD-ONLY no PyWa (assinatura
    com `*`). Chamar posicionalmente estourava só na hora do --verify, contra
    a Meta de verdade — o teste anterior parava no caso "sem canal" e nunca
    chegava na chamada (28/07/2026).

    Amarrado à assinatura REAL: se um upgrade do PyWa mudar isso, quebra aqui
    e não na calibração.
    """
    import inspect

    from pywa import WhatsApp

    sig = inspect.signature(WhatsApp.get_business_phone_number)
    sig.bind(None, phone_id="123456")  # como o comando chama — não pode levantar

    with pytest.raises(TypeError):
        sig.bind(None, "123456")  # posicional: é exatamente o que quebrou
