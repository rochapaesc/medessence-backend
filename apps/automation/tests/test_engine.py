import pytest
from django.utils import timezone

from apps.automation.choices import (
    ConditionOperator,
    ConditionSubject,
    FlowNodeType,
    FlowRunEventType,
    FlowRunStatus,
    FlowStatus,
)
from apps.automation.engine import interpolate, on_inbound, pause_for_agent, start_run
from apps.automation.models import FlowRun
from apps.automation.tests.conftest import make_contact, make_conversation, make_flow
from apps.inbox.choices import AttendedBy, ConversationStatus, MessageKind, SenderKind
from apps.inbox.models import ConversationLabel, Message
from apps.tenants.models import ClinicBusinessHours

pytestmark = pytest.mark.django_db


# ---- montagem ----


def node(node_id, tipo, **config):
    return {"id": node_id, "type": tipo, "label": node_id, "config": config}


def edge(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


def grafo(nodes, edges, entry="n1"):
    return {"entry_node": entry, "nodes": nodes, "edges": edges}


def fluxo_de_menu(clinic):
    """Início → botões (agendar/humano) → mensagem ou handoff."""
    return make_flow(
        clinic,
        status=FlowStatus.ACTIVE,
        graph=grafo(
            [
                node("n1", FlowNodeType.START),
                node(
                    "menu",
                    FlowNodeType.SEND_BUTTONS,
                    text="O que você deseja?",
                    buttons=[
                        {"id": "agendar", "title": "Marcar consulta"},
                        {"id": "humano", "title": "Falar com atendente"},
                    ],
                ),
                node("ok", FlowNodeType.SEND_MESSAGE, text="Vamos agendar!"),
                node("fim", FlowNodeType.END),
                node("humano", FlowNodeType.HANDOFF, note="Quer falar com gente"),
            ],
            [
                edge("n1", "menu"),
                edge("menu", "ok", "button:agendar"),
                edge("menu", "humano", "button:humano"),
                edge("ok", "fim"),
            ],
        ),
    )


def responder(conversation, *, texto="", interactive_id=""):
    """Simula a mensagem do paciente que o webhook já teria ingerido."""
    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        sender_kind=SenderKind.CONTACT,
        kind=MessageKind.INTERACTIVE if interactive_id else MessageKind.TEXT,
        body=texto,
        content_data={"interactive_id": interactive_id} if interactive_id else {},
        wa_timestamp=timezone.now(),
    )


def bot_messages(conversation):
    return list(
        Message.objects.filter(conversation=conversation, sender_kind=SenderKind.BOT).order_by("pk")
    )


@pytest.fixture
def conversa(clinic_a):
    return make_conversation(clinic_a, make_contact(clinic_a))


class TestInterpolacao:
    def test_troca_a_variavel(self):
        assert interpolate("Olá {{nome}}!", {"nome": "Ana"}) == "Olá Ana!"

    def test_variavel_sem_valor_some_em_vez_de_vazar(self):
        """
        O paciente não pode ler `{{nome}}` porque o gestor errou a chave: a
        mensagem fica estranha, mas não parece defeito de sistema.
        """
        assert interpolate("Olá {{nome}}!", {}) == "Olá !"

    def test_texto_sem_variavel_passa_intacto(self):
        assert interpolate("Bom dia", {"nome": "Ana"}) == "Bom dia"

    def test_texto_vazio_nao_estoura(self):
        assert interpolate("", {}) == ""


