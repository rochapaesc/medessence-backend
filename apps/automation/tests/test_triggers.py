from datetime import time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.automation.choices import FlowNodeType, FlowRunStatus, FlowStatus, FlowTrigger
from apps.automation.models import FlowRun
from apps.automation.tasks import sweep_flow_runs
from apps.automation.tests.conftest import (
    make_contact,
    make_conversation,
    make_flow,
    make_inbox,
)
from apps.automation.triggers import handle_inbound, pick_flow
from apps.inbox.choices import AttendedBy, ConversationStatus, MessageKind, SenderKind
from apps.inbox.models import Message
from apps.tenants.models import ClinicBusinessHours

pytestmark = pytest.mark.django_db


def node(node_id, tipo, **config):
    return {"id": node_id, "type": tipo, "label": node_id, "config": config}


def edge(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


def fluxo_simples(clinic, *, name="Boas-vindas", texto="Olá!", **extra):
    return make_flow(
        clinic,
        name=name,
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                node("n1", FlowNodeType.START),
                node("msg", FlowNodeType.SEND_MESSAGE, text=texto),
                node("esperar", FlowNodeType.COLLECT_INPUT, prompt_text="?", var_key="x"),
            ],
            "edges": [edge("n1", "msg"), edge("msg", "esperar")],
        },
        **extra,
    )


def chegou(conversation, texto="oi"):
    """Mensagem do paciente, como a ingestão a teria criado."""
    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        sender_kind=SenderKind.CONTACT,
        kind=MessageKind.TEXT,
        body=texto,
        wa_timestamp=timezone.now(),
    )


def abrir_a_clinica(clinic):
    agora = timezone.now().astimezone(ZoneInfo(clinic.timezone))
    ClinicBusinessHours.objects.create(
        clinic=clinic,
        weekday=agora.weekday(),
        opens_at=time(0, 1),
        closes_at=time(23, 59),
    )


@pytest.fixture
def conversa(clinic_a):
    return make_conversation(clinic_a, make_contact(clinic_a))


class TestPrimeiraMensagem:
    def test_dispara_na_primeira_fala_do_contato(self, clinic_a, conversa):
        fluxo_simples(clinic_a)

        assert handle_inbound(conversa, chegou(conversa)) is True

        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.BOT

    def test_nao_dispara_na_segunda(self, clinic_a, conversa):
        """
        Senão o robô reiniciaria a conversa toda vez que o paciente falasse.
        """
        fluxo_simples(clinic_a)
        chegou(conversa, "primeira")

        assert handle_inbound(conversa, chegou(conversa, "segunda")) is False

    def test_fluxo_em_rascunho_nao_dispara(self, clinic_a, conversa):
        fluxo = fluxo_simples(clinic_a)
        fluxo.status = FlowStatus.DRAFT
        fluxo.save(update_fields=["status"])

        assert handle_inbound(conversa, chegou(conversa)) is False

    def test_fluxo_arquivado_nao_dispara(self, clinic_a, conversa):
        fluxo = fluxo_simples(clinic_a)
        fluxo.status = FlowStatus.ARCHIVED
        fluxo.save(update_fields=["status"])

        assert handle_inbound(conversa, chegou(conversa)) is False

    def test_fluxo_de_outra_clinica_nao_dispara(self, clinic_b, conversa):
        fluxo_simples(clinic_b)

        assert handle_inbound(conversa, chegou(conversa)) is False

    def test_gatilho_manual_nunca_dispara_por_mensagem(self, clinic_a, conversa):
        fluxo_simples(clinic_a, trigger=FlowTrigger.MANUAL)

        assert handle_inbound(conversa, chegou(conversa)) is False


class TestPalavraChave:
    def test_casa_por_conteudo(self, clinic_a, conversa):
        fluxo_simples(
            clinic_a,
            trigger=FlowTrigger.KEYWORD,
            trigger_config={"keywords": ["orçamento"], "match": "contains"},
        )

        assert handle_inbound(conversa, chegou(conversa, "queria um orçamento")) is True

    def test_nao_casa_texto_diferente(self, clinic_a, conversa):
        fluxo_simples(
            clinic_a,
            trigger=FlowTrigger.KEYWORD,
            trigger_config={"keywords": ["orçamento"], "match": "contains"},
        )

        assert handle_inbound(conversa, chegou(conversa, "bom dia")) is False

    def test_ignora_maiuscula(self, clinic_a, conversa):
        fluxo_simples(
            clinic_a,
            trigger=FlowTrigger.KEYWORD,
            trigger_config={"keywords": ["orçamento"], "match": "contains"},
        )

        assert handle_inbound(conversa, chegou(conversa, "ORÇAMENTO")) is True

    def test_modo_exato_nao_casa_frase(self, clinic_a, conversa):
        fluxo_simples(
            clinic_a,
            trigger=FlowTrigger.KEYWORD,
            trigger_config={"keywords": ["menu"], "match": "exact"},
        )

        assert handle_inbound(conversa, chegou(conversa, "quero o menu por favor")) is False

    def test_palavra_chave_vale_a_qualquer_momento_da_conversa(self, clinic_a, conversa):
        """Diferente do first_inbound: "menu" funciona na décima mensagem."""
        fluxo_simples(
            clinic_a,
            trigger=FlowTrigger.KEYWORD,
            trigger_config={"keywords": ["menu"], "match": "exact"},
        )
        chegou(conversa, "bom dia")

        assert handle_inbound(conversa, chegou(conversa, "menu")) is True


