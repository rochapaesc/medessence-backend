"""
Devolver a conversa para a automação (RF-FLW-22).

O que estes testes protegem é o buraco que a F2.6 deixou aberta: depois que um
atendente assumia, não existia caminho nenhum de volta para o robô. A ação
escolhe um fluxo, que começa do INÍCIO, e a conversa deixa de ser de quem
mandou.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.automation.choices import FlowNodeType, FlowRunStatus, FlowStatus, FlowTrigger
from apps.automation.engine import on_inbound, pause_for_agent, start_run
from apps.automation.models import FlowRun
from apps.automation.tests.conftest import make_contact, make_conversation, make_flow
from apps.inbox.attendance import take_over
from apps.inbox.choices import ActivityType, AttendedBy, ConversationStatus, MessageKind, SenderKind
from apps.inbox.models import Message
from conftest import make_user

pytestmark = pytest.mark.django_db

FLOWS_URL = "/api/v1/flows/"


def node(node_id, tipo, **config):
    return {"id": node_id, "type": tipo, "label": node_id, "config": config}


def edge(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


def fluxo_que_fala(clinic, *, name="Recepção padrão", status=FlowStatus.ACTIVE, **extra):
    """
    Um fluxo que PERGUNTA e espera, que é o formato real: ele fala e a
    conversa fica com o robô esperando o paciente.

    ⚠️ Um fluxo que só manda uma mensagem e acaba não serve para exercitar
    isto: ele chega ao fim dentro da própria requisição e o encerramento
    devolve a conversa para a fila, então a posse do robô nunca aparece.
    Esse caminho tem teste próprio, o `test_fluxo_que_acaba_na_hora`.
    """
    return make_flow(
        clinic,
        name=name,
        status=status,
        graph={
            "entry_node": "n1",
            "nodes": [
                node("n1", FlowNodeType.START),
                node(
                    "pergunta",
                    FlowNodeType.COLLECT_INPUT,
                    prompt_text="Você já é paciente da clínica?",
                    var_key="ja_paciente",
                ),
                node("fim", FlowNodeType.END),
            ],
            "edges": [edge("n1", "pergunta"), edge("pergunta", "fim")],
        },
        **extra,
    )


def fluxo_relampago(clinic, *, name="Aviso"):
    """Fala uma vez e acaba, sem esperar resposta."""
    return make_flow(
        clinic,
        name=name,
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                node("n1", FlowNodeType.START),
                node("oi", FlowNodeType.SEND_MESSAGE, text="Estamos fechados hoje."),
                node("fim", FlowNodeType.END),
            ],
            "edges": [edge("n1", "oi"), edge("oi", "fim")],
        },
    )


def bot_messages(conversation):
    """
    As falas do robô que SAEM para o paciente.

    ⚠️ `is_internal=False` não é detalhe: desde o RF-FLW-22.8 a nota do handoff
    também é do robô, e sem este filtro ela entraria na lista das falas.
    """
    return list(
        Message.objects.filter(
            conversation=conversation, sender_kind=SenderKind.BOT, is_internal=False
        ).order_by("pk")
    )


def eventos(conversation, tipo):
    return list(
        Message.objects.filter(
            conversation=conversation, kind=MessageKind.ACTIVITY, activity_type=tipo
        ).order_by("pk")
    )


@pytest.fixture
def conversa(clinic_a):
    return make_conversation(clinic_a, make_contact(clinic_a))


@pytest.fixture
def colega(db, clinic_a):
    """Segundo atendente: a posse de um não é a do outro."""
    user = make_user("colega@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.ATTENDANT)
    return user


@pytest.fixture
def parceiro(db, clinic_a):
    user = make_user("parceiro@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.PARTNER)
    return user


@pytest.fixture
def atendente(api_client, attendant_a):
    api_client.force_authenticate(attendant_a)
    return api_client


def com_a_caneta(conversation, user):
    """A conversa na mão de alguém, que é a porta da ação (RF-FLW-22.2)."""
    take_over(conversation, user)
    conversation.refresh_from_db()
    return conversation


# --------------------------------------------------------------------- #
# A lista de fluxos disparáveis
# --------------------------------------------------------------------- #


class TestFluxosDisponiveis:
    def test_lista_os_ativos_publicados(self, atendente, clinic_a):
        fluxo_que_fala(clinic_a, name="Recepção padrão")
        fluxo_que_fala(clinic_a, name="Pesquisa", trigger=FlowTrigger.MANUAL)

        resposta = atendente.get(f"{FLOWS_URL}available/")

        assert resposta.status_code == 200
        nomes = [f["name"] for f in resposta.data["results"]]
        assert nomes == ["Pesquisa", "Recepção padrão"]
        assert "clinic_open" in resposta.data

    def test_o_gatilho_nao_filtra_nada(self, atendente, clinic_a):
        """
        RF-FLW-22.4: o gatilho diz quando o fluxo começa SOZINHO. À mão é
        outra porta, e um fluxo de palavra-chave é útil de disparar.
        """
        fluxo_que_fala(clinic_a, name="Orçamento", trigger=FlowTrigger.KEYWORD)
        fluxo_que_fala(clinic_a, name="Boas-vindas", trigger=FlowTrigger.FIRST_INBOUND)

        resposta = atendente.get(f"{FLOWS_URL}available/")

        assert {f["name"] for f in resposta.data["results"]} == {"Orçamento", "Boas-vindas"}

    def test_rascunho_e_arquivado_ficam_de_fora(self, atendente, clinic_a):
        """RF-FLW-22.6: disparar um fluxo pela metade é pior do que nenhum."""
        fluxo_que_fala(clinic_a, name="No ar")
        fluxo_que_fala(clinic_a, name="Meio pronto", status=FlowStatus.DRAFT)
        fluxo_que_fala(clinic_a, name="Aposentado", status=FlowStatus.ARCHIVED)

        resposta = atendente.get(f"{FLOWS_URL}available/")

        assert [f["name"] for f in resposta.data["results"]] == ["No ar"]

    def test_fluxo_sem_versao_nao_entra(self, atendente, clinic_a):
        flow = fluxo_que_fala(clinic_a, name="Sem desenho")
        flow.current_version = None
        flow.save(update_fields=["current_version"])

        resposta = atendente.get(f"{FLOWS_URL}available/")

        assert resposta.data["results"] == []

    def test_nao_vaza_fluxo_de_outra_clinica(self, atendente, clinic_a, clinic_b):
        fluxo_que_fala(clinic_a, name="Daqui")
        fluxo_que_fala(clinic_b, name="De lá")

        resposta = atendente.get(f"{FLOWS_URL}available/")

        assert [f["name"] for f in resposta.data["results"]] == ["Daqui"]

    def test_o_parceiro_nao_enxerga_fluxo(self, api_client, parceiro, clinic_a):
        """A cerca do RF-PAR-6 vale para toda view nova, e esta é uma."""
        fluxo_que_fala(clinic_a)
        api_client.force_authenticate(parceiro)

        assert api_client.get(f"{FLOWS_URL}available/").status_code == 403


# --------------------------------------------------------------------- #
# O disparo
# --------------------------------------------------------------------- #


class TestPassarParaOFluxo:
    def test_a_conversa_passa_para_o_robo_e_fica_sem_dono(
        self, atendente, attendant_a, clinic_a, conversa
    ):
        """RF-FLW-22.3: devolver é dizer que não é mais comigo."""
        flow = fluxo_que_fala(clinic_a)
        com_a_caneta(conversa, attendant_a)

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 200
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.BOT
        assert conversa.assigned_to_id is None
        assert conversa.status == ConversationStatus.OPEN

    def test_o_fluxo_comeca_a_falar(self, atendente, attendant_a, clinic_a, conversa):
        flow = fluxo_que_fala(clinic_a)
        com_a_caneta(conversa, attendant_a)

        atendente.post(f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json")

        assert [m.body for m in bot_messages(conversa)] == ["Você já é paciente da clínica?"]
        run = FlowRun.objects.get(conversation=conversa)
        assert run.flow_id == flow.pk

    def test_o_evento_diz_quem_passou_e_para_qual_fluxo(
        self, atendente, attendant_a, clinic_a, conversa
    ):
        """
        Sem o nome do fluxo, quem lê a conversa depois não sabe o que o robô
        foi fazer ali. E sem o `manual`, o front não tem como distinguir do
        gatilho automático, que é a mesma linha do tempo.
        """
        flow = fluxo_que_fala(clinic_a)
        com_a_caneta(conversa, attendant_a)

        atendente.post(f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json")

        evento = eventos(conversa, ActivityType.BOT_STARTED)[-1]
        assert evento.activity_data["flow"] == "Recepção padrão"
        assert evento.activity_data["manual"] is True
        assert evento.sent_by_id == attendant_a.pk

    def test_o_disparo_automatico_nao_vira_manual(self, clinic_a, conversa):
        """O mesmo evento serve aos dois caminhos, e `manual` os separa."""
        flow = fluxo_que_fala(clinic_a)

        start_run(flow, conversa)

        evento = eventos(conversa, ActivityType.BOT_STARTED)[-1]
        assert evento.activity_data["manual"] is False
        assert evento.sent_by_id is None

    def test_fluxo_que_acaba_na_hora_devolve_para_a_fila(
        self, atendente, attendant_a, clinic_a, conversa
    ):
        """
        Fluxo que fala uma vez e encerra chega ao fim dentro da própria
        requisição, e o encerramento devolve a conversa para a FILA. Ela não
        volta para quem passou: devolver é sair de cena (RF-FLW-22.3).
        """
        flow = fluxo_relampago(clinic_a)
        com_a_caneta(conversa, attendant_a)

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 200
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.NONE
        assert conversa.assigned_to_id is None
        assert conversa.status == ConversationStatus.WAITING
        assert [m.body for m in bot_messages(conversa)] == ["Estamos fechados hoje."]

    def test_fluxo_de_fora_do_horario_dispara_igual(
        self, atendente, attendant_a, clinic_a, conversa
    ):
        """
        RF-FLW-22.5: a restrição de horário é do disparo automático. Quem
        manda à mão está olhando a conversa.
        """
        flow = fluxo_que_fala(clinic_a, only_outside_hours=True)
        com_a_caneta(conversa, attendant_a)

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 200
        assert bot_messages(conversa)

    def test_a_execucao_pausada_nao_e_retomada(self, atendente, attendant_a, clinic_a, conversa):
        """
        RF-FLW-22.1: começa do INÍCIO. A execução que morreu quando o
        atendente assumiu fica no histórico, e nasce uma nova.
        """
        flow = fluxo_que_fala(clinic_a)
        antiga = start_run(flow, conversa)
        antiga.vars = {"nome": "Ana"}
        antiga.save(update_fields=["vars"])
        take_over(conversa, attendant_a)
        pause_for_agent(antiga)

        atendente.post(f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json")

        antiga.refresh_from_db()
        assert antiga.status == FlowRunStatus.PAUSED_BY_AGENT
        nova = FlowRun.objects.filter(conversation=conversa, status=FlowRunStatus.ACTIVE).get()
        assert nova.pk != antiga.pk
        assert nova.vars == {}, "variável da execução velha não atravessa para a nova"

    def test_o_gestor_tambem_devolve(self, api_client, manager_single_clinic, clinic_a, conversa):
        """Numa recepção de duas pessoas o gestor atende junto, e a ação é de
        quem está com a conversa, não de quem administra a clínica."""
        flow = fluxo_que_fala(clinic_a)
        com_a_caneta(conversa, manager_single_clinic)
        api_client.force_authenticate(manager_single_clinic)

        resposta = api_client.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 200
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.BOT


# --------------------------------------------------------------------- #
# A nota do handoff (RF-FLW-22.8)
# --------------------------------------------------------------------- #


class TestNotaDoHandoff:
    def fluxo_que_entrega(self, clinic, *, note="Pedido de agendamento.\nPaciente: Ana"):
        return make_flow(
            clinic,
            name="Entrega",
            status=FlowStatus.ACTIVE,
            graph={
                "entry_node": "n1",
                "nodes": [
                    node("n1", FlowNodeType.START),
                    node("passa", FlowNodeType.HANDOFF, note=note),
                ],
                "edges": [edge("n1", "passa")],
            },
        )

    def test_a_nota_do_no_vira_nota_interna(self, clinic_a, conversa):
        """
        RF-FLW-22.8. Antes ela só existia dentro do `activity_data` de um
        evento que a tela nem lia, então o que o fluxo apurou se perdia.
        """
        flow = self.fluxo_que_entrega(clinic_a)

        start_run(flow, conversa)

        nota = Message.objects.get(conversation=conversa, is_internal=True)
        assert nota.body == "Pedido de agendamento.\nPaciente: Ana"
        assert nota.sender_kind == SenderKind.BOT, "quem anotou foi o robô, não um atendente"
        assert nota.sent_by_id is None

    def test_a_nota_do_fluxo_nunca_sai_para_o_paciente(self, clinic_a, conversa):
        """
        ⚠️ A guarda do RF-ATD-3 vale igual aqui, e este é o teste que a afirma
        pelo caminho do fluxo. Nota interna que escapa para o WhatsApp é o pior
        defeito possível deste módulo: a equipe escreve sobre o paciente
        supondo que ele não lê.
        """
        flow = self.fluxo_que_entrega(clinic_a)

        start_run(flow, conversa)

        nota = Message.objects.get(conversation=conversa, is_internal=True)
        assert nota.provider_message_id == "", "sem wamid: não foi entregue a ninguém"
        assert not bot_messages(conversa), "não entrou na fila das falas que saem"

    def test_a_nota_interpola_o_que_o_fluxo_coletou(self, clinic_a, conversa):
        """Sem interpolação a recepção leria `{{nome}}` no lugar do paciente."""
        flow = make_flow(
            clinic_a,
            name="Coleta e entrega",
            status=FlowStatus.ACTIVE,
            graph={
                "entry_node": "n1",
                "nodes": [
                    node("n1", FlowNodeType.START),
                    node(
                        "pergunta",
                        FlowNodeType.COLLECT_INPUT,
                        prompt_text="Qual seu nome?",
                        var_key="nome",
                    ),
                    node("passa", FlowNodeType.HANDOFF, note="Paciente: {{nome}}"),
                ],
                "edges": [edge("n1", "pergunta"), edge("pergunta", "passa")],
            },
        )
        start_run(flow, conversa)

        resposta = Message.objects.create(
            clinic=conversa.clinic,
            conversation=conversa,
            sender_kind=SenderKind.CONTACT,
            kind=MessageKind.TEXT,
            body="Ana Paula",
            wa_timestamp=timezone.now(),
        )
        on_inbound(conversa, resposta)

        nota = Message.objects.get(conversation=conversa, is_internal=True)
        assert nota.body == "Paciente: Ana Paula"

    def test_handoff_sem_nota_nao_cria_nota_vazia(self, clinic_a, conversa):
        """O esgotamento do reprompt entrega sem nota, e nota em branco na
        thread é ruído que a recepção tem de ler para descobrir que não diz
        nada."""
        flow = self.fluxo_que_entrega(clinic_a, note="")

        start_run(flow, conversa)

        assert not Message.objects.filter(conversation=conversa, is_internal=True).exists()


# --------------------------------------------------------------------- #
# A despedida do handoff automático (RF-FLW-11.1)
# --------------------------------------------------------------------- #


class TestDespedida:
    def fluxo_que_pergunta(self, clinic, **fallback):
        flow = make_flow(
            clinic,
            name="Pergunta",
            status=FlowStatus.ACTIVE,
            graph={
                "entry_node": "n1",
                "nodes": [
                    node("n1", FlowNodeType.START),
                    node(
                        "escolha",
                        FlowNodeType.SEND_BUTTONS,
                        text="Qual opção?",
                        buttons=[{"id": "a", "title": "A"}],
                    ),
                    node("fim", FlowNodeType.END),
                ],
                "edges": [edge("n1", "escolha"), edge("escolha", "fim", "button:a")],
            },
        )
        if fallback:
            flow.fallback = {**flow.fallback, **fallback}
            flow.save(update_fields=["fallback"])
        return flow

    def responder(self, conversa, texto, *, horas_atras=0):
        """
        ⚠️ `wa_timestamp` é o que o paciente ESCREVEU, e é ele que manda na
        janela: o `post_save` de Message atualiza `last_inbound_at` a partir
        dele. Por isso "o paciente acabou de responder" e "a janela está
        fechada" não coexistem, e o caso de janela fechada com resposta
        chegando é a Meta REENTREGANDO um webhook antigo.
        """
        return Message.objects.create(
            clinic=conversa.clinic,
            conversation=conversa,
            sender_kind=SenderKind.CONTACT,
            kind=MessageKind.TEXT,
            body=texto,
            wa_timestamp=timezone.now() - timedelta(hours=horas_atras),
        )

    def test_o_robo_avisa_antes_de_entregar(self, clinic_a, conversa):
        """
        ⚠️ Achado ao vivo em 11/08/2026: o paciente errava a resposta três
        vezes e o robô SUMIA, sem dizer nada. O nó de transferir desenhado no
        fluxo nunca teve isso, porque quem monta põe uma fala antes dele; quem
        sofria era o caminho que o paciente não escolheu.
        """
        conversa.last_inbound_at = timezone.now()
        conversa.save(update_fields=["last_inbound_at"])
        flow = self.fluxo_que_pergunta(clinic_a, max_reprompts=0)
        start_run(flow, conversa)

        on_inbound(conversa, self.responder(conversa, "não é botão nenhum"))

        falas = [m.body for m in bot_messages(conversa)]
        assert any("recepção" in f for f in falas), f"o robô saiu calado: {falas}"
        assert falas[-1] == flow.fallback["goodbye_reprompt"]

    def test_a_despedida_nao_sai_com_a_janela_fechada(self, clinic_a, conversa):
        """
        RF-FLW-11.3.1. Acontece com webhook REENTREGUE pela Meta (a mensagem
        chega hoje mas foi escrita há dias) e com fluxo disparado à mão numa
        conversa velha. Sem texto livre possível, tentar enviar deixaria a
        mensagem `failed` na thread, que é pior do que o silêncio.
        """
        conversa.last_inbound_at = timezone.now() - timedelta(hours=30)
        conversa.save(update_fields=["last_inbound_at"])
        flow = self.fluxo_que_pergunta(clinic_a, max_reprompts=0)
        start_run(flow, conversa)

        on_inbound(conversa, self.responder(conversa, "não é botão", horas_atras=30))

        conversa.refresh_from_db()
        assert not conversa.window_open, "a montagem do teste precisa da janela fechada"
        falas = [m.body for m in bot_messages(conversa)]
        assert flow.fallback["goodbye_reprompt"] not in falas

    def test_sem_texto_configurado_nao_inventa_fala(self, clinic_a, conversa):
        """Quem apagou a fala na tela quis o silêncio."""
        conversa.last_inbound_at = timezone.now()
        conversa.save(update_fields=["last_inbound_at"])
        flow = self.fluxo_que_pergunta(clinic_a, max_reprompts=0, goodbye_reprompt="")
        start_run(flow, conversa)

        antes = len(bot_messages(conversa))
        on_inbound(conversa, self.responder(conversa, "qualquer coisa"))

        assert len(bot_messages(conversa)) == antes

    def test_o_timeout_de_fabrica_cabe_na_janela(self, clinic_a):
        """
        RF-FLW-11.3: 20h de timeout contra 24h de janela. Com os dois iguais a
        despedida caía na borda e só sairia como template pago.
        """
        from apps.inbox.models.conversation import WINDOW_HOURS

        flow = make_flow(clinic_a, name="Padrão")
        assert flow.fallback["on_timeout_hours"] < WINDOW_HOURS


# --------------------------------------------------------------------- #
# Travas de laço com outro robô (RF-FLW-23)
# --------------------------------------------------------------------- #


class TestTravasDeLaco:
    def fluxo_em_ciclo(self, clinic, **fallback):
        """
        Menu que volta para si mesmo: ciclo LEGÍTIMO, aprovado pelo validador
        porque passa por nó de espera. É exatamente o desenho que vira laço
        infinito quando o outro lado responde sempre algo válido.
        """
        flow = make_flow(
            clinic,
            name="Menu em ciclo",
            status=FlowStatus.ACTIVE,
            graph={
                "entry_node": "n1",
                "nodes": [
                    node("n1", FlowNodeType.START),
                    node(
                        "menu",
                        FlowNodeType.SEND_BUTTONS,
                        text="O que você quer?",
                        buttons=[{"id": "voltar", "title": "Voltar ao menu"}],
                    ),
                ],
                "edges": [edge("n1", "menu"), edge("menu", "menu", "button:voltar")],
            },
        )
        if fallback:
            flow.fallback = {**flow.fallback, **fallback}
            flow.save(update_fields=["fallback"])
        return flow

    def tocar_botao(self, conversa, botao="voltar"):
        return Message.objects.create(
            clinic=conversa.clinic,
            conversation=conversa,
            sender_kind=SenderKind.CONTACT,
            kind=MessageKind.INTERACTIVE,
            body="Voltar ao menu",
            content_data={"interactive_id": botao},
            wa_timestamp=timezone.now(),
        )

    def test_o_teto_de_falas_corta_o_pingue_pongue(self, clinic_a, conversa):
        """
        ⚠️ RF-FLW-23.1. O outro lado respondendo sempre algo VÁLIDO não
        esgota reprompt (a resposta casa), não estoura o teto de passos (cada
        volta é um avanço novo) e não cai na varredura (a execução avança).
        Sem esta trava o robô falava para sempre, e o preço é a Meta bloquear
        o número da clínica por spam.
        """
        flow = self.fluxo_em_ciclo(clinic_a, max_bot_messages=4)
        run = start_run(flow, conversa)

        for _ in range(10):
            on_inbound(conversa, self.tocar_botao(conversa))

        run.refresh_from_db()
        assert run.status == FlowRunStatus.HANDED_OFF
        assert run.end_reason == "teto_de_falas"
        assert len(bot_messages(conversa)) <= 5, "parou perto do teto, não falou 10 vezes"

    def test_sem_laco_o_teto_nao_atrapalha(self, clinic_a, conversa):
        """A trava não pode encurtar conversa legítima: o fluxo real gasta 8 a
        10 falas, e o teto de fábrica é 30."""
        flow = self.fluxo_em_ciclo(clinic_a)
        run = start_run(flow, conversa)

        for _ in range(3):
            on_inbound(conversa, self.tocar_botao(conversa))

        run.refresh_from_db()
        assert run.status == FlowRunStatus.ACTIVE

    def test_o_mesmo_contato_nao_redispara_sem_parar(self, clinic_a, conversa):
        """
        RF-FLW-23.2, o segundo caminho de laço: ao entregar, a conversa volta
        para a fila com posse `none`, e a palavra-chave dispara um fluxo NOVO.
        A trava do banco só impede duas execuções ATIVAS, não uma fila
        infinita em sequência.
        """
        from apps.automation.triggers import handle_inbound

        flow = fluxo_relampago(clinic_a)
        flow.trigger = FlowTrigger.KEYWORD
        flow.trigger_config = {"keywords": ["oi"], "match": "exact"}
        flow.save(update_fields=["trigger", "trigger_config"])

        for _ in range(6):
            msg = Message.objects.create(
                clinic=conversa.clinic,
                conversation=conversa,
                sender_kind=SenderKind.CONTACT,
                kind=MessageKind.TEXT,
                body="oi",
                wa_timestamp=timezone.now(),
            )
            conversa.refresh_from_db()
            handle_inbound(conversa, msg)

        assert FlowRun.objects.filter(conversation=conversa).count() == 3, (
            "o teto por hora é 3; sem ele seriam 6"
        )


# --------------------------------------------------------------------- #
# As recusas
# --------------------------------------------------------------------- #


class TestQuandoNaoDa:
    def test_sem_a_caneta_na_mao_nao_devolve(self, atendente, clinic_a, conversa):
        """RF-FLW-22.2: conversa na fila não é sua, então não há o que devolver."""
        flow = fluxo_que_fala(clinic_a)

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 403
        assert resposta.data["code"] == "conversation_busy"
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.NONE

    def test_a_conversa_do_colega_fica_com_ele(self, atendente, colega, clinic_a, conversa):
        flow = fluxo_que_fala(clinic_a)
        com_a_caneta(conversa, colega)

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 403
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.AGENT
        assert conversa.assigned_to_id == colega.pk
        assert not bot_messages(conversa), "o fluxo não pode ter falado na conversa de outro"

    def test_conversa_que_ja_esta_com_o_robo_nao_tem_o_que_devolver(
        self, atendente, clinic_a, conversa
    ):
        """
        RF-FLW-22.7. A trava do banco recusaria a segunda execução do mesmo
        contato (RF-FLW-6), mas quem barra ANTES é a posse: com o robô
        rodando, a caneta não está com o atendente.
        """
        flow = fluxo_que_fala(clinic_a)
        start_run(flow, conversa)
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.BOT

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 403
        assert FlowRun.objects.filter(conversation=conversa).count() == 1

    def test_fluxo_em_rascunho_e_recusado(self, atendente, attendant_a, clinic_a, conversa):
        flow = fluxo_que_fala(clinic_a, status=FlowStatus.DRAFT)
        com_a_caneta(conversa, attendant_a)

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 400
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.AGENT, "recusar não pode custar a posse"

    def test_conversa_de_outra_clinica_e_recusada(self, atendente, attendant_a, clinic_a, clinic_b):
        flow = fluxo_que_fala(clinic_a)
        de_la = make_conversation(clinic_b, make_contact(clinic_b, wa_id="5585900000099"))

        resposta = atendente.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": de_la.pk}, format="json"
        )

        assert resposta.status_code == 400
        assert not FlowRun.objects.filter(conversation=de_la).exists()

    def test_perder_a_corrida_do_comeco_nao_custa_a_conversa(
        self, atendente, attendant_a, clinic_a, conversa
    ):
        """
        Dois disparos ao mesmo tempo, ou o disparo junto com o webhook: a
        trava do banco (RF-FLW-6) recusa a segunda execução e o começo estoura
        DEPOIS de a caneta já ter saído da mão do atendente.

        ⚠️ Perder a corrida não pode custar a conversa. O caminho automático
        devolve para a fila, e aqui isso faria o atendente perder a conversa
        por um erro que ele não causou: ela volta para ELE.
        """
        flow = fluxo_que_fala(clinic_a)
        com_a_caneta(conversa, attendant_a)

        with patch.object(
            FlowRun.objects, "create", side_effect=IntegrityError("uniq_run_ativo_por_contato")
        ):
            resposta = atendente.post(
                f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
            )

        assert resposta.status_code == 403
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.AGENT
        assert conversa.assigned_to_id == attendant_a.pk
        assert conversa.status == ConversationStatus.OPEN

    def test_fluxo_de_outra_clinica_nao_existe_para_mim(
        self, atendente, attendant_a, clinic_a, clinic_b, conversa
    ):
        de_la = fluxo_que_fala(clinic_b, name="De lá")
        com_a_caneta(conversa, attendant_a)

        resposta = atendente.post(
            f"{FLOWS_URL}{de_la.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 404
        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.AGENT

    def test_sem_a_conversa_no_corpo_e_recusado(self, atendente, clinic_a):
        flow = fluxo_que_fala(clinic_a)

        assert atendente.post(f"{FLOWS_URL}{flow.pk}/start/", {}, format="json").status_code == 400

    def test_o_parceiro_nao_dispara(self, api_client, parceiro, clinic_a, conversa):
        flow = fluxo_que_fala(clinic_a)
        api_client.force_authenticate(parceiro)

        resposta = api_client.post(
            f"{FLOWS_URL}{flow.pk}/start/", {"conversation": conversa.pk}, format="json"
        )

        assert resposta.status_code == 403
        assert not FlowRun.objects.filter(conversation=conversa).exists()
