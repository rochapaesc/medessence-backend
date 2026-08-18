"""
Os dois atalhos que a tela usa: o aviso simples (RF-SEQ-1.2) e os modelos de
trilha (RF-SEQ-12).

Ambos existem para pagar o custo de uma decisão de desenho. O passo só dispara
FLUXO, então um lembrete precisaria de um fluxo montado à mão; e uma trilha
nasce vazia, então a tela em branco não ensina o que é uma sequência.
"""

from datetime import time

import pytest

from apps.automation.choices import FlowNodeType, FlowStatus
from apps.automation.modelos import catalogo, criar_fluxo_de_aviso
from apps.automation.models import Flow, Sequence, SequenceStep
from apps.automation.tests.conftest import make_flow
from apps.inbox.models import WhatsAppTemplate

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/"
PASSOS = "/api/v1/sequence-steps/"


@pytest.fixture
def modelo_aprovado(clinic_a):
    return WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="convite_retorno",
        category="UTILITY",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Olá, {{1}}! Faz tempo desde a sua consulta."}],
    )


@pytest.fixture
def trilha(clinic_a):
    return Sequence.objects.create(clinic=clinic_a, name="Pós-consulta")


# ---- o aviso simples ----


def test_aviso_cria_e_publica_um_fluxo_de_um_no(
    api_client, manager_single_clinic, clinic_a, trilha, modelo_aprovado
):
    """
    ⚠️ É o que paga o custo de "o passo só dispara fluxo": sem isto a clínica
    abriria o canvas para cada lembrete simples.
    """
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        PASSOS,
        {
            "sequence": trilha.pk,
            "name": "Convite de retorno",
            "offset_days": 55,
            "send_time": "09:00",
            "aviso": {
                "template": "convite_retorno",
                "variables": {"1": {"source": "patient_first_name"}},
            },
        },
        format="json",
    )

    assert resposta.status_code == 201, resposta.data
    passo = SequenceStep.objects.get(pk=resposta.data["id"])
    assert passo.flow.status == FlowStatus.ACTIVE
    assert passo.flow.current_version is not None
    assert passo.flow.name == "Pós-consulta: Convite de retorno"

    # Três nós porque o validador cobra início e fim, mas só UM fala: para
    # quem monta continua sendo "um aviso".
    grafo = passo.flow.current_version.graph
    tipos = [n["type"] for n in grafo["nodes"]]
    assert tipos == [FlowNodeType.START, FlowNodeType.SEND_TEMPLATE, FlowNodeType.END]
    falante = grafo["nodes"][1]
    assert falante["config"]["template_name"] == "convite_retorno"


def test_editar_o_aviso_reusa_o_mesmo_fluxo(
    api_client, manager_single_clinic, clinic_a, trilha, modelo_aprovado
):
    """
    Criar um fluxo novo a cada gravação deixaria um rastro de fluxos órfãos
    publicados, cada um com o nome antigo.
    """
    api_client.force_authenticate(manager_single_clinic)
    payload = {
        "sequence": trilha.pk,
        "name": "Convite",
        "offset_days": 55,
        "send_time": "09:00",
        "aviso": {
            "template": "convite_retorno",
            "variables": {"1": {"source": "patient_first_name"}},
        },
    }
    criado = api_client.post(PASSOS, payload, format="json")
    fluxo_original = SequenceStep.objects.get(pk=criado.data["id"]).flow_id
    antes = Flow.objects.count()

    api_client.patch(
        f"{PASSOS}{criado.data['id']}/",
        {"name": "Convite de volta", **{"aviso": payload["aviso"]}},
        format="json",
    )

    passo = SequenceStep.objects.get(pk=criado.data["id"])
    assert passo.flow_id == fluxo_original
    assert Flow.objects.count() == antes
    assert passo.flow.name == "Pós-consulta: Convite de volta"
    assert passo.flow.versions.count() == 2, "editar versiona, não sobrescreve"


