"""
Os webhooks que a coexistência traz (§4.3.3, F2.7, fatia B).

Três eventos, três formatos DIFERENTES, e é aí que mora o risco:

  `smb_message_echoes` traz as mensagens em `message_echoes` (o nome do evento
  não é o nome do campo);
  `smb_app_state_sync` não tem lista nenhuma, os dados vêm soltos no `value`;
  `account_update` não tem número, só a conta no `entry.id`.

Os payloads abaixo seguem o formato documentado pela Meta. Inventar um formato
"parecido" no dublê é o erro que já custou envio quebrado ao vivo aqui.
"""

import pytest

from apps.inbox.choices import (
    ConversationStatus,
    MessageDirection,
    MessageKind,
    SenderKind,
)
from apps.inbox.models import Channel, Message
from apps.inbox.services import ingest_events
from apps.inbox.tasks import _aplicar_mudancas_de_conta
from apps.inbox.tests.conftest import make_message
from apps.integrations.whatsapp.base import WhatsAppEventKind
from apps.integrations.whatsapp.events import parse_meta_webhook
from apps.patients.models import Contact

PHONE_ID = "109876543210987"
WABA = "102938475601122"
PACIENTE = "5589981191501"


def _eco(wamid="wamid.eco1", texto="Consegui sim, quinta às 14h", para=PACIENTE):
    """Mensagem que a clínica mandou pelo app do celular."""
    return {
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "5589959011077",
                                "phone_number_id": PHONE_ID,
                            },
                            "message_echoes": [
                                {
                                    "from": "5589959011077",
                                    "to": para,
                                    "id": wamid,
                                    "timestamp": "1755600000",
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }


def _agenda(acao="add", telefone=PACIENTE, nome="Willian Souza"):
    """Contato mexido na agenda do celular da clínica."""
    return {
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "smb_app_state_sync",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "5589959011077",
                                "phone_number_id": PHONE_ID,
                            },
                            "action": acao,
                            "contact_phone_number": telefone,
                            "contact_name": nome,
                            "contact_first_name": nome.split()[0],
                        },
                    }
                ],
            }
        ]
    }


def _conta(evento="PARTNER_REMOVED"):
    """⚠️ Sem `metadata`: este evento é da CONTA, e só traz o WABA no entry."""
    return {
        "entry": [
            {
                "id": WABA,
                "time": 1755600000,
                "changes": [
                    {
                        "field": "account_update",
                        "value": {
                            "phone_number": "5589959011077",
                            "event": evento,
                            "disconnection_info": {
                                "disconnect_reason": "partner_removed",
                                "initiator": "business",
                            },
                        },
                    }
                ],
            }
        ]
    }


@pytest.fixture
def canal(inbox_a):
    """O canal da clínica de teste, conectado por coexistência."""
    channel = inbox_a["channel"]
    channel.phone_number_id = PHONE_ID
    channel.waba_id = WABA
    channel.is_coexistence = True
    channel.save()
    return channel


@pytest.fixture
def conversa_esperando(inbox_a, canal):
    """
    Conversa AGUARDANDO com uma não lida: o paciente escreveu e ninguém
    respondeu ainda. É o estado que o eco muda.
    """
    conversa = inbox_a["conversation"]
    conversa.contact.wa_id = PACIENTE
    conversa.contact.save()
    make_message(conversa, sender_kind=SenderKind.CONTACT, mid="wamid.paciente")
    conversa.refresh_from_db()
    conversa.status = ConversationStatus.WAITING
    conversa.unread_count = 1
    conversa.save()
    return conversa


# --------------------------------------------------------------------- #
# Eco: a clínica respondeu pelo celular
# --------------------------------------------------------------------- #


def test_eco_do_celular_vira_mensagem_de_saida(clinic_a, canal):
    stats = ingest_events(canal, parse_meta_webhook(_eco()))

    assert stats["echo"] == 1
    mensagem = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.eco1")
    assert mensagem.direction == MessageDirection.OUT
    assert mensagem.sender_kind == SenderKind.AGENT
    assert mensagem.kind == MessageKind.TEXT
    assert mensagem.body == "Consegui sim, quinta às 14h"