class TestForaDoHorario:
    def test_com_a_clinica_aberta_o_fluxo_marcado_nao_dispara(self, clinic_a, conversa):
        """
        Dentro do expediente a conversa vai direto para a recepção
        (RF-FLW-5.1) - é o que faz o robô valer para uma equipe pequena.
        """
        abrir_a_clinica(clinic_a)
        fluxo_simples(clinic_a, only_outside_hours=True)

        assert handle_inbound(conversa, chegou(conversa)) is False

    def test_com_a_clinica_fechada_o_fluxo_atende(self, clinic_a, conversa):
        fluxo_simples(clinic_a, only_outside_hours=True)

        assert handle_inbound(conversa, chegou(conversa)) is True

    def test_fluxo_sem_a_marca_dispara_de_qualquer_jeito(self, clinic_a, conversa):
        abrir_a_clinica(clinic_a)
        fluxo_simples(clinic_a, only_outside_hours=False)

        assert handle_inbound(conversa, chegou(conversa)) is True


class TestDesempate:
    def test_prioridade_menor_vence(self, clinic_a, conversa):
        fluxo_simples(clinic_a, name="Geral", texto="Sou o geral", priority=20)
        fluxo_simples(clinic_a, name="Prioritário", texto="Sou o prioritário", priority=1)

        handle_inbound(conversa, chegou(conversa))

        enviada = Message.objects.filter(conversation=conversa, sender_kind=SenderKind.BOT).first()
        assert enviada.body == "Sou o prioritário"

    def test_no_empate_vence_o_ativado_por_ultimo(self, clinic_a, conversa):
        antigo = fluxo_simples(clinic_a, name="Antigo", texto="Sou o antigo", priority=10)
        antigo.activated_at = timezone.now() - timedelta(days=3)
        antigo.save(update_fields=["activated_at"])

        novo = fluxo_simples(clinic_a, name="Novo", texto="Sou o novo", priority=10)
        novo.activated_at = timezone.now()
        novo.save(update_fields=["activated_at"])

        handle_inbound(conversa, chegou(conversa))

        enviada = Message.objects.filter(conversation=conversa, sender_kind=SenderKind.BOT).first()
        assert enviada.body == "Sou o novo"

    def test_so_um_fluxo_e_escolhido(self, clinic_a, conversa):
        fluxo_simples(clinic_a, name="A", priority=1)
        fluxo_simples(clinic_a, name="B", priority=2)

        handle_inbound(conversa, chegou(conversa))

        assert FlowRun.objects.count() == 1

    def test_execucao_em_andamento_tem_precedencia_sobre_gatilho_novo(self, clinic_a, conversa):
        """
        A palavra "orçamento" dita no meio de um agendamento não pode
        reiniciar a conversa do zero.
        """
        fluxo_simples(clinic_a, name="Boas-vindas", priority=1)
        fluxo_simples(
            clinic_a,
            name="Orçamento",
            priority=1,
            trigger=FlowTrigger.KEYWORD,
            trigger_config={"keywords": ["orçamento"], "match": "contains"},
        )
        handle_inbound(conversa, chegou(conversa))
        run = FlowRun.objects.get()

        handle_inbound(conversa, chegou(conversa, "quero um orçamento"))

        assert FlowRun.objects.count() == 1
        assert FlowRun.objects.get().pk == run.pk


class TestSemFluxoNenhum:
    def test_clinica_sem_fluxo_ativo_nao_muda_nada(self, conversa):
        assert handle_inbound(conversa, chegou(conversa)) is False

        conversa.refresh_from_db()
        assert conversa.attended_by == AttendedBy.NONE

    def test_pick_flow_sem_candidatos(self, conversa):
        assert pick_flow(conversa, chegou(conversa)) is None


