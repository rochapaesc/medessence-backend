"""
O modo de teste do fluxo (RF-FLW-25).

O que estes testes protegem é a promessa central: o teste roda o MOTOR de
verdade (gatilho, coleta, repetição, guardas), e a conversa de teste não
existe para o resto do sistema (Inbox, contadores, sequências).
"""

import pytest
from django.utils import timezone

from apps.automation import teste as modo_teste
from apps.automation.choices import (
    EnrollmentSource,
    FlowNodeType,
    FlowRunStatus,
    FlowStatus,
    FlowTrigger,
)
from apps.automation.models import Flow, FlowRun, FlowVersion, Sequence, SequenceEnrollment, SequenceStep
from apps.automation.tests.conftest import make_contact
from apps.inbox.models import Channel, Conversation, Message

pytestmark = pytest.mark.django_db

URL = "/api/v1/flows/"


def _no(node_id, tipo, config=None, label=""):
    return {"id": node_id, "type": tipo, "label": label or node_id, "config": config or {}}


def _edge(source, target, condition="default"):
    return {"from": source, "to": target, "condition": condition}


def _fluxo(clinic, *, trigger=FlowTrigger.KEYWORD, keywords=("agenda",), graph=None, nome="Teste"):
    flow = Flow.objects.create(
        clinic=clinic,
        name=nome,
        status=FlowStatus.DRAFT,
        trigger=trigger,
        trigger_config={"keywords": list(keywords)} if trigger == FlowTrigger.KEYWORD else {},
    )
    graph = graph or {
        "entry_node": "start",
        "nodes": [
            _no("start", FlowNodeType.START),
            _no("oi", FlowNodeType.SEND_MESSAGE, {"text": "Olá! Qual é o seu convênio?"}),
            _no("coleta", FlowNodeType.COLLECT_INPUT, {"prompt_text": "Me diga o convênio.", "var_key": "convenio"}),
            _no("fim", FlowNodeType.END),
        ],
        "edges": [
            _edge("start", "oi"),
            _edge("oi", "coleta"),
            _edge("coleta", "fim"),
        ],
    }
    version = FlowVersion.objects.create(flow=flow, number=1, graph=graph)
    flow.current_version = version
    flow.save(update_fields=["current_version"])
    return flow


# ---- o motor de verdade ----


def test_palavra_chave_errada_nao_comeca_e_diz_por_que(clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)

    retrato = modo_teste.falar_no_teste(flow, texto="bom dia")

    assert retrato["situacao"] == "esperando_comecar"
    assert any("agenda" in n for n in retrato["notas"])
    assert FlowRun.objects.count() == 0


def test_palavra_certa_comeca_e_o_robo_fala(clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)

    retrato = modo_teste.falar_no_teste(flow, texto="quero agenda por favor")

    falas = [l for l in retrato["linhas"] if l["tipo"] == "mensagem" and l["quem"] == "robo"]
    assert any("convênio" in f["texto"] for f in falas)
    run = FlowRun.objects.get()
    assert run.is_test is True
    assert run.status == FlowRunStatus.ACTIVE


def test_coleta_guarda_e_o_retrato_conta(clinic_a):
    """A resposta vira variável E vira linha de evento (RF-FLW-25.3)."""
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)
    modo_teste.falar_no_teste(flow, texto="agenda")

    retrato = modo_teste.falar_no_teste(flow, texto="Unimed")

    assert retrato["variaveis"] == {"convenio": "Unimed"}
    eventos = [l for l in retrato["linhas"] if l["tipo"] == "evento"]
    assert any(e["evento"] == "var_saved" and e["dados"]["valor"] == "Unimed" for e in eventos)
    assert retrato["situacao"] == "terminou"


def test_fluxo_manual_so_comeca_pelo_botao(clinic_a):
    flow = _fluxo(clinic_a, trigger=FlowTrigger.MANUAL)
    modo_teste.iniciar_teste(flow)

    sem_botao = modo_teste.falar_no_teste(flow, texto="oi")
    assert sem_botao["situacao"] == "esperando_comecar"
    assert any("manual" in n for n in sem_botao["notas"])

    com_botao = modo_teste.comecar_manual(flow)
    assert com_botao["situacao"] != "esperando_comecar"
    assert FlowRun.objects.filter(is_test=True).count() == 1


def test_desenho_que_nao_anda_bloqueia_com_a_lista(clinic_a):
    """Início solto barra o teste; a lista é a mesma da ativação (RF-FLW-25.4)."""
    flow = _fluxo(
        clinic_a,
        graph={
            "entry_node": "start",
            "nodes": [_no("start", FlowNodeType.START), _no("solto", FlowNodeType.SEND_MESSAGE, {"text": "oi"})],
            "edges": [],
        },
    )

    retrato = modo_teste.iniciar_teste(flow)

    assert retrato["situacao"] == "bloqueado"
    assert retrato["problemas"]