def test_o_eco_vem_MARCADO_como_do_celular(clinic_a, canal):
    """
    RF-CON-5.1: o balão diz de onde a resposta saiu. Campo próprio, e não
    "é de agente e não tem autor": envio por comando de manutenção também não
    tem autor, e passaria a se dizer do celular.
    """
    ingest_events(canal, parse_meta_webhook(_eco()))

    assert Message.objects.get(provider_message_id="wamid.eco1").from_phone


def test_mensagem_enviada_daqui_NAO_e_do_celular(conversa_esperando):
    """A guarda do outro lado: sem ela o campo poderia nascer sempre ligado."""
    daqui = make_message(conversa_esperando, sender_kind=SenderKind.AGENT, mid="wamid.daqui")

    assert not daqui.from_phone


def test_o_eco_resolve_a_conversa_pelo_TO_e_nao_pelo_FROM(clinic_a, canal):
    """
    ⚠️ No eco quem manda é a CLÍNICA: o `from` é o número dela. Resolver por ele
    criaria uma conversa da clínica com ela mesma, e a resposta sumiria da
    conversa do paciente.
    """
    ingest_events(canal, parse_meta_webhook(_eco()))

    mensagem = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.eco1")
    assert mensagem.conversation.contact.wa_id == PACIENTE


def test_eco_TIRA_a_conversa_da_fila_de_espera(conversa_esperando, canal):
    """RF-CON-5.2: quem respondeu pelo aparelho já leu, e a recepção não deve
    responder de novo."""
    ingest_events(canal, parse_meta_webhook(_eco()))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.status == ConversationStatus.OPEN
    assert conversa_esperando.unread_count == 0
    assert conversa_esperando.waiting_since is None


def test_eco_NAO_toma_a_caneta_de_ninguem(conversa_esperando, canal):
    """
    O eco não diz QUEM respondeu (a Meta manda o número da clínica, não a
    pessoa). Inventar um dono travaria a conversa para o resto da equipe.
    """
    from apps.inbox.choices import AttendedBy

    ingest_events(canal, parse_meta_webhook(_eco()))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.attended_by == AttendedBy.NONE
    assert conversa_esperando.assigned_to_id is None


def test_eco_em_conversa_ENCERRADA_nao_ressuscita(conversa_esperando, canal):
    """
    A clínica pode mandar um último aviso pelo celular depois de encerrar.
    Reabrir por causa disso encheria a fila de assunto já resolvido: quem
    reabre continua sendo o paciente (RF-ATD-2).
    """
    conversa_esperando.status = ConversationStatus.RESOLVED
    conversa_esperando.save()

    ingest_events(canal, parse_meta_webhook(_eco()))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.status == ConversationStatus.RESOLVED
    assert Message.objects.filter(provider_message_id="wamid.eco1").exists(), (
        "a mensagem entra na conversa mesmo assim: o que não muda é o estado"
    )


def test_eco_repetido_nao_duplica(clinic_a, canal):
    """A Meta reentrega o mesmo eco; a idempotência é por wamid."""
    ingest_events(canal, parse_meta_webhook(_eco()))
    stats = ingest_events(canal, parse_meta_webhook(_eco()))

    assert stats["echo"] == 0
    assert Message.objects.filter(clinic=clinic_a, provider_message_id="wamid.eco1").count() == 1


def test_eco_NAO_dispara_o_motor_de_fluxos(conversa_esperando, canal):
    """
    ⚠️ O sinal de inbound é do que o PACIENTE manda. Disparar fluxo com a fala
    da própria clínica faria o robô responder a si mesmo, que é o laço que as
    travas do RF-FLW-23 existem para impedir.
    """
    recebidos = []
    from apps.inbox.dispatch import inbound_ingested

    def espiao(sender, **kwargs):
        recebidos.append(kwargs)

    inbound_ingested.connect(espiao)
    try:
        ingest_events(canal, parse_meta_webhook(_eco()))
    finally:
        inbound_ingested.disconnect(espiao)

    assert recebidos == []