class TestInicio:
    def test_o_robo_toma_a_caneta_e_manda_a_primeira_mensagem(self, clinic_a, conversa):
        run = start_run(fluxo_de_menu(clinic_a), conversa)

        conversa.refresh_from_db()
        assert run.status == FlowRunStatus.ACTIVE
        assert conversa.attended_by == AttendedBy.BOT
        assert conversa.status == ConversationStatus.OPEN
        assert [m.body for m in bot_messages(conversa)] == ["O que você deseja?"]

    def test_para_no_no_que_espera_resposta(self, clinic_a, conversa):
        run = start_run(fluxo_de_menu(clinic_a), conversa)

        assert run.current_node == "menu"

    def test_os_botoes_vao_com_id_para_o_provedor(self, clinic_a, conversa):
        """
        É pelo id que o motor resolve a aresta quando o paciente toca - o
        título muda quando o gestor reescreve o botão.
        """
        start_run(fluxo_de_menu(clinic_a), conversa)

        enviada = bot_messages(conversa)[0]
        assert enviada.kind == MessageKind.INTERACTIVE
        assert [b["id"] for b in enviada.content_data["buttons"]] == ["agendar", "humano"]

    def test_nao_comeca_em_conversa_que_ja_tem_dono(self, clinic_a, conversa, attendant_a):
        """A recepção estava atendendo: o robô não atropela."""
        conversa.attended_by = AttendedBy.AGENT
        conversa.assigned_to = attendant_a
        conversa.save(update_fields=["attended_by", "assigned_to"])

        assert start_run(fluxo_de_menu(clinic_a), conversa) is None
        assert bot_messages(conversa) == []

    def test_segunda_partida_para_o_mesmo_contato_e_no_op(self, clinic_a, conversa):
        """
        RF-FLW-6: a segunda entrega do mesmo webhook não pode criar execução
        nova nem deixar a conversa presa ao robô.
        """
        fluxo = fluxo_de_menu(clinic_a)
        start_run(fluxo, conversa)
        conversa.refresh_from_db()

        assert start_run(fluxo, conversa) is None
        assert FlowRun.objects.filter(status=FlowRunStatus.ACTIVE).count() == 1

    def test_fluxo_sem_versao_publicada_nao_comeca(self, clinic_a, conversa):
        fluxo = fluxo_de_menu(clinic_a)
        fluxo.current_version = None
        fluxo.save(update_fields=["current_version"])

        assert start_run(fluxo, conversa) is None
        assert conversa.attended_by == AttendedBy.NONE


class TestRespostaDoPaciente:
    def test_o_botao_escolhe_o_caminho(self, clinic_a, conversa):
        start_run(fluxo_de_menu(clinic_a), conversa)

        assert on_inbound(conversa, responder(conversa, interactive_id="agendar")) is True

        assert [m.body for m in bot_messages(conversa)] == ["O que você deseja?", "Vamos agendar!"]

    def test_o_outro_botao_leva_ao_humano(self, clinic_a, conversa):
        run = start_run(fluxo_de_menu(clinic_a), conversa)

        on_inbound(conversa, responder(conversa, interactive_id="humano"))

        run.refresh_from_db()
        conversa.refresh_from_db()
        assert run.status == FlowRunStatus.HANDED_OFF
        assert conversa.attended_by == AttendedBy.NONE
        assert conversa.status == ConversationStatus.WAITING

    def test_chegar_ao_fim_devolve_a_conversa_para_a_fila(self, clinic_a, conversa):
        """
        Conversa presa ao robô fica invisível para a recepção - o pior fim
        possível.
        """
        run = start_run(fluxo_de_menu(clinic_a), conversa)

        on_inbound(conversa, responder(conversa, interactive_id="agendar"))

        run.refresh_from_db()
        conversa.refresh_from_db()
        assert run.status == FlowRunStatus.COMPLETED
        assert conversa.attended_by == AttendedBy.NONE
        assert conversa.status == ConversationStatus.WAITING

    def test_sem_execucao_ativa_a_mensagem_nao_e_consumida(self, conversa):
        assert on_inbound(conversa, responder(conversa, texto="oi")) is False

    def test_a_coleta_guarda_a_resposta_em_variavel(self, clinic_a, conversa):
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "pergunta",
                        FlowNodeType.COLLECT_INPUT,
                        prompt_text="Qual seu nome?",
                        var_key="nome",
                    ),
                    node("saudacao", FlowNodeType.SEND_MESSAGE, text="Prazer, {{nome}}!"),
                    node("fim", FlowNodeType.END),
                ],
                [edge("n1", "pergunta"), edge("pergunta", "saudacao"), edge("saudacao", "fim")],
            ),
        )
        run = start_run(fluxo, conversa)

        on_inbound(conversa, responder(conversa, texto="Ana"))

        run.refresh_from_db()
        assert run.vars == {"nome": "Ana"}
        assert bot_messages(conversa)[-1].body == "Prazer, Ana!"


