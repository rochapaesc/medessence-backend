"""
O identificador de usuário da Meta (§4.3.3, RF-CON-6, F2.7 fatia C).

A Meta criou nome de usuário no WhatsApp, e com ele **o telefone pode não vir**:
some de quem adotou nome de usuário, não falou com a clínica nos últimos 30
dias e não está na agenda dela. No lugar vem o `user_id`, no formato
`BR.1234...`, que aparece em `contacts[].user_id`, `messages[].from_user_id` e
`statuses[].recipient_user_id`.

⚠️ O que estes testes protegem é a ORDEM: telefone primeiro, identificador
depois. Inverter quebraria o vínculo com a ficha do paciente, que é por
telefone.
"""

import pytest
from django.utils import timezone

from apps.inbox.choices import SenderKind
from apps.inbox.models import Message
from apps.inbox.services import ingest_events
from apps.integrations.whatsapp.events import parse_meta_webhook
from apps.patients.models import Contact

TELEFONE = "5589981191501"
BSUID = "BR.13491208655302741918"
OUTRO_BSUID = "BR.99999999999999999999"


def _inbound(*, telefone=TELEFONE, user_id=BSUID, wamid="wamid.bsuid1", texto="oi"):
    """
    Mensagem recebida. `telefone=""` é o caso da pessoa com nome de usuário:
    a Meta manda o bloco de contato SEM `wa_id` utilizável.
    """
    contato = {"profile": {"name": "Willian"}, "user_id": user_id}
    if telefone:
        contato["wa_id"] = telefone
    mensagem = {
        "id": wamid,
        "timestamp": "1755600000",
        "type": "text",
        "text": {"body": texto},
    }
    if telefone:
        mensagem["from"] = telefone
    if user_id:
        mensagem["from_user_id"] = user_id
    return {
        "entry": [
            {
                "id": "102938475601122",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "109876543210987"},
                            "contacts": [contato],
                            "messages": [mensagem],
                        },
                    }
                ],
            }
        ]
    }


@pytest.fixture
def canal(inbox_a):
    canal = inbox_a["channel"]
    canal.phone_number_id = "109876543210987"
    canal.save()
    return canal


# --------------------------------------------------------------------- #
# O parser
# --------------------------------------------------------------------- #


def test_o_identificador_vem_do_bloco_de_contatos(db):
    """Ele está em `contacts[].user_id` SEMPRE, mesmo para quem não usa nome
    de usuário: é assim que o guardamos ANTES de o telefone sumir."""
    (evento,) = parse_meta_webhook(_inbound())

    assert evento.wa_id == TELEFONE
    assert evento.user_id == BSUID


def test_mensagem_SEM_telefone_ainda_tem_identificador(db):
    (evento,) = parse_meta_webhook(_inbound(telefone=""))

    assert evento.wa_id == ""
    assert evento.user_id == BSUID


def test_o_identificador_da_MENSAGEM_serve_quando_nao_ha_bloco(db):
    """`from_user_id` é o segundo caminho: nem todo payload traz `contacts[]`."""
    payload = _inbound()
    payload["entry"][0]["changes"][0]["value"].pop("contacts")

    (evento,) = parse_meta_webhook(payload)

    assert evento.user_id == BSUID