# --------------------------------------------------------------------- #
# Agenda do celular
# --------------------------------------------------------------------- #


def test_contato_da_agenda_entra_com_o_nome(clinic_a, canal):
    stats = ingest_events(canal, parse_meta_webhook(_agenda()))

    assert stats["contact_sync"] == 1
    contato = Contact.objects.get(clinic=clinic_a, wa_id=PACIENTE)
    assert contato.display_name == "Willian Souza"


def test_contato_da_agenda_NAO_abre_conversa(clinic_a, canal):
    """Salvar um número na agenda não é falar com ele: conversa sem mensagem
    encheria a fila de gente que nunca escreveu."""
    from apps.inbox.models import Conversation

    antes = Conversation.objects.filter(clinic=clinic_a).count()
    ingest_events(canal, parse_meta_webhook(_agenda()))

    assert Conversation.objects.filter(clinic=clinic_a).count() == antes


def test_a_agenda_NAO_sobrescreve_um_nome_que_ja_existe(clinic_a, canal):
    """
    O nome do WhatsApp (escolhido pelo próprio paciente) e o da recepção valem
    mais que o apelido na agenda do dono da clínica, onde a mesma pessoa pode
    estar salva como "Maria Recepção".
    """
    Contact.objects.create(clinic=clinic_a, wa_id=PACIENTE, display_name="Willian S. Neto")

    ingest_events(canal, parse_meta_webhook(_agenda(nome="Zap Willian Novo")))

    assert Contact.objects.get(clinic=clinic_a, wa_id=PACIENTE).display_name == "Willian S. Neto"


def test_REMOVER_da_agenda_e_ignorado(clinic_a, canal):
    """
    ⚠️ Desvio consciente do whatomate, que apaga o contato. Aqui o contato
    carrega vínculo com paciente, conversas e histórico: tirar um número da
    agenda do celular não é ordem de apagar prontuário.
    """
    Contact.objects.create(clinic=clinic_a, wa_id=PACIENTE, display_name="Willian")

    stats = ingest_events(canal, parse_meta_webhook(_agenda(acao="remove")))

    assert stats["contact_sync"] == 0
    assert Contact.objects.filter(clinic=clinic_a, wa_id=PACIENTE).exists()


def test_contato_da_agenda_usa_a_autocura_do_nono_digito(clinic_a, canal):
    """Número salvo sem o 9 no celular não pode virar um contato paralelo."""
    Contact.objects.create(clinic=clinic_a, wa_id=PACIENTE, display_name="Willian")

    ingest_events(canal, parse_meta_webhook(_agenda(telefone="558981191501")))

    assert Contact.objects.filter(clinic=clinic_a).count() == 2, (
        "o contato do fixture da conversa mais este; o do PACIENTE não duplicou"
    )


# --------------------------------------------------------------------- #
# A clínica removeu a integração pelo celular
# --------------------------------------------------------------------- #


def test_PARTNER_REMOVED_derruba_o_canal_com_o_motivo(canal):
    """RF-CON-5.4: sem isto o sistema seguiria tentando enviar e culpando a
    credencial, que é o diagnóstico errado."""
    feitos = _aplicar_mudancas_de_conta(canal, _conta())

    assert feitos == ["partner_removed"]
    canal.refresh_from_db()
    assert canal.disconnected
    assert "removida pelo aplicativo do celular" in canal.disconnect_reason


def test_outro_aviso_de_conta_NAO_derruba_nada(canal):
    """Agir por adivinhação derrubaria o canal por evento informativo."""
    feitos = _aplicar_mudancas_de_conta(canal, _conta(evento="VERIFIED_ACCOUNT"))

    assert feitos == []
    canal.refresh_from_db()
    assert not canal.disconnected


def test_o_canal_de_um_evento_SEM_numero_sai_do_waba(client, canal, settings):
    """
    ⚠️ `account_update` não tem `metadata.phone_number_id`. Antes disto, ele
    caía no ramo "assinado pela Meta, mas sem número" e ninguém o lia.
    """
    from apps.inbox.webhooks import _canais_do_payload

    achados = _canais_do_payload(_conta())

    assert [c.pk for c in achados] == [canal.pk]