class TestPosse:
    """
    RF-FLW-9 e RF-FLW-10: o robô perde a caneta e não a toma de volta.
    """

    def test_atendente_assumiu_no_meio_pausa_a_execucao(self, clinic_a, conversa, attendant_a):
        from apps.inbox.attendance import take_over

        run = start_run(fluxo_de_menu(clinic_a), conversa)
        take_over(conversa, attendant_a)

        on_inbound(conversa, responder(conversa, interactive_id="agendar"))

        run.refresh_from_db()
        assert run.status == FlowRunStatus.PAUSED_BY_AGENT

    def test_o_robo_nao_manda_mais_nada_depois_de_perder_a_caneta(
        self, clinic_a, conversa, attendant_a
    ):
        """
        O caso que este módulo existe para impedir: o paciente receber a fala
        do robô depois de a recepcionista já ter respondido.
        """
        from apps.inbox.attendance import take_over

        start_run(fluxo_de_menu(clinic_a), conversa)
        antes = len(bot_messages(conversa))
        take_over(conversa, attendant_a)

        on_inbound(conversa, responder(conversa, interactive_id="agendar"))

        assert len(bot_messages(conversa)) == antes

    def test_o_robo_nao_volta_sozinho_quando_o_paciente_responde_de_novo(
        self, clinic_a, conversa, attendant_a
    ):
        from apps.inbox.attendance import take_over

        run = start_run(fluxo_de_menu(clinic_a), conversa)
        take_over(conversa, attendant_a)
        on_inbound(conversa, responder(conversa, interactive_id="agendar"))

        on_inbound(conversa, responder(conversa, texto="e aí?"))

        run.refresh_from_db()
        conversa.refresh_from_db()
        assert run.status == FlowRunStatus.PAUSED_BY_AGENT
        assert conversa.attended_by == AttendedBy.AGENT

    def test_pausar_execucao_ja_encerrada_nao_faz_nada(self, clinic_a, conversa):
        run = start_run(fluxo_de_menu(clinic_a), conversa)
        on_inbound(conversa, responder(conversa, interactive_id="humano"))
        run.refresh_from_db()

        pause_for_agent(run)

        run.refresh_from_db()
        assert run.status == FlowRunStatus.HANDED_OFF


class TestReprompt:
    def fluxo_com_teto(self, clinic, teto):
        fluxo = fluxo_de_menu(clinic)
        fluxo.fallback = {"max_reprompts": teto, "on_timeout_hours": 24, "on_exhaust": "handoff"}
        fluxo.save(update_fields=["fallback"])
        return fluxo

    def test_resposta_que_nao_casa_repete_a_pergunta(self, clinic_a, conversa):
        run = start_run(self.fluxo_com_teto(clinic_a, 2), conversa)

        on_inbound(conversa, responder(conversa, texto="blablabla"))

        run.refresh_from_db()
        assert run.reprompt_count == 1
        assert [m.body for m in bot_messages(conversa)] == [
            "O que você deseja?",
            "O que você deseja?",
        ]

    def test_esgotado_o_teto_entrega_ao_humano(self, clinic_a, conversa):
        run = start_run(self.fluxo_com_teto(clinic_a, 1), conversa)

        on_inbound(conversa, responder(conversa, texto="nada a ver"))
        on_inbound(conversa, responder(conversa, texto="continua sem sentido"))

        run.refresh_from_db()
        conversa.refresh_from_db()
        assert run.status == FlowRunStatus.HANDED_OFF
        assert run.end_reason == "reprompt_esgotado"
        assert conversa.status == ConversationStatus.WAITING

    def test_acertar_depois_de_errar_zera_a_contagem(self, clinic_a, conversa):
        run = start_run(self.fluxo_com_teto(clinic_a, 2), conversa)
        on_inbound(conversa, responder(conversa, texto="errado"))

        on_inbound(conversa, responder(conversa, interactive_id="agendar"))

        run.refresh_from_db()
        assert run.reprompt_count == 0