def test_aviso_com_variavel_sem_fonte_e_recusado_com_o_motivo(
    api_client, manager_single_clinic, clinic_a, trilha, modelo_aprovado
):
    """
    Template com parâmetro faltando é recusado pela Meta NA HORA DO ENVIO, com
    o paciente do outro lado. O lugar de barrar é aqui.
    """
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        PASSOS,
        {
            "sequence": trilha.pk,
            "name": "Convite",
            "offset_days": 55,
            "send_time": "09:00",
            "aviso": {"template": "convite_retorno", "variables": {}},
        },
        format="json",
    )

    assert resposta.status_code == 400
    assert "aviso" in resposta.data


def test_aviso_com_template_nao_aprovado_e_recusado(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        PASSOS,
        {
            "sequence": trilha.pk,
            "name": "Convite",
            "offset_days": 55,
            "send_time": "09:00",
            "aviso": {"template": "nao_existe", "variables": {}},
        },
        format="json",
    )
    assert resposta.status_code == 400


def test_passo_com_fluxo_pronto_continua_funcionando(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """O atalho é uma alternativa, não uma substituição."""
    flow = make_flow(clinic_a, name="Fluxo montado à mão", status=FlowStatus.ACTIVE)
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        PASSOS,
        {
            "sequence": trilha.pk,
            "name": "Com fluxo",
            "offset_days": 1,
            "send_time": "09:00",
            "flow": flow.pk,
        },
        format="json",
    )

    assert resposta.status_code == 201
    assert SequenceStep.objects.get(pk=resposta.data["id"]).flow_id == flow.pk


def test_o_servico_recusa_sem_passar_pela_api(clinic_a, modelo_aprovado):
    """A validação é do serviço, não do viewset: quem chamar por dentro paga igual."""
    with pytest.raises(ValueError):
        criar_fluxo_de_aviso(
            clinic_a, nome="Aviso", template_name="convite_retorno", variables={}
        )


# ---- os modelos de trilha ----


def test_catalogo_traz_a_forma_e_nao_a_mensagem():
    """
    ⚠️ O modelo NÃO inventa texto: ele depende de template aprovado na conta da
    clínica. Prometer mensagem pronta criaria passo que nunca sai.
    """
    modelos = catalogo()
    assert {m["slug"] for m in modelos} >= {"pos_consulta", "resgate"}
    for m in modelos:
        assert m["passos"], "modelo sem passo não ensina nada"
        for passo in m["passos"]:
            assert set(passo) == {"nome", "offset_days", "send_time"}
            assert "texto" not in passo


def test_criar_a_partir_de_um_modelo_traz_os_passos(
    api_client, manager_single_clinic, clinic_a
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        URL,
        {"name": "Resgate de inativos", "is_marketing": True, "template": "resgate"},
        format="json",
    )

    assert resposta.status_code == 201
    trilha = Sequence.objects.get(pk=resposta.data["id"])
    assert trilha.steps.count() == 3
    # Na ordem do relógio, e cada um com um fluxo em RASCUNHO que já vem com a
    # MENSAGEM do modelo escrita (18/08): modelo que entregava esqueleto sem
    # carne não validava nada, e rascunho já dá para testar (RF-FLW-25).
    passos = list(trilha.steps.order_by("offset_days"))
    assert [p.offset_days for p in passos] == [0, 7, 21]
    # ⚠️ TEMPLATE, nunca texto (correção do usuário em 18/08): o público de
    # campanha está fora da janela de 24h, e texto livre ali fica segurado
    # para sempre. O texto do modelo vira sugestão de corpo.
    for passo in passos:
        assert passo.flow.status == "draft"
        grafo = passo.flow.current_version.graph
        fala = next(n for n in grafo["nodes"] if n["type"] == "send_template")
        assert fala["config"]["template_name"] == "", "template é escolha da clínica"
        assert fala["config"]["suggested_body"], "a sugestão de corpo fica"