def test_evento_de_conta_de_WABA_desconhecido_nao_acha_canal(canal):
    from apps.inbox.webhooks import _canais_do_payload

    payload = _conta()
    payload["entry"][0]["id"] = "999999999"

    assert _canais_do_payload(payload) == []


# --------------------------------------------------------------------- #
# O parser
# --------------------------------------------------------------------- #


def test_o_parser_le_o_campo_certo_de_cada_evento(db):
    """
    ⚠️ O nome do EVENTO e o da CHAVE são diferentes no eco
    (`smb_message_echoes` × `message_echoes`), e o da agenda não tem chave
    nenhuma: os dados vêm soltos no `value`.
    """
    (eco,) = parse_meta_webhook(_eco())
    (agenda,) = parse_meta_webhook(_agenda())

    assert eco.kind == WhatsAppEventKind.ECHO
    assert eco.wa_id == PACIENTE
    assert agenda.kind == WhatsAppEventKind.CONTACT_SYNC
    assert agenda.sync_action == "add"
    assert agenda.contact_name == "Willian Souza"


def test_agenda_sem_telefone_nao_vira_evento(db):
    """Contato sem número não tem o que sincronizar, e viraria contato vazio."""
    payload = _agenda()
    payload["entry"][0]["changes"][0]["value"]["contact_phone_number"] = ""

    assert parse_meta_webhook(payload) == []


def test_o_mais_da_frente_do_payload_nao_confunde_o_parser(db):
    """
    O `smb_app_state_sync` não pode ser lido como se tivesse mensagens, e o
    laço de mensagens não pode varrer o payload da agenda.
    """
    eventos = parse_meta_webhook(_agenda())

    assert len(eventos) == 1
    assert eventos[0].kind == WhatsAppEventKind.CONTACT_SYNC


def test_account_update_nao_vira_evento_de_mensagem(db):
    """Ele é tratado fora da ingestão, no caminho de conta."""
    assert parse_meta_webhook(_conta()) == []


# --------------------------------------------------------------------- #
# A volta para a fila (RF-ATD-1, corrigido em 21/08/2026)
# --------------------------------------------------------------------- #