class TestCondicao:
    def fluxo_com_condicao(self, clinic, **cfg):
        return make_flow(
            clinic,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("se", FlowNodeType.CONDITION, **cfg),
                    node("sim", FlowNodeType.SEND_MESSAGE, text="Caminho do sim"),
                    node("nao", FlowNodeType.SEND_MESSAGE, text="Caminho do não"),
                    node("fim", FlowNodeType.END),
                ],
                [
                    edge("n1", "se"),
                    edge("se", "sim", "true"),
                    edge("se", "nao", "false"),
                    edge("sim", "fim"),
                    edge("nao", "fim"),
                ],
            ),
        )

    def test_variavel_ausente_cai_no_falso(self, clinic_a, conversa):
        fluxo = self.fluxo_com_condicao(
            clinic_a,
            subject=ConditionSubject.VAR,
            subject_key="plano",
            operator=ConditionOperator.PRESENT,
        )

        start_run(fluxo, conversa)

        assert bot_messages(conversa)[0].body == "Caminho do não"

    def test_comparacao_ignora_maiuscula(self, clinic_a, conversa):
        """
        O paciente digita "SIM", "Sim" e "sim" - tratar como respostas
        diferentes é defeito garantido em produção.

        O cenário é o real: coleta a resposta e só então compara. Reaproveitar
        uma execução já encerrada não testaria nada, porque a conversa já
        teria voltado para a fila e o robô não escreveria mais.
        """
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "pergunta",
                        FlowNodeType.COLLECT_INPUT,
                        prompt_text="Confirma?",
                        var_key="resposta",
                    ),
                    node(
                        "se",
                        FlowNodeType.CONDITION,
                        subject=ConditionSubject.VAR,
                        subject_key="resposta",
                        operator=ConditionOperator.EQUALS,
                        value="sim",
                    ),
                    node("sim", FlowNodeType.SEND_MESSAGE, text="Caminho do sim"),
                    node("nao", FlowNodeType.SEND_MESSAGE, text="Caminho do não"),
                    node("fim", FlowNodeType.END),
                ],
                [
                    edge("n1", "pergunta"),
                    edge("pergunta", "se"),
                    edge("se", "sim", "true"),
                    edge("se", "nao", "false"),
                    edge("sim", "fim"),
                    edge("nao", "fim"),
                ],
            ),
        )
        start_run(fluxo, conversa)

        on_inbound(conversa, responder(conversa, texto="SIM"))

        assert bot_messages(conversa)[-1].body == "Caminho do sim"

    def test_clinica_fechada_cai_no_falso(self, clinic_a, conversa):
        """
        Sem horário cadastrado a clínica está sempre fechada, e é o default
        certo: clínica nova não atende de madrugada por omissão (RF-FLW-5.1.1).
        """
        fluxo = self.fluxo_com_condicao(
            clinic_a,
            subject=ConditionSubject.BUSINESS_HOURS,
            operator=ConditionOperator.PRESENT,
        )

        start_run(fluxo, conversa)

        assert bot_messages(conversa)[0].body == "Caminho do não"

    def test_clinica_aberta_agora_cai_no_verdadeiro(self, clinic_a, conversa):
        from zoneinfo import ZoneInfo

        agora = timezone.now().astimezone(ZoneInfo(clinic_a.timezone))
        ClinicBusinessHours.objects.create(
            clinic=clinic_a,
            weekday=agora.weekday(),
            opens_at=agora.time().replace(hour=0, minute=1),
            closes_at=agora.time().replace(hour=23, minute=59),
        )
        fluxo = self.fluxo_com_condicao(
            clinic_a,
            subject=ConditionSubject.BUSINESS_HOURS,
            operator=ConditionOperator.PRESENT,
        )

        start_run(fluxo, conversa)

        assert bot_messages(conversa)[0].body == "Caminho do sim"


class TestEtiqueta:
    def test_marca_etiqueta_de_conversa(self, clinic_a, conversa):
        etiqueta = ConversationLabel.objects.create(clinic=clinic_a, name="Agendamento")
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("marca", FlowNodeType.SET_LABEL, label_id=etiqueta.pk),
                    node("fim", FlowNodeType.END),
                ],
                [edge("n1", "marca"), edge("marca", "fim")],
            ),
        )

        start_run(fluxo, conversa)

        assert list(conversa.labels.all()) == [etiqueta]

    def test_etiqueta_de_outra_clinica_e_ignorada(self, clinic_a, clinic_b, conversa):
        """Escopo de tenant vale também para o que o fluxo escreve."""
        alheia = ConversationLabel.objects.create(clinic=clinic_b, name="Outra")
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("marca", FlowNodeType.SET_LABEL, label_id=alheia.pk),
                    node("fim", FlowNodeType.END),
                ],
                [edge("n1", "marca"), edge("marca", "fim")],
            ),
        )

        start_run(fluxo, conversa)

        assert conversa.labels.count() == 0