def test_recomecar_zera_a_conversa(clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)
    modo_teste.falar_no_teste(flow, texto="agenda")
    assert Message.objects.filter(conversation__channel__is_test=True).exists()

    retrato = modo_teste.iniciar_teste(flow)

    assert retrato["linhas"] == []
    assert retrato["situacao"] == "esperando_comecar"
    assert not FlowRun.objects.filter(is_test=True, status=FlowRunStatus.ACTIVE).exists()


# ---- a conversa de teste não existe para o resto ----


def test_sequencia_e_anunciada_e_nao_executada(clinic_a):
    """RF-FLW-25.4: nada de gente de teste no painel da sequência real."""
    from apps.automation.tests.conftest import make_flow

    trilha = Sequence.objects.create(clinic=clinic_a, name="Resgate", is_active=True)
    SequenceStep.objects.create(
        sequence=trilha,
        order=1,
        offset_days=0,
        send_time=timezone.now().time(),
        flow=make_flow(clinic_a, name="Passo", status=FlowStatus.ACTIVE),
    )
    flow = _fluxo(
        clinic_a,
        graph={
            "entry_node": "start",
            "nodes": [
                _no("start", FlowNodeType.START),
                _no("poe", FlowNodeType.ENROLL_SEQUENCE, {"sequence_id": trilha.pk}),
                _no("fim", FlowNodeType.END),
            ],
            "edges": [_edge("start", "poe"), _edge("poe", "fim")],
        },
    )
    modo_teste.iniciar_teste(flow)

    retrato = modo_teste.falar_no_teste(flow, texto="agenda")

    assert SequenceEnrollment.objects.count() == 0
    eventos = [l for l in retrato["linhas"] if l["tipo"] == "evento"]
    anuncio = next(e for e in eventos if e["evento"] == "sequence_applied")
    assert anuncio["dados"]["anunciado"] is True
    assert anuncio["dados"]["sequence"] == "Resgate"


def test_no_fluxo_de_verdade_a_sequencia_executa(clinic_a):
    """A guarda é do TESTE: produção continua inscrevendo."""
    from apps.automation.engine import start_run
    from apps.automation.tests.conftest import make_conversation, make_flow

    trilha = Sequence.objects.create(clinic=clinic_a, name="Resgate", is_active=True)
    SequenceStep.objects.create(
        sequence=trilha,
        order=1,
        offset_days=0,
        send_time=timezone.now().time(),
        flow=make_flow(clinic_a, name="Passo", status=FlowStatus.ACTIVE),
    )
    flow = _fluxo(
        clinic_a,
        graph={
            "entry_node": "start",
            "nodes": [
                _no("start", FlowNodeType.START),
                _no("poe", FlowNodeType.ENROLL_SEQUENCE, {"sequence_id": trilha.pk}),
                _no("fim", FlowNodeType.END),
            ],
            "edges": [_edge("start", "poe"), _edge("poe", "fim")],
        },
    )
    conversa = make_conversation(clinic_a, make_contact(clinic_a, wa_id="5585911110001"))

    start_run(flow, conversa)

    assert SequenceEnrollment.objects.count() == 1


def test_conversa_de_teste_fora_do_inbox(api_client, manager_single_clinic, clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)
    modo_teste.falar_no_teste(flow, texto="agenda")

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get("/api/v1/conversations/")

    ids = [c["id"] for c in response.data.get("results", response.data)]
    de_teste = Conversation.objects.filter(channel__is_test=True).values_list("pk", flat=True)
    assert not set(ids) & set(de_teste)


def test_execucao_de_teste_fora_do_contador(api_client, manager_single_clinic, clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)
    modo_teste.falar_no_teste(flow, texto="agenda")
    assert FlowRun.objects.filter(is_test=True, status=FlowRunStatus.ACTIVE).exists()

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL)
    linha = next(f for f in response.data["results"] if f["id"] == flow.pk)

    assert linha["runs_active"] == 0


def test_disparo_de_sequencia_nunca_usa_o_canal_de_teste(clinic_a):
    """`conversa_para_disparo` com só o canal de teste na clínica recusa."""
    from apps.inbox.services import ConversaSemDestino, conversa_para_disparo

    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)  # cria o canal de teste
    assert Channel.objects.filter(clinic=clinic_a).count() == 1

    contato = make_contact(clinic_a, wa_id="5585911110002")
    with pytest.raises(ConversaSemDestino):
        conversa_para_disparo(clinic_a, contato)