def test_o_status_traz_o_identificador_de_quem_recebeu(db):
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "109876543210987"},
                            "statuses": [
                                {
                                    "id": "wamid.x",
                                    "status": "delivered",
                                    "recipient_id": TELEFONE,
                                    "recipient_user_id": BSUID,
                                    "timestamp": "1755600100",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    (evento,) = parse_meta_webhook(payload)

    assert evento.user_id == BSUID


# --------------------------------------------------------------------- #
# A resolução do contato
# --------------------------------------------------------------------- #


def test_contato_novo_guarda_os_DOIS_identificadores(clinic_a, canal):
    ingest_events(canal, parse_meta_webhook(_inbound()))

    contato = Contact.objects.get(clinic=clinic_a, wa_id=TELEFONE)
    assert contato.user_id == BSUID


def test_contato_que_ja_existia_ganha_o_identificador(clinic_a, canal):
    """O contato do cadastro (que entrou só com telefone) aprende o
    identificador na primeira mensagem, e é isso que o mantém alcançável
    depois que o telefone sumir."""
    Contact.objects.create(clinic=clinic_a, wa_id=TELEFONE, display_name="Willian")

    ingest_events(canal, parse_meta_webhook(_inbound()))

    assert Contact.objects.get(clinic=clinic_a, wa_id=TELEFONE).user_id == BSUID


def test_o_TELEFONE_manda_na_hora_de_achar_o_contato(clinic_a, canal):
    """
    ⚠️ RF-CON-6.2: dois contatos, um com o telefone e outro com o identificador.
    O telefone vence, porque é ele que liga o contato à ficha do paciente.
    """
    por_telefone = Contact.objects.create(clinic=clinic_a, wa_id=TELEFONE)
    Contact.objects.create(clinic=clinic_a, wa_id="", user_id=BSUID)

    ingest_events(canal, parse_meta_webhook(_inbound()))

    mensagem = Message.objects.get(provider_message_id="wamid.bsuid1")
    assert mensagem.conversation.contact_id == por_telefone.pk


def test_identificador_JA_TOMADO_por_outro_contato_nao_derruba_a_mensagem(
    clinic_a, canal
):
    """
    ⚠️ Achado ao escrever o teste acima: a pessoa apareceu primeiro sem telefone
    e depois com ele, então ela existe em duas linhas. Roubar o identificador
    estouraria a unicidade e a MENSAGEM se perderia. Juntar os dois é fusão de
    contato, decisão de quem atende; aqui o que não pode é derrubar o webhook.
    """
    por_telefone = Contact.objects.create(clinic=clinic_a, wa_id=TELEFONE)
    sem_telefone = Contact.objects.create(clinic=clinic_a, wa_id="", user_id=BSUID)

    ingest_events(canal, parse_meta_webhook(_inbound()))

    assert Message.objects.filter(provider_message_id="wamid.bsuid1").exists()
    por_telefone.refresh_from_db()
    sem_telefone.refresh_from_db()
    assert por_telefone.user_id == "", "não roubou"
    assert sem_telefone.user_id == BSUID, "e o dono continua com ele"


def test_sem_telefone_o_contato_e_achado_pelo_identificador(clinic_a, canal):
    """É o caso que esta fatia existe para resolver."""
    conhecido = Contact.objects.create(
        clinic=clinic_a, wa_id="", user_id=BSUID, display_name="Willian"
    )

    ingest_events(canal, parse_meta_webhook(_inbound(telefone="")))

    mensagem = Message.objects.get(provider_message_id="wamid.bsuid1")
    assert mensagem.conversation.contact_id == conhecido.pk
    assert Contact.objects.filter(clinic=clinic_a).count() == 2, (
        "o do fixture e o conhecido: nenhum contato novo foi inventado"
    )


def test_contato_pode_NASCER_sem_telefone(clinic_a, canal):
    """RF-CON-6.4, a única mudança estrutural da frente."""
    ingest_events(canal, parse_meta_webhook(_inbound(telefone="")))

    contato = Contact.objects.get(clinic=clinic_a, user_id=BSUID)
    assert contato.wa_id == ""
    assert contato.conversations.count() == 1, "e ele conversa normalmente"


def test_quem_chega_sem_telefone_NAO_perde_o_nome(clinic_a, canal):
    """
    ⚠️ Achado rodando o `ensaio_de_coexistencia`: o nome do perfil era indexado
    só pelo telefone, então quem chegava sem ele nascia sem nome e a fila
    mostrava "Contato sem número" para alguém cujo nome a Meta tinha acabado
    de mandar.
    """
    ingest_events(canal, parse_meta_webhook(_inbound(telefone="")))

    assert Contact.objects.get(clinic=clinic_a, user_id=BSUID).display_name == "Willian"


def test_dois_contatos_SEM_telefone_convivem(clinic_a, canal):
    """
    ⚠️ Sem a condição `~Q(wa_id="")` na unicidade, o segundo contato sem número
    colidiria com o primeiro no vazio, e a mensagem dele seria recusada pelo
    banco.
    """
    ingest_events(canal, parse_meta_webhook(_inbound(telefone="")))
    ingest_events(
        canal,
        parse_meta_webhook(
            _inbound(telefone="", user_id=OUTRO_BSUID, wamid="wamid.bsuid2")
        ),
    )

    assert Contact.objects.filter(clinic=clinic_a, wa_id="").count() == 2


def test_o_telefone_que_VOLTA_e_guardado(clinic_a, canal):
    """
    A pessoa entrou só pelo identificador e depois o telefone reaparece (ela
    voltou a falar com a clínica). Guardá-lo é o que permite achar a ficha do
    paciente dela.
    """
    ingest_events(canal, parse_meta_webhook(_inbound(telefone="")))
    ingest_events(canal, parse_meta_webhook(_inbound(wamid="wamid.bsuid2")))

    contato = Contact.objects.get(clinic=clinic_a, user_id=BSUID)
    assert contato.wa_id == TELEFONE
    assert Contact.objects.filter(clinic=clinic_a).count() == 2, (
        "o do fixture e este: o telefone não criou um contato paralelo"
    )


def test_identificador_REGENERADO_e_atualizado(clinic_a, canal):
    """
    ⚠️ O identificador muda quando a pessoa troca de telefone, e a Meta manda o
    novo no webhook seguinte. Guardar o mais recente é o que impede de
    responder para um identificador morto (RF-CON-6.5).
    """
    ingest_events(canal, parse_meta_webhook(_inbound()))
    ingest_events(
        canal, parse_meta_webhook(_inbound(user_id=OUTRO_BSUID, wamid="wamid.bsuid2"))
    )

    contato = Contact.objects.get(clinic=clinic_a, wa_id=TELEFONE)
    assert contato.user_id == OUTRO_BSUID
    assert Contact.objects.filter(clinic=clinic_a).count() == 2


# --------------------------------------------------------------------- #
# Por onde se responde
# --------------------------------------------------------------------- #


def test_com_telefone_a_resposta_vai_pelo_TELEFONE(clinic_a):
    """
    ⚠️ Decisão registrada no RF-CON-6.3: o identificador é REGENERADO quando a
    pessoa troca de número, então um guardado aqui pode estar velho; e a
    própria Meta dá precedência ao telefone quando recebe os dois.
    """
    contato = Contact.objects.create(clinic=clinic_a, wa_id=TELEFONE, user_id=BSUID)

    assert contato.destino == TELEFONE


def test_sem_telefone_a_resposta_vai_pelo_IDENTIFICADOR(clinic_a):
    contato = Contact.objects.create(clinic=clinic_a, wa_id="", user_id=BSUID)

    assert contato.destino == BSUID


def test_o_envio_usa_o_destino_do_contato(clinic_a, canal, monkeypatch):
    """
    A prova de que o caminho de envio passa pelo ponto único, e não pelo
    `wa_id` cru: contato sem telefone não pode virar envio para string vazia.
    """
    from apps.inbox.choices import MessageKind
    from apps.inbox.models import Conversation
    from apps.inbox.services import send_message

    contato = Contact.objects.create(clinic=clinic_a, wa_id="", user_id=BSUID)
    conversa = Conversation.objects.create(
        clinic=clinic_a, channel=canal, contact=contato
    )
    mensagem = Message.objects.create(
        clinic=clinic_a,
        conversation=conversa,
        sender_kind=SenderKind.AGENT,
        kind=MessageKind.TEXT,
        body="oi",
        wa_timestamp=timezone.now(),
    )

    destinos = []

    class ProviderFalso:
        def send_text(self, to, body, reply_to=None):
            destinos.append(to)
            from apps.integrations.whatsapp.base import SendResult

            return SendResult(provider_message_id="wamid.enviada")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda canal: ProviderFalso(),
    )
    send_message(mensagem)

    assert destinos == [BSUID]