class TestTetoDePassos:
    def test_laco_que_o_validador_nao_pegou_nao_gira_para_sempre(self, clinic_a, conversa):
        """
        Fluxo semeado direto no banco não passou pelo validador. O teto é a
        última barreira antes de a Meta bloquear o número da clínica por spam.
        """
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("a", FlowNodeType.SEND_MESSAGE, text="oi"),
                ],
                [edge("n1", "a"), edge("a", "a")],
            ),
        )

        run = start_run(fluxo, conversa)

        run.refresh_from_db()
        assert run.status == FlowRunStatus.FAILED
        assert run.end_reason == "laco"
        assert len(bot_messages(conversa)) <= 25


class TestEventos:
    def test_a_execucao_registra_o_caminho_percorrido(self, clinic_a, conversa):
        """RF-FLW-12: é como o gestor descobre em que pergunta as pessoas somem."""
        run = start_run(fluxo_de_menu(clinic_a), conversa)
        on_inbound(conversa, responder(conversa, interactive_id="agendar"))

        tipos = list(run.events.values_list("event_type", flat=True))

        assert FlowRunEventType.ENTERED in tipos
        assert FlowRunEventType.SENT in tipos
        assert FlowRunEventType.REPLIED in tipos
        assert FlowRunEventType.ENDED in tipos


class TestOrdemDasFalas:
    """
    As falas de um avanço saem NA ORDEM em que o fluxo as produziu.

    Achado no primeiro teste ao vivo: a saudação chegou DEPOIS do menu no
    WhatsApp. A causa era uma task por mensagem com o worker em concorrência
    4: duas chamadas paralelas à Meta, e quem respondesse primeiro chegava
    primeiro.
    """

    def test_o_avanco_enfileira_UMA_task_com_as_falas(
        self, clinic_a, conversa, monkeypatch, django_capture_on_commit_callbacks
    ):
        despachos = []
        monkeypatch.setattr(
            "apps.automation.tasks.enviar_falas_do_fluxo.delay",
            lambda run_id, ids: despachos.append(ids),
        )

        # O despacho sai no `on_commit`, que não roda sozinho dentro da
        # transação do teste.
        with django_capture_on_commit_callbacks(execute=True):
            start_run(fluxo_de_menu(clinic_a), conversa)

        assert len(despachos) == 1, "uma task por avanço, não uma por mensagem"

    def test_a_ordem_da_fila_e_a_ordem_de_producao(
        self, clinic_a, conversa, monkeypatch, django_capture_on_commit_callbacks
    ):
        despachos = []
        monkeypatch.setattr(
            "apps.automation.tasks.enviar_falas_do_fluxo.delay",
            lambda run_id, ids: despachos.append(ids),
        )
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("a", FlowNodeType.SEND_MESSAGE, text="primeira"),
                    node("b", FlowNodeType.SEND_MESSAGE, text="segunda"),
                    node("c", FlowNodeType.SEND_MESSAGE, text="terceira"),
                    node("fim", FlowNodeType.END),
                ],
                [edge("n1", "a"), edge("a", "b"), edge("b", "c"), edge("c", "fim")],
            ),
        )

        with django_capture_on_commit_callbacks(execute=True):
            start_run(fluxo, conversa)

        corpos = [Message.objects.get(pk=i).body for i in despachos[0]]
        assert corpos == ["primeira", "segunda", "terceira"]

    def test_quem_assumiu_no_meio_impede_o_resto_de_sair(
        self, clinic_a, conversa, attendant_a
    ):
        """
        A conferência de posse acontece antes de CADA envio, e não só ao
        montar: entre criar a mensagem e ela sair de fato, alguém pode assumir.
        """
        from apps.automation.tasks import enviar_falas_do_fluxo
        from apps.inbox.attendance import take_over

        start_run(fluxo_de_menu(clinic_a), conversa)
        pendentes = list(
            Message.objects.filter(
                conversation=conversa, sender_kind=SenderKind.BOT, provider_message_id=""
            ).values_list("pk", flat=True)
        )
        conversa.refresh_from_db()
        take_over(conversa, attendant_a)

        resultado = enviar_falas_do_fluxo(1, pendentes)

        assert resultado["enviadas"] == 0, "o robô falou por cima do atendente"

    def test_fala_descartada_nao_fica_na_thread_como_entregue(
        self, clinic_a, conversa, attendant_a
    ):
        """
        A recepção não pode ler no Inbox uma mensagem que o paciente nunca
        recebeu: ela responderia em cima de uma conversa que não aconteceu.
        """
        from apps.automation.tasks import enviar_falas_do_fluxo
        from apps.inbox.attendance import take_over
        from apps.inbox.choices import MessageStatus

        start_run(fluxo_de_menu(clinic_a), conversa)
        pendentes = list(
            Message.objects.filter(
                conversation=conversa, sender_kind=SenderKind.BOT, provider_message_id=""
            ).values_list("pk", flat=True)
        )
        conversa.refresh_from_db()
        take_over(conversa, attendant_a)

        enviar_falas_do_fluxo(1, pendentes)

        for message in Message.objects.filter(pk__in=pendentes):
            assert message.status == MessageStatus.FAILED
            assert message.status_error