def test_trilha_nasce_sempre_desligada(api_client, manager_single_clinic, clinic_a):
    """
    RF-SEQ-12: ligar é decisão de quem gerencia. Uma trilha que começa a falar
    no instante em que foi criada não deu a ninguém a chance de conferir.
    """
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        URL, {"name": "Tenta nascer ligada", "is_active": True}, format="json"
    )

    assert resposta.status_code == 201
    assert Sequence.objects.get(pk=resposta.data["id"]).is_active is False


def test_criar_do_zero_nao_cria_passo(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, {"name": "Do zero"}, format="json")

    assert resposta.status_code == 201
    assert Sequence.objects.get(pk=resposta.data["id"]).steps.count() == 0


def test_modelo_desconhecido_nao_quebra_a_criacao(
    api_client, manager_single_clinic, clinic_a
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        URL, {"name": "Slug errado", "template": "nao_existe"}, format="json"
    )

    assert resposta.status_code == 201
    assert Sequence.objects.get(pk=resposta.data["id"]).steps.count() == 0


def test_atendente_nao_cria_passo(api_client, attendant_a, clinic_a, trilha):
    """Montar é do gestor (RF-SEQ-10)."""
    flow = make_flow(clinic_a, name="F", status=FlowStatus.ACTIVE)
    api_client.force_authenticate(attendant_a)
    resposta = api_client.post(
        PASSOS,
        {
            "sequence": trilha.pk,
            "name": "Não deveria",
            "offset_days": 1,
            "send_time": time(9, 0).isoformat(),
            "flow": flow.pk,
        },
        format="json",
    )
    assert resposta.status_code == 403


# ---- apagar (18/08): as guardas que faltavam ----


def test_apagar_passo_reaponta_quem_estava_parado_nele(
    api_client, manager_single_clinic, clinic_a
):
    """
    RF-SEQ-2.3: sem o reaponte, a inscrição fica presa num passo morto que o
    FK ainda resolve, e a varredura dispararia um passo que a clínica apagou.
    """
    from apps.automation.choices import EnrollmentSource, FlowStatus
    from apps.automation.models import SequenceEnrollment
    from apps.automation.sequences import inscrever
    from apps.automation.tests.conftest import make_contact, make_flow

    sequence = Sequence.objects.create(clinic=clinic_a, name="Trilha", is_active=True)
    primeiro = SequenceStep.objects.create(
        sequence=sequence, order=1, name="Primeiro", offset_days=0,
        send_time=time(9, 0),
        flow=make_flow(clinic_a, name="F1", status=FlowStatus.ACTIVE),
    )
    segundo = SequenceStep.objects.create(
        sequence=sequence, order=2, name="Segundo", offset_days=7,
        send_time=time(9, 0),
        flow=make_flow(clinic_a, name="F2", status=FlowStatus.ACTIVE),
    )
    contato = make_contact(clinic_a, wa_id="5585933330001")
    enrollment = inscrever(sequence, contato, source=EnrollmentSource.FLOW_NODE)
    assert enrollment.current_step_id == primeiro.pk

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.delete(f"/api/v1/sequence-steps/{primeiro.pk}/")

    assert response.status_code in (200, 204)
    enrollment.refresh_from_db()
    assert enrollment.current_step_id == segundo.pk


def test_apagar_o_ultimo_passo_conclui_quem_estava_nele(
    api_client, manager_single_clinic, clinic_a
):
    from apps.automation.choices import (
        EnrollmentSource,
        FlowStatus,
        SequenceEnrollmentStatus,
    )
    from apps.automation.sequences import inscrever
    from apps.automation.tests.conftest import make_contact, make_flow

    sequence = Sequence.objects.create(clinic=clinic_a, name="Curta", is_active=True)
    unico = SequenceStep.objects.create(
        sequence=sequence, order=1, name="Único", offset_days=0,
        send_time=time(9, 0),
        flow=make_flow(clinic_a, name="F", status=FlowStatus.ACTIVE),
    )
    contato = make_contact(clinic_a, wa_id="5585933330002")
    enrollment = inscrever(sequence, contato, source=EnrollmentSource.FLOW_NODE)

    api_client.force_authenticate(manager_single_clinic)
    api_client.delete(f"/api/v1/sequence-steps/{unico.pk}/")

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.COMPLETED