class TestVarredura:
    def test_conversa_que_mudou_de_dono_pausa_a_execucao(self, clinic_a, conversa, attendant_a):
        """
        O atendente assumiu e o paciente nunca mais escreveu. Sem a varredura a
        execução ficaria ACTIVE para sempre, ocupando a trava do contato e
        impedindo qualquer fluxo futuro para aquela pessoa.
        """
        from apps.inbox.attendance import take_over

        fluxo_simples(clinic_a)
        handle_inbound(conversa, chegou(conversa))
        conversa.refresh_from_db()
        take_over(conversa, attendant_a)

        assert sweep_flow_runs()["assumidas"] == 1
        assert FlowRun.objects.get().status == FlowRunStatus.PAUSED_BY_AGENT

    def test_silencio_longo_entrega_ao_humano(self, clinic_a, conversa):
        fluxo = fluxo_simples(clinic_a)
        fluxo.fallback = {"max_reprompts": 2, "on_timeout_hours": 24, "on_exhaust": "handoff"}
        fluxo.save(update_fields=["fallback"])
        handle_inbound(conversa, chegou(conversa))

        run = FlowRun.objects.get()
        run.last_advanced_at = timezone.now() - timedelta(hours=25)
        run.save(update_fields=["last_advanced_at"])

        assert sweep_flow_runs()["expiradas"] == 1

        run.refresh_from_db()
        conversa.refresh_from_db()
        assert run.status == FlowRunStatus.TIMED_OUT
        assert conversa.status == ConversationStatus.WAITING
        assert conversa.attended_by == AttendedBy.NONE

    def test_dentro_do_prazo_nao_expira(self, clinic_a, conversa):
        fluxo_simples(clinic_a)
        handle_inbound(conversa, chegou(conversa))

        assert sweep_flow_runs()["expiradas"] == 0
        assert FlowRun.objects.get().status == FlowRunStatus.ACTIVE

    def test_espera_do_relogio_nao_conta_como_silencio(self, clinic_a, conversa):
        """
        Um fluxo parado no nó "Aguardar 3 dias" não pode ser encerrado por
        inatividade: ali quem manda é o relógio do fluxo, não o silêncio do
        paciente.
        """
        fluxo = fluxo_simples(clinic_a)
        fluxo.fallback = {"max_reprompts": 2, "on_timeout_hours": 1, "on_exhaust": "handoff"}
        fluxo.save(update_fields=["fallback"])
        handle_inbound(conversa, chegou(conversa))

        run = FlowRun.objects.get()
        run.last_advanced_at = timezone.now() - timedelta(hours=5)
        run.wake_at = timezone.now() + timedelta(days=2)
        run.save(update_fields=["last_advanced_at", "wake_at"])

        assert sweep_flow_runs()["expiradas"] == 0
        assert FlowRun.objects.get().status == FlowRunStatus.ACTIVE

    def test_varredura_sem_execucao_nenhuma_nao_estoura(self):
        assert sweep_flow_runs() == {"assumidas": 0, "acordadas": 0, "expiradas": 0}


class TestLigacaoComAIngestao:
    def test_a_ingestao_aciona_o_motor_pelo_sinal(self, clinic_a):
        """
        O caminho real: `ingest_events` cria a mensagem, emite o sinal, e o
        motor responde. É o que faz o "oi" das 22h ser atendido sem ninguém
        chamar o motor à mão.
        """
        from apps.inbox.services import ingest_events
        from apps.integrations.whatsapp.events import parse_meta_webhook
        from apps.integrations.whatsapp.fake.adapter import build_inbound_payload

        inbox = make_inbox(clinic_a)
        fluxo_simples(clinic_a, texto="Olá! Sou o assistente.")
        payload = build_inbound_payload(wa_id=inbox["contact"].wa_id, body="oi")

        ingest_events(inbox["channel"], parse_meta_webhook(payload))

        conversation = inbox["conversation"]
        conversation.refresh_from_db()
        assert conversation.attended_by == AttendedBy.BOT
        assert Message.objects.filter(
            conversation=conversation, sender_kind=SenderKind.BOT
        ).exists()

    def test_falha_do_motor_nao_derruba_a_ingestao(self, clinic_a, monkeypatch):
        """
        Mensagem que não foi gravada porque o motor estourou é pior do que
        fluxo que não respondeu: a recepção deixa de ver que o paciente
        escreveu, e a Meta reentregaria o webhook tentando de novo.
        """
        from apps.inbox.services import ingest_events
        from apps.integrations.whatsapp.events import parse_meta_webhook
        from apps.integrations.whatsapp.fake.adapter import build_inbound_payload

        def explode(*args, **kwargs):
            raise RuntimeError("motor quebrado")

        monkeypatch.setattr("apps.automation.triggers.handle_inbound", explode)
        inbox = make_inbox(clinic_a)
        fluxo_simples(clinic_a)
        payload = build_inbound_payload(wa_id=inbox["contact"].wa_id, body="oi")

        stats = ingest_events(inbox["channel"], parse_meta_webhook(payload))

        assert stats["inbound"] == 1
        assert Message.objects.filter(
            conversation=inbox["conversation"], sender_kind=SenderKind.CONTACT
        ).exists()