class TestOPacienteQueFazBagunca:
    """
    O paciente rola a conversa e toca num botão de três passos atrás, manda
    três mensagens em rajada, responde fora de hora. Nada disso pode
    desalinhar o fluxo (06/08/2026, levantado pelo usuário e conferido contra
    o wacrm, que resolve as mesmas três coisas).
    """

    def _fluxo_com_pergunta(self, clinic):
        """Menu de botões → pergunta o nome → repete o nome."""
        return make_flow(
            clinic,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "menu",
                        FlowNodeType.SEND_BUTTONS,
                        text="O que você deseja?",
                        buttons=[
                            {"id": "agendar", "title": "Marcar consulta"},
                            {"id": "humano", "title": "Falar com atendente"},
                        ],
                    ),
                    node(
                        "nome",
                        FlowNodeType.COLLECT_INPUT,
                        prompt_text="Qual o seu nome completo?",
                        var_key="nome",
                    ),
                    node("eco", FlowNodeType.SEND_MESSAGE, text="Prazer, {{nome}}!"),
                    node("fim", FlowNodeType.END),
                    node("humano", FlowNodeType.HANDOFF, note="quer gente"),
                ],
                [
                    edge("n1", "menu"),
                    edge("menu", "nome", "button:agendar"),
                    edge("menu", "humano", "button:humano"),
                    edge("nome", "eco"),
                    edge("eco", "fim"),
                ],
            ),
        )

    def test_botao_VELHO_nao_vira_o_nome_do_paciente(self, clinic_a, conversa):
        """
        O caso que motivou tudo. O WhatsApp manda o id E o título do botão;
        sem a guarda, `nome` virava "Marcar consulta" e o fluxo seguia em
        frente sem reclamar, com a recepção recebendo isso na nota.
        """
        run = start_run(self._fluxo_com_pergunta(clinic_a), conversa)
        on_inbound(conversa, responder(conversa, interactive_id="agendar"))
        run.refresh_from_db()
        assert run.current_node == "nome", "o fluxo está esperando o NOME"

        # Rola a conversa e toca de novo no botão de antes.
        on_inbound(
            conversa,
            responder(conversa, interactive_id="agendar", texto="Marcar consulta"),
        )

        run.refresh_from_db()
        assert run.vars.get("nome") is None, "toque não é resposta a pergunta aberta"
        assert run.current_node == "nome", "continua esperando o nome"

    def test_e_o_paciente_ainda_consegue_responder_depois(self, clinic_a, conversa):
        """A guarda repete a pergunta, não trava o fluxo."""
        run = start_run(self._fluxo_com_pergunta(clinic_a), conversa)
        on_inbound(conversa, responder(conversa, interactive_id="agendar"))
        on_inbound(conversa, responder(conversa, interactive_id="agendar", texto="Marcar consulta"))

        on_inbound(conversa, responder(conversa, texto="Gabriel Rocha"))

        run.refresh_from_db()
        assert run.vars["nome"] == "Gabriel Rocha"

    def test_o_toque_fora_de_hora_REPETE_a_pergunta(self, clinic_a, conversa):
        run = start_run(self._fluxo_com_pergunta(clinic_a), conversa)
        on_inbound(conversa, responder(conversa, interactive_id="agendar"))
        antes = len(bot_messages(conversa))

        on_inbound(conversa, responder(conversa, interactive_id="agendar", texto="Marcar consulta"))

        falas = bot_messages(conversa)
        assert len(falas) == antes + 1
        assert falas[-1].body == "Qual o seu nome completo?"
        run.refresh_from_db()
        assert run.reprompt_count == 1

    def test_botao_de_OUTRO_no_nao_suja_a_variavel(self, clinic_a, conversa):
        """
        O avanço em si já era barrado pelo grafo (a aresta não existe naquele
        nó). O que a conferência acrescenta é NÃO GRAVAR a variável antes de
        descobrir isso: sem ela, `_guardar_escolha` rodava primeiro e o valor
        ficava com o título do botão velho, mesmo com o fluxo parado no lugar.
        """
        fluxo = make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "primeiro",
                        FlowNodeType.SEND_BUTTONS,
                        text="Confirma?",
                        buttons=[{"id": "sim", "title": "Sim, confirmo"}],
                    ),
                    node(
                        "segundo",
                        FlowNodeType.SEND_BUTTONS,
                        text="Quer receber lembrete?",
                        buttons=[{"id": "quero", "title": "Quero"}],
                        var_key="lembrete",
                    ),
                    node("fim", FlowNodeType.END),
                ],
                [
                    edge("n1", "primeiro"),
                    edge("primeiro", "segundo", "button:sim"),
                    edge("segundo", "fim", "button:quero"),
                ],
            ),
        )
        run = start_run(fluxo, conversa)
        on_inbound(conversa, responder(conversa, interactive_id="sim"))
        run.refresh_from_db()
        assert run.current_node == "segundo"

        # Rola a conversa e toca no "Sim, confirmo" lá de trás.
        on_inbound(
            conversa,
            responder(conversa, interactive_id="sim", texto="Sim, confirmo"),
        )

        run.refresh_from_db()
        assert run.current_node == "segundo", "não pode ter avançado"
        assert "lembrete" not in (run.vars or {}), "nem sujar a variável do passo"
        assert run.status == FlowRunStatus.ACTIVE

    def test_toque_em_botao_NAO_comeca_fluxo_nenhum(self, clinic_a, conversa):
        """
        Ele é resposta a uma pergunta que já foi feita. O WhatsApp manda junto
        o TÍTULO, então um fluxo com a palavra "consulta" dispararia quando o
        paciente tocasse num botão velho chamado "Marcar consulta".
        """
        from apps.automation.triggers import pick_flow

        make_flow(
            clinic_a,
            status=FlowStatus.ACTIVE,
            trigger="keyword",
            trigger_config={"match": "contains", "keywords": ["consulta"]},
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("oi", FlowNodeType.SEND_MESSAGE, text="Olá!"),
                    node("fim", FlowNodeType.END),
                ],
                [edge("n1", "oi"), edge("oi", "fim")],
            ),
        )

        toque = responder(conversa, interactive_id="agendar", texto="Marcar consulta")
        digitado = responder(conversa, texto="quero marcar consulta")

        assert pick_flow(conversa, toque) is None
        assert pick_flow(conversa, digitado) is not None, "digitar continua valendo"


