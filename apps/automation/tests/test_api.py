import pytest

from apps.automation.choices import FlowNodeType, FlowRunStatus, FlowStatus, FlowTrigger
from apps.automation.models import Flow, FlowRun, FlowVersion
from apps.automation.tests.conftest import (
    make_contact,
    make_conversation,
    make_flow,
    make_inbox,
)

pytestmark = pytest.mark.django_db

FLOWS_URL = "/api/v1/flows/"
RUNS_URL = "/api/v1/flow-runs/"


def node(node_id, tipo, **config):
    return {"id": node_id, "type": tipo, "label": node_id, "config": config}


def edge(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


GRAFO_BOM = {
    "entry_node": "n1",
    "nodes": [
        node("n1", FlowNodeType.START),
        node("msg", FlowNodeType.SEND_MESSAGE, text="Olá!"),
        node("fim", FlowNodeType.END),
    ],
    "edges": [edge("n1", "msg"), edge("msg", "fim")],
}

GRAFO_QUEBRADO = {
    "entry_node": "n1",
    "nodes": [
        node("n1", FlowNodeType.START),
        node("msg", FlowNodeType.SEND_MESSAGE, text=""),
        node("orfao", FlowNodeType.SEND_MESSAGE, text="ninguém chega"),
    ],
    "edges": [edge("n1", "msg")],
}


@pytest.fixture
def gestor(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


class TestCriarEEditar:
    def test_fluxo_novo_nasce_em_rascunho_com_a_versao_1(self, gestor):
        resposta = gestor.post(
            FLOWS_URL, {"name": "Boas-vindas", "graph": GRAFO_BOM}, format="json"
        )

        assert resposta.status_code == 201
        assert resposta.data["status"] == FlowStatus.DRAFT
        assert resposta.data["current_version_number"] == 1

    def test_mexer_no_desenho_cria_versao_nova(self, gestor, clinic_a):
        flow = make_flow(clinic_a, graph=GRAFO_BOM)

        gestor.patch(f"{FLOWS_URL}{flow.pk}/", {"graph": GRAFO_BOM}, format="json")

        flow.refresh_from_db()
        assert flow.versions.count() == 2
        assert flow.current_version.number == 2

    def test_mudar_so_a_politica_nao_versiona(self, gestor, clinic_a):
        """
        Renomear o fluxo ou mexer na prioridade não é mudar o desenho - e
        versionar à toa encheria a lista de versões idênticas.
        """
        flow = make_flow(clinic_a, graph=GRAFO_BOM)

        gestor.patch(f"{FLOWS_URL}{flow.pk}/", {"priority": 3}, format="json")

        flow.refresh_from_db()
        assert flow.versions.count() == 1
        assert flow.priority == 3

    def test_a_lista_diz_quantos_pacientes_estao_dentro(self, gestor, clinic_a):
        """O número que faz o gestor pensar antes de mexer no desenho."""
        flow = make_flow(clinic_a, graph=GRAFO_BOM)
        contato = make_contact(clinic_a)
        FlowRun.objects.create(
            clinic=clinic_a,
            flow=flow,
            version=flow.current_version,
            contact=contato,
            conversation=make_conversation(clinic_a, contato),
        )

        resposta = gestor.get(FLOWS_URL)

        assert resposta.data["results"][0]["runs_active"] == 1


class TestAtivacao:
    def test_grafo_bom_ativa(self, gestor, clinic_a):
        flow = make_flow(clinic_a, graph=GRAFO_BOM)

        resposta = gestor.post(f"{FLOWS_URL}{flow.pk}/activate/", format="json")

        assert resposta.status_code == 200
        flow.refresh_from_db()
        assert flow.status == FlowStatus.ACTIVE
        assert flow.activated_at is not None
        assert flow.current_version.published_at is not None

    def test_grafo_quebrado_e_recusado_com_a_lista_de_pendencias(self, gestor, clinic_a):
        """
        As frases voltam em português de gente: quem conserta é o gestor, e
        "nó órfão" não diz nada para quem não desenha grafo.
        """
        flow = make_flow(clinic_a, graph=GRAFO_QUEBRADO)

        resposta = gestor.post(f"{FLOWS_URL}{flow.pk}/activate/", format="json")

        assert resposta.status_code == 400
        assert len(resposta.data["problems"]) >= 2
        flow.refresh_from_db()
        assert flow.status == FlowStatus.DRAFT

    def test_fluxo_sem_desenho_nao_ativa(self, gestor, clinic_a):
        flow = make_flow(clinic_a)
        flow.current_version = None
        flow.save(update_fields=["current_version"])

        resposta = gestor.post(f"{FLOWS_URL}{flow.pk}/activate/", format="json")

        assert resposta.status_code == 400

    def test_rascunho_salva_quebrado_de_proposito(self, gestor):
        """
        Montar um fluxo é trabalho de várias sessões: exigir grafo íntegro
        para SALVAR obrigaria o gestor a terminar de uma vez.
        """
        resposta = gestor.post(
            FLOWS_URL, {"name": "Em construção", "graph": GRAFO_QUEBRADO}, format="json"
        )

        assert resposta.status_code == 201
        assert resposta.data["can_activate"] is False

    def test_desativar_volta_para_rascunho(self, gestor, clinic_a):
        flow = make_flow(clinic_a, status=FlowStatus.ACTIVE, graph=GRAFO_BOM)

        resposta = gestor.post(f"{FLOWS_URL}{flow.pk}/deactivate/", format="json")

        assert resposta.status_code == 200
        flow.refresh_from_db()
        assert flow.status == FlowStatus.DRAFT

    def test_desativar_nao_derruba_quem_esta_no_meio(self, gestor, clinic_a):
        """
        Cortar a execução no meio deixaria o paciente falando sozinho. Elas
        seguem na versão em que começaram até terminar ou cair no timeout.
        """
        flow = make_flow(clinic_a, status=FlowStatus.ACTIVE, graph=GRAFO_BOM)
        contato = make_contact(clinic_a)
        run = FlowRun.objects.create(
            clinic=clinic_a,
            flow=flow,
            version=flow.current_version,
            contact=contato,
            conversation=make_conversation(clinic_a, contato),
        )

        gestor.post(f"{FLOWS_URL}{flow.pk}/deactivate/", format="json")

        run.refresh_from_db()
        assert run.status == FlowRunStatus.ACTIVE


class TestVersoes:
    def test_lista_as_versoes_com_as_pendencias_de_cada_uma(self, gestor, clinic_a):
        flow = make_flow(clinic_a, graph=GRAFO_QUEBRADO)

        resposta = gestor.get(f"{FLOWS_URL}{flow.pk}/versions/")

        assert resposta.status_code == 200
        assert resposta.data[0]["problems"]


class TestPermissoes:
    def test_atendente_nao_ve_fluxos(self, api_client, attendant_a, clinic_a):
        """
        Diferente do catálogo de etiquetas: um fluxo mal montado responde no
        lugar da clínica para todo paciente que escrever.
        """
        make_flow(clinic_a, graph=GRAFO_BOM)
        api_client.force_authenticate(attendant_a)

        assert api_client.get(FLOWS_URL).status_code == 403

    def test_atendente_nao_ativa(self, api_client, attendant_a, clinic_a):
        flow = make_flow(clinic_a, graph=GRAFO_BOM)
        api_client.force_authenticate(attendant_a)

        assert api_client.post(f"{FLOWS_URL}{flow.pk}/activate/").status_code == 403

    def test_fluxo_de_outra_clinica_nao_aparece(self, gestor, clinic_a, clinic_b):
        make_flow(clinic_a, name="Meu", graph=GRAFO_BOM)
        make_flow(clinic_b, name="Da outra", graph=GRAFO_BOM)

        resposta = gestor.get(FLOWS_URL)

        assert [f["name"] for f in resposta.data["results"]] == ["Meu"]

    def test_execucao_de_outra_clinica_nao_aparece(self, gestor, clinic_a, clinic_b):
        for clinic in (clinic_a, clinic_b):
            flow = make_flow(clinic, graph=GRAFO_BOM)
            contato = make_contact(clinic, wa_id=f"55859000000{clinic.pk}")
            FlowRun.objects.create(
                clinic=clinic,
                flow=flow,
                version=flow.current_version,
                contact=contato,
                conversation=make_conversation(clinic, contato),
            )

        resposta = gestor.get(RUNS_URL)

        assert resposta.data["count"] == 1


class TestSeedDeDemonstracao:
    def test_o_fluxo_semeado_passa_no_proprio_validador(self, clinic_a):
        """
        É o exemplo que o cliente vê primeiro: se nasce quebrado, o defeito é
        nosso e não de quem monta.
        """
        from django.core.management import call_command

        call_command("seed_flow_demo", clinic=clinic_a.pk)

        flow = Flow.objects.get(clinic=clinic_a)
        assert flow.status == FlowStatus.DRAFT
        assert flow.trigger == FlowTrigger.FIRST_INBOUND
        assert flow.only_outside_hours is True

    def test_ativar_pelo_comando_publica(self, clinic_a):
        from django.core.management import call_command

        call_command("seed_flow_demo", clinic=clinic_a.pk, ativar=True)

        flow = Flow.objects.get(clinic=clinic_a)
        assert flow.status == FlowStatus.ACTIVE
        assert flow.current_version.published_at is not None

    def test_rodar_duas_vezes_versiona_em_vez_de_duplicar(self, clinic_a):
        from django.core.management import call_command

        call_command("seed_flow_demo", clinic=clinic_a.pk)
        call_command("seed_flow_demo", clinic=clinic_a.pk)

        assert Flow.objects.filter(clinic=clinic_a).count() == 1
        assert FlowVersion.objects.count() == 2

    def test_o_fluxo_semeado_nao_tem_nenhum_no_de_ia_nem_http(self, clinic_a):
        """
        P14 e P15. Comparar com o próprio `FlowNodeType` não serviria: se um
        dia alguém acrescentar `llm_agent` ao enum e usar aqui, o teste
        passaria. Os nomes proibidos vão LITERAIS.
        """
        from django.core.management import call_command

        call_command("seed_flow_demo", clinic=clinic_a.pk)

        grafo = Flow.objects.get(clinic=clinic_a).current_version.graph
        tipos = {n["type"] for n in grafo["nodes"]}
        assert not tipos & {"llm_agent", "transcribe_audio", "http_request", "message_router"}

    def test_o_fluxo_de_agendamento_roda_de_ponta_a_ponta(self, clinic_a):
        """
        O caminho que o cliente vai ver: "oi" → menu → especialidade →
        horário → recepção. Passa pelo pipeline inteiro (payload no formato
        Meta → parse → ingestão → motor), não por chamada direta ao motor.
        """
        from django.core.management import call_command

        from apps.automation.models import FlowRun
        from apps.inbox.choices import AttendedBy, ConversationStatus, SenderKind
        from apps.inbox.models import Message
        from apps.inbox.services import ingest_events
        from apps.integrations.whatsapp.events import parse_meta_webhook
        from apps.integrations.whatsapp.fake.adapter import build_inbound_payload

        call_command("seed_flow_demo", clinic=clinic_a.pk, ativar=True)
        inbox = make_inbox(clinic_a, wa_id="5500999990001")
        conversa = inbox["conversation"]

        def paciente_diz(texto, reply_id=""):
            ingest_events(
                inbox["channel"],
                parse_meta_webhook(
                    build_inbound_payload(
                        wa_id=inbox["contact"].wa_id, body=texto, reply_id=reply_id
                    )
                ),
            )

        paciente_diz("oi")
        paciente_diz("Marcar consulta", reply_id="agendar")
        paciente_diz("Cardiologia", reply_id="cardio")
        paciente_diz("Amanhã de manhã", reply_id="manha")

        conversa.refresh_from_db()
        run = FlowRun.objects.get(conversation=conversa)
        falas = list(
            Message.objects.filter(conversation=conversa, sender_kind=SenderKind.BOT)
            .exclude(kind="activity")
            .values_list("body", flat=True)
        )

        assert len(falas) == 5  # saudação, menu, especialidade, horário, confirmação
        assert run.status == FlowRunStatus.HANDED_OFF
        # E o mais importante: a conversa VOLTA para a recepção, sem dono.
        assert conversa.attended_by == AttendedBy.NONE
        assert conversa.status == ConversationStatus.WAITING
        assert list(conversa.labels.values_list("name", flat=True)) == ["Agendamento"]

    def test_o_fluxo_semeado_marca_etiqueta_de_conversa_e_nunca_tag(self, clinic_a):
        """
        RF-FLW-13.1: a `patients.Tag` sincroniza com a vSaúde, e "Agendamento"
        viraria tag no prontuário do paciente.
        """
        from django.core.management import call_command

        from apps.inbox.models import ConversationLabel
        from apps.patients.models import Tag

        call_command("seed_flow_demo", clinic=clinic_a.pk)

        assert ConversationLabel.objects.filter(clinic=clinic_a, name="Agendamento").exists()
        assert not Tag.objects.filter(name="Agendamento").exists()


class TestFluxoNovoNasceUsavel:
    """
    O fluxo criado pela tela tem que abrir com por onde começar.

    Nascia com o grafo vazio, e o início não está no cardápio de criar passo
    (é um por fluxo). O gestor abria a tela em branco e não tinha saída: no
    banco sobrou um fluxo com zero passos, que foi como o defeito apareceu.
    """

    def test_nasce_com_o_no_de_inicio(self, gestor):
        resposta = gestor.post(FLOWS_URL, {"name": "Do zero"}, format="json")

        grafo = Flow.objects.get(pk=resposta.data["id"]).current_version.graph
        assert [n["type"] for n in grafo["nodes"]] == [FlowNodeType.START]

    def test_o_inicio_ja_e_o_ponto_de_entrada(self, gestor):
        resposta = gestor.post(FLOWS_URL, {"name": "Do zero"}, format="json")

        grafo = Flow.objects.get(pk=resposta.data["id"]).current_version.graph
        assert grafo["entry_node"] == grafo["nodes"][0]["id"]

    def test_ainda_nao_pode_publicar_so_com_o_inicio(self, gestor):
        """Ter por onde começar não é ter fluxo: falta ligar o primeiro passo."""
        resposta = gestor.post(FLOWS_URL, {"name": "Do zero"}, format="json")

        assert resposta.data["can_activate"] is False

    def test_o_desenho_mandado_pela_tela_prevalece(self, gestor):
        """Quem manda grafo no POST não recebe o início de fábrica em cima."""
        resposta = gestor.post(
            FLOWS_URL, {"name": "Com desenho", "graph": GRAFO_BOM}, format="json"
        )

        grafo = Flow.objects.get(pk=resposta.data["id"]).current_version.graph
        assert len(grafo["nodes"]) == 3
