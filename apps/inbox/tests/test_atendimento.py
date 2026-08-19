"""
Ciclo de vida e posse do atendimento (F2.5 Bloco A, §4.3.1).

Os testes que mais importam aqui não são os do caminho feliz: são os que
provam que **nota interna não vaza** e que **duas pessoas não escrevem na
mesma conversa** — os dois erros cujo custo aparece no celular do paciente.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.attendance import ConversationBusy, resolve, snooze, take_over, wake_snoozed
from apps.inbox.choices import (
    ActivityType,
    AttendedBy,
    ConversationStatus,
    MessageKind,
    SenderKind,
)
from apps.inbox.models import Conversation, Message
from apps.inbox.tests.conftest import make_message

MESSAGES_URL = "/api/v1/messages/"


def _url(conversation, acao):
    return f"/api/v1/conversations/{conversation.pk}/{acao}/"


@pytest.fixture
def conversation(inbox_a):
    return inbox_a["conversation"]


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


@pytest.fixture
def outro_atendente(db, clinic_a):
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    user = make_user("colega.atendimento@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.ATTENDANT)
    return user


# ─────────────────────────── ciclo de vida ───────────────────────────


def test_conversa_nasce_aguardando(conversation):
    assert conversation.status == ConversationStatus.WAITING
    assert conversation.attended_by == AttendedBy.NONE


def test_resolver_tira_da_fila_e_registra_evento(logado, conversation, manager_single_clinic):
    response = logado.post(_url(conversation, "resolve"), {}, format="json")

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.RESOLVED
    assert conversation.resolved_at is not None
    assert Message.objects.filter(
        conversation=conversation, activity_type=ActivityType.RESOLVED
    ).exists()


def test_nota_de_encerramento_e_opcional_e_vira_nota_interna(logado, conversation):
    logado.post(_url(conversation, "resolve"), {"note": "Remarcou para sexta"}, format="json")

    nota = Message.objects.get(conversation=conversation, is_internal=True)
    assert nota.body == "Remarcou para sexta"
    assert nota.provider_message_id == "", "nota nunca ganha wamid: não foi enviada"


def test_inbound_reabre_conversa_resolvida(logado, conversation, inbox_a):
    """RF-ATD-2: resolver não arquiva — a paciente volta e a conversa acorda."""
    resolve(conversation, None)

    make_message(conversation, sender_kind=SenderKind.CONTACT, body="oi de novo")
    from apps.inbox.services import apply_message_to_conversation

    apply_message_to_conversation(
        Message.objects.filter(conversation=conversation).order_by("-pk").first(), created=True
    )

    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.WAITING
    assert conversation.resolved_at is None
    assert Message.objects.filter(
        conversation=conversation, activity_type=ActivityType.REOPENED
    ).exists()


def test_reabertura_de_adiada_devolve_para_quem_adiou(conversation, manager_single_clinic):
    """
    Conversa ADIADA não perde o dono só porque ficou parada: quem adiou
    combinou de retomar, e o paciente que escreve antes da hora cai de volta
    com ele.
    """
    from apps.inbox.attendance import reopen

    take_over(conversation, manager_single_clinic)
    snooze(conversation, manager_single_clinic, until=timezone.now() + timedelta(days=2))

    reopen(conversation, by_contact=True)

    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.OPEN
    assert conversation.assigned_to_id == manager_single_clinic.pk
    assert conversation.attended_by == AttendedBy.AGENT


def test_encerrar_solta_a_conversa_e_ela_reabre_na_fila(conversation, manager_single_clinic):
    """
    Encerrar é "acabou": solta a caneta E o dono. Apontado ao vivo em
    31/07/2026, quando o atendente continuou responsável por conversas que
    tinha resolvido, e por isso nenhum fluxo automático voltava a entrar nelas.
    """
    from apps.inbox.attendance import reopen

    take_over(conversation, manager_single_clinic)
    resolve(conversation, manager_single_clinic)

    conversation.refresh_from_db()
    assert conversation.assigned_to_id is None, "encerrada não guarda responsável"
    assert conversation.attended_by == AttendedBy.NONE

    reopen(conversation, by_contact=True)

    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.WAITING, "assunto novo entra na fila"
    assert conversation.assigned_to_id is None
    assert conversation.attended_by == AttendedBy.NONE


def test_adiar_exige_futuro_e_guarda_a_hora(logado, conversation):
    passado = (timezone.now() - timedelta(hours=1)).isoformat()
    assert logado.post(_url(conversation, "snooze"), {"until": passado}, format="json").status_code == 400
    assert logado.post(_url(conversation, "snooze"), {}, format="json").status_code == 400

    futuro = timezone.now() + timedelta(days=2)
    response = logado.post(
        _url(conversation, "snooze"), {"until": futuro.isoformat()}, format="json"
    )

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.SNOOZED
    assert conversation.snoozed_until is not None


def test_contadores_sao_por_status(logado, conversation, inbox_a):
    resolve(conversation, None)

    response = logado.get("/api/v1/conversations/counters/")

    assert response.data["resolved"] == 1
    assert set(response.data) >= {"waiting", "open", "snoozed", "resolved"}


def test_contadores_separam_quem_atende_de_quem_a_ia_conduz(
    logado, conversation, clinic_a, manager_single_clinic, inbox_a
):
    """
    Cada botão da fila mostra o número DELE (29/07).

    A asserção que importa é a última: `open` sozinho serve para os dois botões
    e somaria coisas diferentes — o que a equipe atende com o que a IA conduz.
    """
    from apps.patients.models import Contact

    take_over(conversation, manager_single_clinic)

    da_ia = Conversation.objects.create(
        clinic=clinic_a,
        channel=inbox_a["channel"],
        contact=Contact.objects.create(
            clinic=clinic_a, wa_id="5585900000009", display_name="Rafael"
        ),
        status=ConversationStatus.OPEN,
        attended_by=AttendedBy.BOT,
    )

    dados = logado.get("/api/v1/conversations/counters/").data

    assert dados["attending"] == 1
    assert dados["bot"] == 1
    assert dados["waiting"] == 0
    # O motivo de os dois campos existirem: `open` sozinho é 2 e não diz de quem
    # é cada uma — os dois botões mostrariam o mesmo número.
    assert dados["open"] == 2
    # E o botão da IA conta a conversa CERTA, não só "alguma aberta".
    assert Conversation.objects.filter(
        pk=da_ia.pk, status=ConversationStatus.OPEN, attended_by=AttendedBy.BOT
    ).exists()


def test_filtro_de_status_aceita_lista(logado, conversation):
    """A fila padrão do front é "o que está vivo" — Resolvidas por filtro."""
    resolve(conversation, None)

    vivas = logado.get("/api/v1/conversations/", {"status": "waiting,open"})
    resolvidas = logado.get("/api/v1/conversations/", {"status": "resolved"})

    assert conversation.pk not in {c["id"] for c in vivas.data["results"]}
    assert conversation.pk in {c["id"] for c in resolvidas.data["results"]}


# ──────────────────────────── nota interna ────────────────────────────


def test_nota_interna_NAO_e_enviada_ao_paciente(logado, conversation):
    """
    O teste mais importante do bloco: o custo do erro é a paciente ler
    comentário da equipe sobre ela.
    """
    make_message(conversation, sender_kind=SenderKind.CONTACT)  # abre a janela

    response = logado.post(
        MESSAGES_URL,
        {"conversation": conversation.pk, "body": "conferir convênio antes", "is_internal": True},
        format="json",
    )

    assert response.status_code == 201
    nota = Message.objects.get(pk=response.data["id"])
    assert nota.is_internal is True
    # A task eager teria gravado wamid se tivesse enviado.
    assert nota.provider_message_id == ""
    assert nota.status == ""


def test_nota_interna_passa_com_a_janela_de_24h_FECHADA(logado, conversation):
    """Barrar a nota pela janela impediria registrar contexto justamente na
    conversa parada há dias — quando mais se precisa dele."""
    assert conversation.window_open is False

    response = logado.post(
        MESSAGES_URL,
        {"conversation": conversation.pk, "body": "paciente sumiu, tentar por telefone",
         "is_internal": True},
        format="json",
    )

    assert response.status_code == 201


def test_nota_interna_nao_assume_a_conversa(logado, conversation):
    """Anotar não é atender: a conversa continua na fila para quem for pegar."""
    logado.post(
        MESSAGES_URL,
        {"conversation": conversation.pk, "body": "nota", "is_internal": True},
        format="json",
    )

    conversation.refresh_from_db()
    assert conversation.attended_by == AttendedBy.NONE
    assert conversation.status == ConversationStatus.WAITING


def test_nota_interna_vazia_e_recusada(logado, conversation):
    response = logado.post(
        MESSAGES_URL,
        {"conversation": conversation.pk, "body": "   ", "is_internal": True},
        format="json",
    )
    assert response.status_code == 400


# ──────────────────────────── posse e trava ────────────────────────────


def test_escrever_em_conversa_livre_a_assume(logado, conversation, manager_single_clinic):
    """RF-ATD-14: senão a recepção daria dois cliques para responder a
    primeira mensagem do dia."""
    make_message(conversation, sender_kind=SenderKind.CONTACT)

    logado.post(MESSAGES_URL, {"conversation": conversation.pk, "body": "oi"}, format="json")

    conversation.refresh_from_db()
    assert conversation.attended_by == AttendedBy.AGENT
    assert conversation.assigned_to_id == manager_single_clinic.pk
    assert conversation.status == ConversationStatus.OPEN


def test_quem_nao_tem_a_caneta_e_RECUSADO_pela_API(
    api_client, conversation, manager_single_clinic, outro_atendente
):
    """A trava vive no servidor: front desabilitando campo não protege de
    duas abas, F5 no meio, nem da IA."""
    make_message(conversation, sender_kind=SenderKind.CONTACT)
    take_over(conversation, manager_single_clinic)

    api_client.force_authenticate(outro_atendente)
    response = api_client.post(
        MESSAGES_URL, {"conversation": conversation.pk, "body": "deixa que eu falo"}, format="json"
    )

    assert response.status_code == 403
    assert response.data["code"] == "conversation_busy"
    assert response.data["holder"], "a tela precisa do nome para explicar"
    assert not Message.objects.filter(conversation=conversation, body="deixa que eu falo").exists()


def test_conversa_com_a_IA_bloqueia_ate_o_humano_assumir(
    logado, conversation, manager_single_clinic
):
    Conversation.objects.filter(pk=conversation.pk).update(attended_by=AttendedBy.BOT)
    make_message(conversation, sender_kind=SenderKind.CONTACT)

    bloqueado = logado.post(
        MESSAGES_URL, {"conversation": conversation.pk, "body": "oi"}, format="json"
    )
    assert bloqueado.status_code == 403
    assert bloqueado.data["attended_by"] == AttendedBy.BOT

    logado.post(_url(conversation, "assign"), {"expected_attended_by": "bot"}, format="json")

    liberado = logado.post(
        MESSAGES_URL, {"conversation": conversation.pk, "body": "oi"}, format="json"
    )
    assert liberado.status_code == 201


def test_tomar_da_IA_registra_evento_de_tomada(conversation, manager_single_clinic):
    Conversation.objects.filter(pk=conversation.pk).update(attended_by=AttendedBy.BOT)
    conversation.refresh_from_db()

    take_over(conversation, manager_single_clinic, expected=AttendedBy.BOT)

    assert Message.objects.filter(
        conversation=conversation, activity_type=ActivityType.TAKEN_OVER
    ).exists()


def test_corrida_entre_dois_atendentes_so_um_vence(
    conversation, manager_single_clinic, outro_atendente
):
    """
    Os dois viram a conversa livre e clicam juntos. O segundo perde: a troca é
    condicionada ao responsável que ele VIU, não ao que existe agora.
    """
    visto_pelos_dois = conversation.attended_by

    take_over(conversation, manager_single_clinic, expected=visto_pelos_dois)

    with pytest.raises(ConversationBusy):
        take_over(conversation, outro_atendente, expected=visto_pelos_dois)

    conversation.refresh_from_db()
    assert conversation.assigned_to_id == manager_single_clinic.pk


def test_assumir_duas_vezes_e_idempotente(conversation, manager_single_clinic):
    take_over(conversation, manager_single_clinic)
    take_over(conversation, manager_single_clinic)  # não pode levantar

    conversation.refresh_from_db()
    assert conversation.assigned_to_id == manager_single_clinic.pk


def test_devolver_para_a_fila_perde_o_responsavel(logado, conversation, manager_single_clinic):
    take_over(conversation, manager_single_clinic)

    logado.post(_url(conversation, "mark-waiting"), {}, format="json")

    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.WAITING
    assert conversation.attended_by == AttendedBy.NONE
    assert conversation.assigned_to_id is None


# ──────────────────────────── métrica ────────────────────────────


def test_primeira_resposta_humana_e_carimbada(logado, conversation):
    make_message(conversation, sender_kind=SenderKind.CONTACT)
    conversation.refresh_from_db()
    assert conversation.first_response_at is None

    logado.post(MESSAGES_URL, {"conversation": conversation.pk, "body": "oi"}, format="json")

    conversation.refresh_from_db()
    assert conversation.first_response_at is not None


def test_nota_interna_nao_conta_como_primeira_resposta(logado, conversation):
    """A paciente não recebeu nada — não houve resposta."""
    make_message(conversation, sender_kind=SenderKind.CONTACT)

    logado.post(
        MESSAGES_URL,
        {"conversation": conversation.pk, "body": "nota", "is_internal": True},
        format="json",
    )

    conversation.refresh_from_db()
    assert conversation.first_response_at is None


def test_evento_de_atividade_nao_polui_a_thread_como_mensagem(conversation, manager_single_clinic):
    take_over(conversation, manager_single_clinic)

    evento = Message.objects.get(conversation=conversation, kind=MessageKind.ACTIVITY)
    assert evento.sender_kind == SenderKind.SYSTEM
    assert evento.body == ""
    assert evento.activity_data == {"from": AttendedBy.NONE}


def test_evento_nao_vira_previa_da_lista(conversation, manager_single_clinic):
    """
    Achado na calibração de 28/07: resolver trocava a última fala do paciente
    por "Evento" na listagem - apagando a informação que diz onde a conversa
    parou. Evento é METADADO da conversa, não conteúdo dela.
    """
    make_message(conversation, sender_kind=SenderKind.CONTACT, body="Quero remarcar quinta")
    conversation.refresh_from_db()
    antes = (conversation.last_message_preview, conversation.last_message_at)

    resolve(conversation, manager_single_clinic)

    conversation.refresh_from_db()
    assert conversation.last_message_preview == "Quero remarcar quinta"
    assert (conversation.last_message_preview, conversation.last_message_at) == antes


# ──────────────────── adiamento que VOLTA (RF-ATD-1.2) ────────────────────
#
# `snoozed_until` ficou uma sessão inteira sendo gravado sem ninguém ler — a
# tela prometia "volta sozinha" e nada voltava. Estes testes amarram a
# promessa ao código.


def _adiada_vencida(conversation, user):
    from datetime import timedelta

    from django.utils import timezone

    snooze(conversation, user, until=timezone.now() + timedelta(minutes=5))
    # Vence o prazo por baixo, como o tempo faria.
    Conversation.objects.filter(pk=conversation.pk).update(
        snoozed_until=timezone.now() - timedelta(minutes=1)
    )
    conversation.refresh_from_db()
    return conversation


def test_adiada_vencida_volta_para_a_fila_SEM_dono(conversation, manager_single_clinic):
    take_over(conversation, manager_single_clinic)
    _adiada_vencida(conversation, manager_single_clinic)

    assert wake_snoozed(conversation) is True

    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.WAITING
    assert conversation.attended_by == AttendedBy.NONE
    assert conversation.assigned_to_id is None
    assert conversation.snoozed_until is None
    assert conversation.waiting_since is not None

    evento = Message.objects.filter(
        conversation=conversation,
        kind=MessageKind.ACTIVITY,
        activity_type=ActivityType.REOPENED,
    ).latest("id")
    assert evento.activity_data["by"] == "snooze"
    # O dono anterior fica na história, não no registro vivo.
    assert evento.activity_data["was_with"]


def test_acordar_duas_vezes_gera_UM_evento(conversation, manager_single_clinic):
    _adiada_vencida(conversation, manager_single_clinic)

    assert wake_snoozed(conversation) is True
    assert wake_snoozed(conversation) is False

    eventos = Message.objects.filter(
        conversation=conversation,
        kind=MessageKind.ACTIVITY,
        activity_type=ActivityType.REOPENED,
    )
    assert eventos.count() == 1


def test_paciente_reabriu_antes_da_hora_e_a_varredura_nao_atropela(
    conversation, manager_single_clinic
):
    """A corrida real: inbound reabre às 8h59, a varredura roda às 9h00."""
    take_over(conversation, manager_single_clinic)  # adiada COM dono
    _adiada_vencida(conversation, manager_single_clinic)
    make_message(conversation, sender_kind=SenderKind.CONTACT, body="cheguei antes")
    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.OPEN  # reabriu com dono

    # A varredura pega um retrato velho (ainda SNOOZED na memória dela).
    retrato_velho = Conversation.objects.get(pk=conversation.pk)
    retrato_velho.status = ConversationStatus.SNOOZED

    assert wake_snoozed(retrato_velho) is False

    conversation.refresh_from_db()
    assert conversation.status == ConversationStatus.OPEN
    assert conversation.assigned_to_id == manager_single_clinic.pk


def test_varredura_acorda_so_as_vencidas(conversation, inbox_a, manager_single_clinic):
    from datetime import timedelta

    from django.utils import timezone

    from apps.inbox.tasks import wake_snoozed_conversations
    from apps.patients.models import Contact

    _adiada_vencida(conversation, manager_single_clinic)
    futura = Conversation.objects.create(
        clinic=conversation.clinic,
        channel=inbox_a["channel"],
        contact=Contact.objects.create(clinic=conversation.clinic, wa_id="5511988887777"),
        status=ConversationStatus.SNOOZED,
        snoozed_until=timezone.now() + timedelta(hours=2),
    )

    resultado = wake_snoozed_conversations()

    assert resultado == {"woken": 1}
    conversation.refresh_from_db()
    futura.refresh_from_db()
    assert conversation.status == ConversationStatus.WAITING
    assert futura.status == ConversationStatus.SNOOZED, "a que não venceu não acorda"


def test_conversa_nova_da_ingestao_nasce_com_o_relogio_da_fila(inbox_a):
    """RF-ATD-11: sem `waiting_since` na criação, o "aguardando há X" da
    situação mais comum - conversa nova - ficaria em branco."""
    from apps.inbox.services import _get_or_create_conversation

    class _Evento:
        wa_id = "5511977776666"
        contact_name = "Paciente Novo"

    # Devolve a tupla desde 18/08/2026: quem chama precisa saber se ela NASCEU,
    # para emitir `conversation:new` em vez de `conversation:updated` - sem
    # isso a conversa não aparecia no Inbox de ninguém sem recarregar a página.
    conversation, nasceu = _get_or_create_conversation(inbox_a["channel"], _Evento())

    assert nasceu is True
    assert conversation.status == ConversationStatus.WAITING
    assert conversation.waiting_since is not None


def test_listagem_expoe_waiting_since(logado, conversation):
    resposta = logado.get("/api/v1/conversations/")

    linha = next(c for c in resposta.data["results"] if c["id"] == conversation.pk)
    assert "waiting_since" in linha


def test_idioma_do_template_vem_do_sincronizado_nao_de_constante(conversation):
    """Achado ao vivo: hello_world só existe em en_US e o envio cravado em
    pt_BR morria com 132001 — um erro que nem menciona idioma."""
    from apps.inbox.models import WhatsAppTemplate
    from apps.inbox.services import _template_language

    class _Msg:
        clinic_id = conversation.clinic_id
        template_name = "hello_world"

    WhatsAppTemplate.objects.create(
        clinic=conversation.clinic, name="hello_world", language="en_US", status="APPROVED"
    )
    assert _template_language(_Msg()) == "en_US"

    # Existindo nos dois idiomas, o da clínica ganha.
    WhatsAppTemplate.objects.create(
        clinic=conversation.clinic, name="hello_world", language="pt_BR", status="APPROVED"
    )
    assert _template_language(_Msg()) == "pt_BR"

    _Msg.template_name = "inexistente"
    assert _template_language(_Msg()) == "pt_BR"


class TestResolverSoltaACaneta:
    """
    Encerrar solta a posse (corrigido 31/07/2026).

    Antes a conversa terminava "resolvida e sendo atendida" ao mesmo tempo, e
    reabria com dono - o que impedia qualquer fluxo automático de entrar nela
    depois. Dez das doze resolvidas do tenant real estavam nesse estado.
    """

    def test_resolver_deixa_a_conversa_sem_dono(self, conversation, attendant_a):
        from apps.inbox.attendance import resolve, take_over

        take_over(conversation, attendant_a)
        assert conversation.attended_by == AttendedBy.AGENT

        resolve(conversation, attendant_a)

        conversation.refresh_from_db()
        assert conversation.attended_by == AttendedBy.NONE
        assert conversation.attended_since is None

    def test_quem_cuidou_fica_na_linha_do_tempo_e_nao_no_dono(self, conversation, attendant_a):
        """
        Encerrar solta o dono TAMBÉM (segunda volta da correção, 31/07/2026).
        Guardar o `assigned_to` mantinha o atendente responsável por assunto
        encerrado, e `reopen` devolvia a caneta a ele: nada mudava na prática.

        A resposta para "quem cuidou disto" é o evento na thread, que continua
        nomeando a pessoa (RF-ATD-4).
        """
        from apps.inbox.attendance import resolve, take_over

        take_over(conversation, attendant_a)
        resolve(conversation, attendant_a)

        conversation.refresh_from_db()
        assert conversation.assigned_to is None
        evento = Message.objects.get(
            conversation=conversation, activity_type=ActivityType.RESOLVED
        )
        assert evento.sent_by == attendant_a

    def test_conversa_resolvida_aceita_o_robo_depois(self, conversation, attendant_a):
        """
        A consequência que motivou a correção: com a caneta presa, o motor de
        fluxos nunca mais entrava naquela conversa.
        """
        from apps.automation.engine import _claim_for_bot
        from apps.inbox.attendance import resolve, take_over

        take_over(conversation, attendant_a)
        resolve(conversation, attendant_a)
        conversation.refresh_from_db()

        assert _claim_for_bot(conversation) is True