class TestAEscolhaVIRAVariavel:
    """
    Botão e lista guardam o que foi escolhido quando o nó tem `var_key`
    (06/08/2026, achado no teste ao vivo).

    Antes só `coletar resposta` guardava, e por isso a única forma de saber
    algo era perguntar por texto livre. À pergunta "você tem convênio?" o
    paciente respondeu "Tenho", e a recepção abriu a conversa com
    `Pagamento: Tenho`. Pior: o tipo de atendimento escolhido na lista não
    chegava à nota de jeito nenhum.
    """

    def _fluxo_com_botao(self, clinic, **extra):
        return make_flow(
            clinic,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "escolha",
                        FlowNodeType.SEND_BUTTONS,
                        text="Como será o pagamento?",
                        buttons=[
                            {"id": "particular", "title": "Particular"},
                            {"id": "convenio", "title": "Tenho convênio"},
                        ],
                        **extra,
                    ),
                    node("eco", FlowNodeType.SEND_MESSAGE, text="Anotado: {{pagamento}}"),
                    node("fim", FlowNodeType.END),
                ],
                [
                    edge("n1", "escolha"),
                    edge("escolha", "eco", "button:particular"),
                    edge("escolha", "eco", "button:convenio"),
                    edge("eco", "fim"),
                ],
            ),
        )

    def test_guarda_o_TITULO_e_nao_o_id(self, clinic_a, conversa):
        """A variável existe para ser lida por gente: "Tenho convênio" diz o
        que `convenio` não diz."""
        run = start_run(self._fluxo_com_botao(clinic_a, var_key="pagamento"), conversa)

        on_inbound(conversa, responder(conversa, interactive_id="convenio"))

        run.refresh_from_db()
        assert run.vars["pagamento"] == "Tenho convênio"

    def test_a_escolha_aparece_na_fala_seguinte(self, clinic_a, conversa):
        start_run(self._fluxo_com_botao(clinic_a, var_key="pagamento"), conversa)

        on_inbound(conversa, responder(conversa, interactive_id="particular"))

        ultima = Message.objects.filter(
            conversation=conversa, sender_kind=SenderKind.BOT
        ).order_by("-pk").first()
        assert ultima.body == "Anotado: Particular"

    def test_sem_var_key_nada_e_guardado(self, clinic_a, conversa):
        """A variável é opcional: quem só desvia o fluxo pela aresta não quer
        lixo em `vars`."""
        run = start_run(self._fluxo_com_botao(clinic_a), conversa)

        on_inbound(conversa, responder(conversa, interactive_id="particular"))

        run.refresh_from_db()
        assert run.vars == {}

    def test_resposta_digitada_guarda_o_texto_da_pessoa(self, clinic_a, conversa):
        """
        Sem toque em botão não há título para procurar. Guardar o que a pessoa
        escreveu é melhor do que variável vazia numa nota que alguém vai ler.
        """
        fluxo = self._fluxo_com_botao(clinic_a, var_key="pagamento")
        fluxo.current_version.graph["edges"].append(
            {"from": "escolha", "to": "eco", "condition": "pago em dinheiro"}
        )
        fluxo.current_version.save(update_fields=["graph"])
        run = start_run(fluxo, conversa)

        on_inbound(conversa, responder(conversa, texto="pago em dinheiro"))

        run.refresh_from_db()
        assert run.vars["pagamento"] == "pago em dinheiro"


