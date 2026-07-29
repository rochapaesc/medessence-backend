"""
Assinatura de quem escreve (29/07/2026).

Do outro lado é um número só — o da clínica. Sem o nome, o paciente não sabe
se fala com a recepção, com a doutora ou com um robô.

O Chatwoot assina no FIM, com delimitador `--` e texto livre por usuário
(`User.message_signature`). Aqui é o nome no COMEÇO, em negrito e com dois
pontos — formato escolhido pelo usuário — e gravado no corpo, para a thread mostrar exatamente
o que chegou no celular do paciente.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.models import Message

MESSAGES = "/api/v1/messages/"


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


@pytest.fixture(autouse=True)
def janela_aberta(inbox_a):
    c = inbox_a["conversation"]
    c.last_inbound_at = timezone.now() - timedelta(minutes=5)
    c.save(update_fields=["last_inbound_at"])
    return c


def _nome(user):
    return user.get_full_name()


def test_mensagem_do_atendente_sai_assinada(logado, inbox_a, manager_single_clinic):
    resposta = logado.post(
        MESSAGES,
        {"conversation": inbox_a["conversation"].id, "body": "Bom dia, como posso ajudar"},
        format="json",
    )

    assert resposta.status_code == 201
    esperado = f"*{_nome(manager_single_clinic)}:*\nBom dia, como posso ajudar"
    assert resposta.data["body"] == esperado
    # Gravado assinado: a thread mostra o que o paciente recebeu, não outra
    # versão da mesma fala.
    assert Message.objects.get(pk=resposta.data["id"]).body == esperado


def test_a_previa_da_fila_acompanha_o_texto_assinado(logado, inbox_a):
    """A prévia é montada pelo signal na criação — se a assinatura chegasse
    depois, a lista mostraria a versão sem nome."""
    logado.post(
        MESSAGES,
        {"conversation": inbox_a["conversation"].id, "body": "Segue o preparo"},
        format="json",
    )

    inbox_a["conversation"].refresh_from_db()
    assert "Segue o preparo" in inbox_a["conversation"].last_message_preview


def test_nota_da_equipe_NAO_e_assinada(logado, inbox_a):
    """Ela não sai daqui, e a autoria já aparece no rodapé do balão."""
    resposta = logado.post(
        MESSAGES,
        {
            "conversation": inbox_a["conversation"].id,
            "body": "Paciente prefere manhã",
            "is_internal": True,
        },
        format="json",
    )

    assert resposta.data["body"] == "Paciente prefere manhã"


def test_template_NAO_e_assinado(logado, inbox_a):
    """O conteúdo é o aprovado pela Meta — mexer nele reprova o envio."""
    inbox_a["conversation"].last_inbound_at = timezone.now() - timedelta(hours=40)
    inbox_a["conversation"].save(update_fields=["last_inbound_at"])

    resposta = logado.post(
        MESSAGES,
        {
            "conversation": inbox_a["conversation"].id,
            "template_name": "confirmacao_consulta",
            "body": "Confirmando sua consulta",
        },
        format="json",
    )

    assert resposta.status_code == 201
    assert resposta.data["body"] == "Confirmando sua consulta"


def test_legenda_de_anexo_NAO_e_assinada(logado, inbox_a, manager_single_clinic):
    """O nome dominaria a linha curta que descreve a foto."""
    from django.core.files.base import ContentFile

    from apps.inbox.choices import MediaState
    from apps.inbox.models import MediaAsset

    media = MediaAsset.objects.create(
        clinic=inbox_a["conversation"].clinic,
        mime_type="image/png",
        filename="foto.png",
        state=MediaState.READY,
    )
    media.stored_file.save("foto.png", ContentFile(b"\x89PNG\r\n\x1a\n0"), save=True)

    resposta = logado.post(
        MESSAGES,
        {
            "conversation": inbox_a["conversation"].id,
            "media": media.pk,
            "caption": "Segue o preparo",
        },
        format="json",
    )

    assert resposta.data["caption"] == "Segue o preparo"


def test_reenvio_NAO_assina_de_novo(logado, inbox_a, manager_single_clinic):
    """A linha já está lá — assinar outra vez empilharia o nome."""
    from apps.inbox.services import assinar_mensagem

    criada = logado.post(
        MESSAGES,
        {"conversation": inbox_a["conversation"].id, "body": "oi"},
        format="json",
    )
    message = Message.objects.get(pk=criada.data["id"])
    antes = message.body

    assinou = assinar_mensagem(message, manager_single_clinic)

    assert assinou is False
    message.refresh_from_db()
    assert message.body == antes