def _inbound(wamid="wamid.dePaciente", texto="Bom dia!", quando="1755700000"):
    """Mensagem do PACIENTE, no formato que a Meta entrega."""
    return {
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_ID},
                            "contacts": [
                                {"wa_id": PACIENTE, "profile": {"name": "Tatiane"}}
                            ],
                            "messages": [
                                {
                                    "from": PACIENTE,
                                    "id": wamid,
                                    "timestamp": quando,
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }


def test_paciente_que_escreve_em_conversa_SEM_DONO_devolve_ela_a_fila(
    conversa_esperando, canal
):
    """
    ⚠️ O defeito que a clínica real mostrou em 21/08/2026.

    A clínica responde pelo celular, o eco põe a conversa em ABERTA SEM DONO,
    e nada a tirava mais desse estado: o `reopen` só age em conversa dormente.
    O paciente escrevia de novo, ninguém respondia, e a conversa não aparecia
    em recorte nenhum da fila - havia gente esperando e invisível.
    """
    from apps.inbox.choices import AttendedBy

    ingest_events(canal, parse_meta_webhook(_eco()))
    conversa_esperando.refresh_from_db()
    assert conversa_esperando.status == ConversationStatus.OPEN, "o eco abriu"

    ingest_events(canal, parse_meta_webhook(_inbound()))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.status == ConversationStatus.WAITING
    assert conversa_esperando.attended_by == AttendedBy.NONE
    assert conversa_esperando.waiting_since is not None


def test_o_relogio_da_fila_conta_desde_a_MENSAGEM_e_nao_desde_agora(
    conversa_esperando, canal
):
    """
    A Meta reentrega webhook com horas de atraso. Marcando `now()`, a conversa
    de quem espera desde ontem diria "aguardando há um minuto", e a fila
    perderia justamente a ordem que ela existe para dar.
    """
    ingest_events(canal, parse_meta_webhook(_eco()))
    ingest_events(canal, parse_meta_webhook(_inbound(quando="1755700000")))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.waiting_since == conversa_esperando.last_inbound_at


def test_conversa_COM_ATENDENTE_nao_e_arrancada_de_quem_atende(
    conversa_esperando, canal, clinic_a
):
    """
    PROVA NEGATIVA: devolver à fila uma conversa que alguém está atendendo
    tiraria a conversa das mãos de quem está digitando a resposta.
    """
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from apps.inbox.attendance import take_over
    from apps.inbox.choices import AttendedBy
    from conftest import make_user

    atendente = make_user("recepcao.fila@teste.dev")
    Membership.objects.create(
        user=atendente, clinic=clinic_a, role=MembershipRole.ATTENDANT
    )
    take_over(conversa_esperando, atendente)

    ingest_events(canal, parse_meta_webhook(_inbound()))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.status == ConversationStatus.OPEN
    assert conversa_esperando.attended_by == AttendedBy.AGENT
    assert conversa_esperando.assigned_to_id == atendente.pk


def test_conversa_COM_O_ROBO_nao_e_interrompida(conversa_esperando, canal):
    """
    PROVA NEGATIVA: cortar o robô no meio deixaria o paciente falando sozinho
    (RF-FLW-11). Responder é justamente o que o fluxo espera do paciente.
    """
    from apps.inbox.choices import AttendedBy

    conversa_esperando.status = ConversationStatus.OPEN
    conversa_esperando.attended_by = AttendedBy.BOT
    conversa_esperando.waiting_since = None
    conversa_esperando.save()

    ingest_events(canal, parse_meta_webhook(_inbound()))

    conversa_esperando.refresh_from_db()
    assert conversa_esperando.status == ConversationStatus.OPEN
    assert conversa_esperando.attended_by == AttendedBy.BOT


def test_os_quatro_recortes_SOMAM_a_fila(conversa_esperando, canal, clinic_a):
    """
    ⚠️ O contador que faltava. Na clínica real os três botões diziam
    1 + 2 + 0 numa fila de 10: sete conversas estavam abertas sem dono e não
    cabiam em recorte nenhum.
    """
    from django.test import override_settings
    from rest_framework.test import APIClient

    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    # Uma aberta pelo celular: é a que não era contada por ninguém.
    ingest_events(canal, parse_meta_webhook(_eco()))

    gestor = make_user("gestor.contadores@teste.dev")
    Membership.objects.create(user=gestor, clinic=clinic_a, role=MembershipRole.MANAGER)

    with override_settings(ALLOWED_HOSTS=["*"]):
        client = APIClient()
        client.force_authenticate(gestor)
        client.credentials(HTTP_X_CLINIC_ID=str(clinic_a.pk))
        c = client.get("/api/v1/conversations/counters/").data
        fila = client.get("/api/v1/conversations/?status=waiting,open").data["count"]

    assert c["unattended"] == 1
    assert c["attending"] + c["waiting"] + c["bot"] + c["unattended"] == fila


# --------------------------------------------------------------------- #
# Mensagem apagada pelo paciente (webhook `revoke`, RF-INB-6.6)
# --------------------------------------------------------------------- #


def _revoke(wamid_original, wamid="wamid.revoke1", quando="1787200000"):
    """
    O paciente apagou uma mensagem.

    ⚠️ Payload da DOCUMENTAÇÃO da Meta, não invenção: o tipo é `revoke` e o
    `original_message_id` diz QUAL mensagem sumiu. Este webhook só existe em
    coexistência, e foi por isso que ele passou meses despercebido aqui -
    caía no balde de "não suportado" e virava um balão vazio.
    """
    return {
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_ID},
                            "contacts": [
                                {"wa_id": PACIENTE, "profile": {"name": "Willian"}}
                            ],
                            "messages": [
                                {
                                    "from": PACIENTE,
                                    "id": wamid,
                                    "timestamp": quando,
                                    "type": "revoke",
                                    "revoke": {
                                        "original_message_id": wamid_original
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }


def _texto(wamid, corpo, quando="1787100000"):
    return {
        "entry": [
            {
                "id": WABA,
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": PHONE_ID},
                            "contacts": [
                                {"wa_id": PACIENTE, "profile": {"name": "Willian"}}
                            ],
                            "messages": [
                                {
                                    "from": PACIENTE,
                                    "id": wamid,
                                    "timestamp": quando,
                                    "type": "text",
                                    "text": {"body": corpo},
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }


def test_o_conteudo_da_mensagem_apagada_FICA(conversa_esperando, canal):
    """
    ⚠️ O registro do atendimento é da CLÍNICA. A recepção leu aquilo e
    respondeu com base naquilo; apagar junto reescreveria o que aconteceu.
    A tela esmaece e avisa - o texto continua.
    """
    ingest_events(canal, parse_meta_webhook(_texto("wamid.opa", "Opa opa")))
    ingest_events(canal, parse_meta_webhook(_revoke("wamid.opa")))

    apagada = Message.objects.get(provider_message_id="wamid.opa")
    assert apagada.body == "Opa opa"
    assert apagada.revoked_at is not None


def test_apagar_NAO_cria_balao_novo(conversa_esperando, canal):
    """
    ⚠️ O defeito que a clínica viu em 21/08/2026: o `revoke` não estava no
    mapa do parser, caía em "não suportado" e nascia uma mensagem VAZIA na
    conversa - o apagar aparecia como se fosse conteúdo novo.
    """
    ingest_events(canal, parse_meta_webhook(_texto("wamid.opa", "Opa opa")))
    antes = Message.objects.filter(conversation=conversa_esperando).count()

    ingest_events(canal, parse_meta_webhook(_revoke("wamid.opa")))

    assert Message.objects.filter(conversation=conversa_esperando).count() == antes
    assert not Message.objects.filter(provider_message_id="wamid.revoke1").exists()


def test_a_MENSAGEM_CERTA_e_marcada(conversa_esperando, canal):
    """
    A Meta diz qual foi, então não há adivinhação: apagar a primeira de três
    não pode carimbar a última.
    """
    ingest_events(canal, parse_meta_webhook(_texto("wamid.um", "primeira")))
    ingest_events(canal, parse_meta_webhook(_texto("wamid.dois", "segunda")))
    ingest_events(canal, parse_meta_webhook(_texto("wamid.tres", "terceira")))

    ingest_events(canal, parse_meta_webhook(_revoke("wamid.um")))

    marcadas = list(
        Message.objects.filter(revoked_at__isnull=False).values_list("body", flat=True)
    )
    assert marcadas == ["primeira"]


def test_revoke_de_mensagem_que_nao_temos_some_calado(conversa_esperando, canal):
    """
    PROVA NEGATIVA: o paciente pode apagar algo anterior à conexão do canal.
    Não há o que marcar, e um aviso sobre mensagem que a clínica nunca viu não
    ajudaria ninguém.
    """
    antes = Message.objects.filter(conversation=conversa_esperando).count()

    stats = ingest_events(canal, parse_meta_webhook(_revoke("wamid.NUNCA-EXISTIU")))

    assert stats["revoke"] == 0
    assert Message.objects.filter(conversation=conversa_esperando).count() == antes


def test_revoke_repetido_nao_reescreve_a_hora(conversa_esperando, canal):
    """Reentrega do webhook é comum; a hora de quando foi apagada é uma só."""
    ingest_events(canal, parse_meta_webhook(_texto("wamid.opa", "Opa opa")))
    ingest_events(canal, parse_meta_webhook(_revoke("wamid.opa")))
    primeira = Message.objects.get(provider_message_id="wamid.opa").revoked_at

    ingest_events(
        canal, parse_meta_webhook(_revoke("wamid.opa", wamid="wamid.revoke2"))
    )

    assert Message.objects.get(provider_message_id="wamid.opa").revoked_at == primeira