class TestAUltimaFalaSempreSai:
    """
    A mensagem de encerramento é a que o paciente MAIS precisa ler, e era
    justamente a única que nunca saía (achado ao vivo em 31/07/2026: o
    paciente respondeu o nome e a confirmação nunca chegou).

    A causa: `_finish` devolve a conversa para a fila ANTES de despachar, e a
    task barrava tudo que não estivesse com posse `bot`. Como o próprio motor
    tinha acabado de soltar a posse, a última fala era descartada sempre.
    """

    def _fluxo_de_uma_fala(self, clinic):
        return make_flow(
            clinic,
            status=FlowStatus.ACTIVE,
            graph=grafo(
                [
                    node("n1", FlowNodeType.START),
                    node("tchau", FlowNodeType.SEND_MESSAGE, text="Prazer, até já!"),
                    node("fim", FlowNodeType.END),
                ],
                [edge("n1", "tchau"), edge("tchau", "fim")],
            ),
        )

    def test_conversa_devolvida_para_a_fila_nao_engole_a_despedida(
        self, clinic_a, conversa
    ):
        from apps.automation.tasks import enviar_falas_do_fluxo

        run = start_run(self._fluxo_de_uma_fala(clinic_a), conversa)

        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.NONE, "o fluxo terminou e soltou"
        pendente = Message.objects.get(
            conversation=conversa, sender_kind=SenderKind.BOT, provider_message_id=""
        )

        resultado = enviar_falas_do_fluxo(run.pk, [pendente.pk])

        assert resultado["enviadas"] == 1, "a última fala do fluxo nunca chegou"
        pendente.refresh_from_db()
        assert pendente.provider_message_id, "saiu sem wamid: não foi para a Meta"
