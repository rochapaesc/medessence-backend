"""
A rede de segurança da mensagem que não saiu (18/08/2026).

Caso real: um passo de sequência disparou, a mensagem foi criada, e ela nunca
foi despachada - sem identificador, sem status e **sem erro nenhum**. Não
aparecia como falha no Inbox nem em lugar nenhum, e o painel da trilha seguia
contando o disparo como feito. O usuário só descobriu porque não recebeu.

O que estes testes prendem é tanto o que a varredura PEGA quanto o que ela NÃO
PODE pegar: nota interna nasce sem status de propósito, e envio em nova
tentativa também.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.automation.tests.conftest import make_channel, make_contact
from apps.inbox.choices import MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Conversation, Message
from apps.inbox.tasks import MINUTOS_PARA_DAR_POR_PERDIDA, varrer_mensagens_paradas

pytestmark = pytest.mark.django_db

VELHA = MINUTOS_PARA_DAR_POR_PERDIDA + 5


@pytest.fixture
def conversa(clinic_a):
    return Conversation.objects.create(
        clinic=clinic_a, channel=make_channel(clinic_a), contact=make_contact(clinic_a)
    )


def _mensagem(conversa, *, minutos, **extra):
    campos = {
        "clinic": conversa.clinic,
        "conversation": conversa,
        "kind": MessageKind.TEMPLATE,
        "sender_kind": SenderKind.BOT,
        "body": "Olá",
        "wa_timestamp": timezone.now(),
        **extra,
    }
    m = Message.objects.create(**campos)
    # `created_at` é auto_now_add: envelhecer exige UPDATE.
    Message.objects.filter(pk=m.pk).update(
        created_at=timezone.now() - timedelta(minutes=minutos)
    )
    return m


def test_mensagem_parada_vira_falha_com_motivo(conversa):
    presa = _mensagem(conversa, minutos=VELHA)

    assert varrer_mensagens_paradas() == {"paradas": 1}

    presa.refresh_from_db()
    assert presa.status == MessageStatus.FAILED
    assert "não chegou a ser enviada" in presa.status_error


def test_recem_criada_fica_quieta(conversa):
    """
    O autoretry do envio tenta por até meia hora, e nesse meio-tempo a mensagem
    fica sem status DE PROPÓSITO. Varrer antes mataria envio que ainda ia
    acontecer.
    """
    nova = _mensagem(conversa, minutos=2)

    assert varrer_mensagens_paradas() == {"paradas": 0}

    nova.refresh_from_db()
    assert nova.status == ""


def test_nota_interna_nao_e_falha(conversa):
    """Ela existe para NÃO sair (RF-ATD-3), e nasce sem status para sempre."""
    nota = _mensagem(conversa, minutos=VELHA, is_internal=True, kind=MessageKind.TEXT)

    assert varrer_mensagens_paradas() == {"paradas": 0}

    nota.refresh_from_db()
    assert nota.status == ""


def test_mensagem_do_paciente_nao_e_falha(conversa):
    """Recebida não tem status de envio: marcar falha ali seria inventar."""
    recebida = _mensagem(
        conversa, minutos=VELHA, sender_kind=SenderKind.CONTACT, kind=MessageKind.TEXT
    )

    assert varrer_mensagens_paradas() == {"paradas": 0}

    recebida.refresh_from_db()
    assert recebida.status == ""


def test_a_que_saiu_nao_e_tocada(conversa):
    enviada = _mensagem(
        conversa, minutos=VELHA, provider_message_id="wamid.ABC", status=MessageStatus.SENT
    )

    assert varrer_mensagens_paradas() == {"paradas": 0}

    enviada.refresh_from_db()
    assert enviada.status == MessageStatus.SENT


def test_rodar_duas_vezes_nao_conta_de_novo(conversa):
    """A segunda passada não pode reavisar a recepção da mesma falha."""
    _mensagem(conversa, minutos=VELHA)

    assert varrer_mensagens_paradas() == {"paradas": 1}
    assert varrer_mensagens_paradas() == {"paradas": 0}


def test_quem_foi_tentado_recebe_aviso_diferente(conversa):
    """
    ⚠️ A diferença entre as duas frases é o que evita mandar duas vezes.

    Caso real de 18/08: a mensagem TINHA sido enviada, a Meta cobrou e
    entregou, e só o comprovante se perdeu. A varredura dizia "não chegou a ser
    enviada", que convida a recepção a reenviar em cima de algo que o paciente
    já leu.
    """
    tentada = _mensagem(conversa, minutos=VELHA, send_attempted_at=timezone.now())

    varrer_mensagens_paradas()

    tentada.refresh_from_db()
    assert tentada.status == MessageStatus.FAILED
    assert "não voltou confirmação" in tentada.status_error
    assert "não mandar duas vezes" in tentada.status_error


def test_quem_nunca_foi_tentado_pode_reenviar_sem_medo(conversa):
    nunca = _mensagem(conversa, minutos=VELHA)
    assert nunca.send_attempted_at is None

    varrer_mensagens_paradas()

    nunca.refresh_from_db()
    assert "não chegou a ser enviada" in nunca.status_error


def test_o_envio_carimba_a_tentativa_antes_de_falar_com_a_meta(conversa, monkeypatch):
    """
    O carimbo tem de sobreviver à chamada estourando NO MEIO: é justamente esse
    o caso em que ele decide se reenviar duplica. A mensagem pode ter chegado
    ao paciente e o nosso lado não saber.
    """
    from apps.inbox.services import send_message
    from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

    def morre_no_meio(*args, **kwargs):
        raise RuntimeError("a rede caiu depois de a Meta receber")

    monkeypatch.setattr(FakeWhatsAppAdapter, "send_text", morre_no_meio)

    m = _mensagem(conversa, minutos=0, kind=MessageKind.TEXT)
    with pytest.raises(RuntimeError):
        send_message(m)

    m.refresh_from_db()
    assert m.send_attempted_at is not None, (
        "sem o carimbo, esta mensagem seria indistinguível de uma que nunca "
        "foi tentada, e a varredura mandaria reenviar"
    )
    assert m.provider_message_id == ""
