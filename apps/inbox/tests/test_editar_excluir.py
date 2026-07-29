"""
Editar e excluir mensagem (29/07/2026).

O escopo saiu de uma restrição REAL da plataforma, não de preguiça: a Cloud
API não apaga nem edita mensagem já entregue — isso é recurso do aplicativo do
celular, não da API. Nenhum dos três repositórios de referência edita mensagem;
o Chatwoot apaga só do lado dele.

Decisão do usuário: **excluir apenas o que nunca chegou ao paciente** (nota da
equipe e mensagem que falhou) e **editar apenas nota**. O registro fica: é
soft delete, com auditoria.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.choices import MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Message

MESSAGES = "/api/v1/messages/"


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _mensagem(conversation, autor=None, **kwargs):
    kwargs.setdefault("sender_kind", SenderKind.AGENT)
    kwargs.setdefault("kind", MessageKind.TEXT)
    kwargs.setdefault("wa_timestamp", timezone.now())
    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        sent_by=autor,
        **kwargs,
    )


# ────────────────────────────── editar ──────────────────────────────


def test_edita_nota_da_equipe_e_marca_que_foi_reescrita(
    logado, inbox_a, manager_single_clinic
):
    """A equipe LÊ a nota como registro do que se sabia: uma que mudou de
    texto sem avisar faria alguém confiar numa versão que já não é a original."""
    nota = _mensagem(
        inbox_a["conversation"],
        manager_single_clinic,
        body="Paciente prefere manhã",
        is_internal=True,
    )

    resposta = logado.patch(
        f"{MESSAGES}{nota.pk}/", {"body": "Paciente prefere TARDE"}, format="json"
    )

    assert resposta.status_code == 200
    nota.refresh_from_db()
    assert nota.body == "Paciente prefere TARDE"
    assert nota.edited_at is not None


def test_NAO_edita_mensagem_enviada(logado, inbox_a, manager_single_clinic):
    """A Cloud API não edita mensagem entregue. O paciente já leu o original;
    mudar aqui faria a thread contar história que o celular dele desmente."""
    enviada = _mensagem(
        inbox_a["conversation"],
        manager_single_clinic,
        body="Bom dia!",
        provider_message_id="wamid.OK",
        status=MessageStatus.DELIVERED,
    )

    resposta = logado.patch(
        f"{MESSAGES}{enviada.pk}/", {"body": "Boa tarde!"}, format="json"
    )

    assert resposta.status_code == 400
    assert "mande outra" in str(resposta.data)
    enviada.refresh_from_db()
    assert enviada.body == "Bom dia!"


def test_nota_vazia_e_recusada(logado, inbox_a, manager_single_clinic):
    nota = _mensagem(
        inbox_a["conversation"], manager_single_clinic, body="algo", is_internal=True
    )

    resposta = logado.patch(f"{MESSAGES}{nota.pk}/", {"body": "   "}, format="json")

    assert resposta.status_code == 400


# ────────────────────────────── excluir ──────────────────────────────


def test_exclui_nota_mas_o_REGISTRO_fica(logado, inbox_a, manager_single_clinic):
    """Soft delete: some da thread, permanece no banco com a auditoria."""
    nota = _mensagem(
        inbox_a["conversation"], manager_single_clinic, body="rascunho", is_internal=True
    )

    resposta = logado.delete(f"{MESSAGES}{nota.pk}/")

    assert resposta.status_code == 204
    assert not Message.objects.filter(pk=nota.pk).exists()
    guardada = Message.all_objects.get(pk=nota.pk)
    assert guardada.deleted_at is not None
    assert guardada.body == "rascunho"


def test_exclui_mensagem_que_FALHOU(logado, inbox_a, manager_single_clinic):
    """Nunca chegou ao paciente — apagar não cria divergência nenhuma."""
    falha = _mensagem(
        inbox_a["conversation"],
        manager_single_clinic,
        body="não saiu",
        status=MessageStatus.FAILED,
        status_error="Canal desconectado",
    )

    resposta = logado.delete(f"{MESSAGES}{falha.pk}/")

    assert resposta.status_code == 204


def test_NAO_exclui_mensagem_entregue(logado, inbox_a, manager_single_clinic):
    """
    O celular do paciente continuaria mostrando.

    Deixar apagar daria a falsa sensação de que sumiu dos dois lados, que é
    pior do que não ter o botão.
    """
    entregue = _mensagem(
        inbox_a["conversation"],
        manager_single_clinic,
        body="Seu resultado está pronto",
        provider_message_id="wamid.ENTREGUE",
        status=MessageStatus.READ,
    )

    resposta = logado.delete(f"{MESSAGES}{entregue.pk}/")

    assert resposta.status_code == 400
    assert "continuaria mostrando" in str(resposta.data)
    assert Message.objects.filter(pk=entregue.pk).exists()


def test_nota_de_OUTRA_pessoa_nao_e_mexida_por_atendente(
    api_client, attendant_a, manager_single_clinic, inbox_a
):
    """Nota de colega é registro do que AQUELA pessoa soube — não é para outro
    atendente reescrever."""
    nota = _mensagem(
        inbox_a["conversation"], manager_single_clinic, body="do gestor", is_internal=True
    )
    api_client.force_authenticate(attendant_a)

    editar = api_client.patch(f"{MESSAGES}{nota.pk}/", {"body": "mudei"}, format="json")
    excluir = api_client.delete(f"{MESSAGES}{nota.pk}/")

    assert editar.status_code == 403
    assert excluir.status_code == 403


def test_gestor_pode_limpar_nota_de_quem_saiu(
    logado, inbox_a, attendant_a
):
    """Alguém precisa poder limpar o que foi escrito errado quando quem
    escreveu não está mais na clínica."""
    nota = _mensagem(
        inbox_a["conversation"], attendant_a, body="errada", is_internal=True
    )

    resposta = logado.delete(f"{MESSAGES}{nota.pk}/")

    assert resposta.status_code == 204


def test_evento_da_linha_do_tempo_nao_e_alterado(logado, inbox_a):
    evento = _mensagem(
        inbox_a["conversation"],
        kind=MessageKind.ACTIVITY,
        sender_kind=SenderKind.SYSTEM,
        activity_type="resolved",
    )

    resposta = logado.delete(f"{MESSAGES}{evento.pk}/")

    assert resposta.status_code == 400


def test_apagar_a_ultima_refaz_a_previa_da_fila(
    logado, inbox_a, manager_single_clinic
):
    """Sem isto, a lista mostraria um texto que já não existe — e a recepção
    clicaria na conversa procurando algo que sumiu."""
    conversation = inbox_a["conversation"]
    _mensagem(
        conversation,
        manager_single_clinic,
        body="a que fica",
        wa_timestamp=timezone.now() - timedelta(minutes=5),
    )
    ultima = _mensagem(
        conversation, manager_single_clinic, body="a que sai", is_internal=True
    )
    conversation.refresh_from_db()
    assert conversation.last_message_preview == "a que sai"

    logado.delete(f"{MESSAGES}{ultima.pk}/")

    conversation.refresh_from_db()
    assert conversation.last_message_preview == "a que fica"