# ---- a cota do repique não é gasta pela sequência (18/08/2026) ----


class TestCotaDoRepique:
    """
    ⚠️ Achado ao vivo: seis passos de sequência em vinte minutos gastaram a cota
    do contato, e a palavra-chave foi recusada duas vezes seguidas. Quem estava
    testando achou que o gatilho tinha quebrado.

    A trava é contra o repique do PACIENTE em conversa. Passo agendado pela
    clínica é calendário, e calendário não repica.
    """

    def _execucoes(self, clinic, conversation, quantas, *, de_sequencia):
        """Cria execuções para o contato, de sequência ou de conversa."""
        from datetime import time as hora

        from django.utils import timezone

        from apps.automation.choices import SequenceDispatchStatus
        from apps.automation.models import (
            FlowRun,
            Sequence,
            SequenceDispatch,
            SequenceEnrollment,
            SequenceStep,
        )
        from apps.automation.tests.conftest import make_flow

        flow = make_flow(clinic, name=f"Fluxo {de_sequencia}", status=FlowStatus.ACTIVE)
        criadas = []
        for i in range(quantas):
            # CONCLUÍDAS: a trava do banco (RF-FLW-6) só deixa UMA ativa
            # por contato, e o estado real depois de um passo é este - o fluxo
            # termina e a conversa volta para a fila.
            run = FlowRun.objects.create(
                clinic=clinic,
                flow=flow,
                version=flow.current_version,
                contact=conversation.contact,
                conversation=conversation,
                current_node="n1",
                status=FlowRunStatus.COMPLETED,
            )
            criadas.append(run)
            if not de_sequencia:
                continue
            trilha = Sequence.objects.create(clinic=clinic, name=f"Trilha {i}")
            passo = SequenceStep.objects.create(
                sequence=trilha, order=1, offset_days=0, send_time=hora(9, 0), flow=flow
            )
            inscricao = SequenceEnrollment.objects.create(
                clinic=clinic,
                sequence=trilha,
                contact=conversation.contact,
                anchor_at=timezone.now(),
            )
            SequenceDispatch.objects.create(
                enrollment=inscricao,
                step=passo,
                scheduled_for=timezone.now(),
                resolved_at=timezone.now(),
                status=SequenceDispatchStatus.STARTED,
                flow_run=run,
            )
        return criadas

    def test_passos_de_sequencia_nao_gastam_a_cota(self, clinic_a):
        from apps.automation.triggers import _repicou_demais

        inbox = make_inbox(clinic_a)
        conversa = inbox["conversation"]
        self._execucoes(clinic_a, conversa, 6, de_sequencia=True)

        assert _repicou_demais(conversa) is False, (
            "seis passos de trilha não podem impedir o paciente de usar uma "
            "palavra-chave"
        )

    def test_conversas_automaticas_de_verdade_ainda_travam(self, clinic_a):
        """A trava continua existindo: ela é contra robô do outro lado."""
        from apps.automation.triggers import _repicou_demais

        inbox = make_inbox(clinic_a)
        conversa = inbox["conversation"]
        self._execucoes(clinic_a, conversa, 3, de_sequencia=False)

        assert _repicou_demais(conversa) is True

    def test_a_trava_deixa_nota_interna_para_quem_atende(self, clinic_a):
        """
        Antes, a recusa vivia só no log: quem estava com a conversa aberta via o
        paciente escrever, o robô calar, e concluía que a mensagem tinha sumido.
        """
        from apps.inbox.models import Message
        from apps.automation.triggers import _repicou_demais

        inbox = make_inbox(clinic_a)
        conversa = inbox["conversation"]
        self._execucoes(clinic_a, conversa, 3, de_sequencia=False)

        _repicou_demais(conversa)

        nota = Message.objects.filter(conversation=conversa, is_internal=True).first()
        assert nota is not None
        assert "trava contra repetição" in nota.body
        # Nunca sai para o paciente.
        assert nota.is_internal is True
        assert nota.provider_message_id == ""

    def test_a_nota_nao_se_repete_a_cada_mensagem(self, clinic_a):
        """Quem repica manda várias seguidas; uma nota por mensagem entulharia
        a conversa com o aviso de que ela está entulhada."""
        from apps.inbox.models import Message
        from apps.automation.triggers import _repicou_demais

        inbox = make_inbox(clinic_a)
        conversa = inbox["conversation"]
        self._execucoes(clinic_a, conversa, 3, de_sequencia=False)

        for _ in range(4):
            _repicou_demais(conversa)

        assert Message.objects.filter(conversation=conversa, is_internal=True).count() == 1