def test_fluxo_publicado_nao_se_apaga(api_client, manager_single_clinic, clinic_a):
    from apps.automation.choices import FlowStatus
    from apps.automation.tests.conftest import make_flow

    flow = make_flow(clinic_a, name="No ar", status=FlowStatus.ACTIVE)
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.delete(f"/api/v1/flows/{flow.pk}/")

    assert response.status_code == 400
    assert "Despublique" in str(response.data)
    flow.refresh_from_db()
    assert flow.deleted_at is None


def test_fluxo_que_e_passo_de_sequencia_diz_qual(
    api_client, manager_single_clinic, clinic_a
):
    """A mensagem de banco não diz à clínica QUAL sequência segura o fluxo."""
    from apps.automation.tests.conftest import make_flow

    flow = make_flow(clinic_a, name="Preso")
    sequence = Sequence.objects.create(clinic=clinic_a, name="Dona do passo")
    SequenceStep.objects.create(
        sequence=sequence, order=1, offset_days=0, send_time=time(9, 0), flow=flow
    )
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.delete(f"/api/v1/flows/{flow.pk}/")

    assert response.status_code == 400
    assert "Dona do passo" in str(response.data)


def test_fluxo_rascunho_sem_donos_se_apaga(api_client, manager_single_clinic, clinic_a):
    from apps.automation.tests.conftest import make_flow

    flow = make_flow(clinic_a, name="Solto")
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.delete(f"/api/v1/flows/{flow.pk}/")

    assert response.status_code in (200, 204)


def test_o_passo_diz_se_abre_com_modelo(api_client, manager_single_clinic, clinic_a):
    """
    RF-SEQ-5.3 na tela: onde a campanha nasce, cada passo responde se alcança
    quem está fora da janela. Sem isso a clínica inscreve 1.000 e descobre no
    painel que todo mundo ficou segurado.
    """
    from apps.automation.choices import FlowStatus
    from apps.automation.tests.conftest import make_flow

    com_modelo = make_flow(
        clinic_a,
        name="Abre com modelo",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {"id": "n1", "type": "start", "label": "n1", "config": {}},
                {
                    "id": "n2",
                    "type": "send_template",
                    "label": "n2",
                    "config": {"template_name": "retorno", "variables": {}},
                },
                {"id": "n3", "type": "end", "label": "n3", "config": {}},
            ],
            "edges": [
                {"from": "n1", "to": "n2", "condition": "default"},
                {"from": "n2", "to": "n3", "condition": "default"},
            ],
        },
    )
    # ⚠️ Grafo explícito: o make_flow padrão vem VAZIO, e fluxo que não fala
    # não esbarra na janela (conta como True). O caso que importa é abrir a
    # boca com texto.
    com_texto = make_flow(
        clinic_a,
        name="Abre com texto",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {"id": "n1", "type": "start", "label": "n1", "config": {}},
                {
                    "id": "n2",
                    "type": "send_message",
                    "label": "n2",
                    "config": {"text": "oi"},
                },
                {"id": "n3", "type": "end", "label": "n3", "config": {}},
            ],
            "edges": [
                {"from": "n1", "to": "n2", "condition": "default"},
                {"from": "n2", "to": "n3", "condition": "default"},
            ],
        },
    )

    sequence = Sequence.objects.create(clinic=clinic_a, name="Mista")
    SequenceStep.objects.create(
        sequence=sequence, order=1, offset_days=0, send_time=time(9, 0), flow=com_modelo
    )
    SequenceStep.objects.create(
        sequence=sequence, order=2, offset_days=7, send_time=time(9, 0), flow=com_texto
    )

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(f"{URL}{sequence.pk}/")

    por_nome = {p["flow_name"]: p["abre_com_modelo"] for p in resposta.data["steps"]}
    assert por_nome["Abre com modelo"] is True
    assert por_nome["Abre com texto"] is False