def test_encerrar_apaga_o_rastro(clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)
    modo_teste.falar_no_teste(flow, texto="agenda")

    modo_teste.encerrar_teste(flow)

    assert not Message.objects.filter(conversation__channel__is_test=True).exists()
    assert not FlowRun.objects.filter(is_test=True, status=FlowRunStatus.ACTIVE).exists()


# ---- a API ----


def test_api_abre_fala_e_encerra(api_client, manager_single_clinic, clinic_a):
    flow = _fluxo(clinic_a)
    api_client.force_authenticate(manager_single_clinic)

    aberto = api_client.post(f"{URL}{flow.pk}/teste/")
    assert aberto.status_code == 200
    assert aberto.data["situacao"] == "esperando_comecar"
    assert aberto.data["gatilho"]["palavras"] == ["agenda"]

    falou = api_client.post(f"{URL}{flow.pk}/teste/falar/", {"texto": "agenda"}, format="json")
    assert falou.status_code == 200
    assert any(l["quem"] == "robo" for l in falou.data["linhas"] if l["tipo"] == "mensagem")

    encerrado = api_client.delete(f"{URL}{flow.pk}/teste/")
    assert encerrado.status_code == 200


def test_api_recusa_fala_vazia(api_client, manager_single_clinic, clinic_a):
    flow = _fluxo(clinic_a)
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(f"{URL}{flow.pk}/teste/")

    response = api_client.post(f"{URL}{flow.pk}/teste/falar/", {}, format="json")

    assert response.status_code == 400


def test_atendente_nao_testa(api_client, attendant_a, clinic_a):
    """Testar é montar: gestor. O atendente nem enxerga fluxos."""
    flow = _fluxo(clinic_a)
    api_client.force_authenticate(attendant_a)
    response = api_client.post(f"{URL}{flow.pk}/teste/")
    assert response.status_code in (403, 404)


def test_espera_vira_relogio_e_pular_avanca(clinic_a):
    """O nó Aguardar não pode travar o teste por 2 dias (RF-FLW-25.3)."""
    flow = _fluxo(
        clinic_a,
        graph={
            "entry_node": "start",
            "nodes": [
                _no("start", FlowNodeType.START),
                _no("oi", FlowNodeType.SEND_MESSAGE, {"text": "Já volto."}),
                _no("espera", FlowNodeType.WAIT, {"amount": 2, "unit": "days"}),
                _no("depois", FlowNodeType.SEND_MESSAGE, {"text": "Voltei!"}),
                _no("fim", FlowNodeType.END),
            ],
            "edges": [
                _edge("start", "oi"),
                _edge("oi", "espera"),
                _edge("espera", "depois"),
                _edge("depois", "fim"),
            ],
        },
    )
    modo_teste.iniciar_teste(flow)

    esperando = modo_teste.falar_no_teste(flow, texto="agenda")
    assert esperando["situacao"] == "esperando_o_relogio"
    assert esperando["espera"] is not None

    depois = modo_teste.pular_espera(flow)
    falas = [l for l in depois["linhas"] if l["tipo"] == "mensagem" and l["quem"] == "robo"]
    assert any("Voltei!" in f["texto"] for f in falas)
    assert depois["situacao"] == "terminou"


def test_pular_sem_espera_explica(clinic_a):
    flow = _fluxo(clinic_a)
    modo_teste.iniciar_teste(flow)
    retrato = modo_teste.pular_espera(flow)
    assert any("Não há espera" in n for n in retrato["notas"])


def test_botoes_do_robo_viram_opcoes_clicaveis(clinic_a):
    """O retrato carrega as opções para o painel clicar, e o clique avança."""
    flow = _fluxo(
        clinic_a,
        graph={
            "entry_node": "start",
            "nodes": [
                _no("start", FlowNodeType.START),
                _no(
                    "menu",
                    FlowNodeType.SEND_BUTTONS,
                    {
                        "text": "Qual convênio?",
                        "buttons": [
                            {"id": "unimed", "title": "Unimed"},
                            {"id": "outro", "title": "Outro"},
                        ],
                        "var_key": "convenio",
                    },
                ),
                _no("fim", FlowNodeType.END),
            ],
            "edges": [
                _edge("start", "menu"),
                _edge("menu", "fim", "button:unimed"),
                _edge("menu", "fim", "button:outro"),
            ],
        },
    )
    modo_teste.iniciar_teste(flow)

    retrato = modo_teste.falar_no_teste(flow, texto="agenda")
    menu = next(
        l for l in retrato["linhas"] if l["tipo"] == "mensagem" and l["opcoes"]
    )
    assert {o["id"] for o in menu["opcoes"]} == {"unimed", "outro"}

    clicou = modo_teste.falar_no_teste(flow, texto="Unimed", interactive_id="unimed")
    assert clicou["variaveis"] == {"convenio": "Unimed"}
    assert clicou["situacao"] == "terminou"
