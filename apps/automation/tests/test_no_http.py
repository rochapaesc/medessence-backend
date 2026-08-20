"""
O nó "Chamar sistema externo" e a cerca dele (RF-FLW-16, RF-FLW-16.1).

⚠️ Estes testes são a razão de o nó existir. Ele ficou adiado pela P15 desde o
desenho do catálogo, e o que destravou foi a cerca — então cada item dela tem
um teste que FALHA se alguém a afrouxar. Sem isso o nó volta a ser o que a
pendência descrevia: dado de paciente indo para qualquer endereço, e o nosso
servidor alcançando a rede interna por procuração.

O caso que mais engana é o do redirecionamento: a URL cadastrada passa em toda
checagem, e o `302` leva para a metadata da nuvem. Quem esquece
`follow_redirects=False` tem uma cerca que parece completa e não é.
"""

import httpx
import pytest
from django.core.exceptions import ValidationError

from apps.automation.choices import EDGE_FALSE, EDGE_TRUE, FlowNodeType
from apps.automation.graph import Node
from apps.automation.models import HttpDestination
from apps.automation.tests.conftest import make_contact, make_conversation
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.core.ssrf import BlockedDestination, check_public_url, is_public_address

pytestmark = pytest.mark.django_db


@pytest.fixture
def conversa(clinic_a):
    return make_conversation(clinic_a, make_contact(clinic_a))


@pytest.fixture
def destino(clinic_a):
    return HttpDestination.objects.create(
        clinic=clinic_a, name="ERP da recepção", url="https://erp.exemplo.com/hook"
    )


class TestCercaDeRede:
    """RF-FLW-16.1 itens b e c."""

    @pytest.mark.parametrize(
        "endereco",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            # ⚠️ A metadata da nuvem. É o alvo clássico de SSRF: devolve
            # credencial da máquina para quem perguntar, sem autenticação.
            "169.254.169.254",
            # ⚠️ CGNAT. `is_private` do Python devolve FALSE para esta faixa,
            # então quem usar aquele atributo em vez de `is_global` deixa a
            # faixa inteira passar. A regex da referência tem o mesmo furo.
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fc00::1",
            # IPv4 escondido dentro de IPv6.
            "::ffff:127.0.0.1",
        ],
    )
    def test_endereco_interno_nao_e_publico(self, endereco):
        assert is_public_address(endereco) is False

    @pytest.mark.parametrize("endereco", ["8.8.8.8", "1.1.1.1", "2606:4700::1"])
    def test_endereco_da_internet_passa(self, endereco):
        assert is_public_address(endereco) is True

    def test_http_puro_e_recusado(self):
        with pytest.raises(BlockedDestination, match="https"):
            check_public_url("http://exemplo.com/hook")

    def test_usuario_e_senha_na_url_sao_recusados(self):
        # Credencial em URL vaza em log de proxy e em histórico. E `@` na URL
        # é o truque velho de fazer o host parecer outro.
        with pytest.raises(BlockedDestination, match="usuário e senha"):
            check_public_url("https://alguem:segredo@exemplo.com/hook")

    def test_porta_fora_do_padrao_e_recusada(self):
        # Liberar porta arbitrária transforma o nó num scanner da rede.
        with pytest.raises(BlockedDestination, match="porta"):
            check_public_url("https://exemplo.com:8080/hook")

    @pytest.mark.parametrize(
        "url",
        [
            "https://localhost/x",
            "https://qualquer.local/x",
            "https://metadata.internal/x",
            "https://10.0.0.1/x",
            "https://169.254.169.254/latest/meta-data/",
        ],
    )
    def test_nome_e_ip_da_rede_interna_sao_recusados(self, url):
        with pytest.raises(BlockedDestination):
            check_public_url(url)


class TestCadastroDoDestino:
    """RF-FLW-16.1 item a: a URL não se digita no nó, e o cadastro cobra."""

    def test_cadastro_com_endereco_interno_nao_salva(self, clinic_a):
        destino = HttpDestination(
            clinic=clinic_a, name="Interno", url="https://192.168.0.5/hook"
        )
        with pytest.raises(ValidationError) as erro:
            destino.full_clean()
        assert "url" in erro.value.message_dict


class TestChamada:
    def _no(self, **config):
        return Node(
            id="n1", type=FlowNodeType.HTTP_REQUEST, label="Avisar o ERP", config=config
        )

    def test_sucesso_sai_pela_saida_de_sucesso(self, monkeypatch, destino, conversa, flow_a):
        from apps.automation import http_call

        enviado = {}

        class _Resposta:
            status_code = 200

            def read(self):
                return b'{"protocolo": "AB-1"}'

        class _Cliente:
            def __init__(self, **kwargs):
                enviado["kwargs"] = kwargs

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None, headers=None):
                enviado["url"] = url
                enviado["json"] = json
                enviado["headers"] = headers
                return _Resposta()

        monkeypatch.setattr(http_call.httpx, "Client", _Cliente)
        monkeypatch.setattr(http_call, "check_public_url", lambda u: ["1.2.3.4"])

        run = _run(flow_a, conversa, vars={"nome": "Ana", "cpf": "000"})
        no = self._no(destination_id=destino.pk, send=["nome"], save={"protocolo": "codigo"})

        assert http_call.chamar(no, run, conversa) is True
        assert enviado["url"] == destino.url
        # ⚠️ Item f: só o que o nó pediu. `cpf` estava disponível e NÃO foi.
        assert enviado["json"] == {"nome": "Ana"}
        assert run.vars["codigo"] == "AB-1"

    def test_redirecionamento_nao_e_seguido(self, monkeypatch, destino, conversa, flow_a):
        # ⚠️ O teste mais importante do arquivo. Sem `follow_redirects=False`,
        # a URL cadastrada passa em toda checagem e o 302 leva o cliente para
        # 169.254.169.254 sozinho: a cerca inteira vira decoração.
        from apps.automation import http_call

        visto = {}

        class _Cliente:
            def __init__(self, **kwargs):
                visto.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                class _R:
                    status_code = 302

                    def read(self):
                        return b""

                return _R()

        monkeypatch.setattr(http_call.httpx, "Client", _Cliente)
        monkeypatch.setattr(http_call, "check_public_url", lambda u: ["1.2.3.4"])

        run = _run(flow_a, conversa)
        no = self._no(destination_id=destino.pk)

        resultado = http_call.chamar(no, run, conversa)

        assert visto.get("follow_redirects") is False, "o cliente segue redirecionamento"
        assert resultado is False, "3xx com redirecionamento desligado não é sucesso"

    def test_destino_que_virou_interno_e_barrado_no_disparo(
        self, monkeypatch, destino, conversa, flow_a
    ):
        # ⚠️ Item e: o nome cadastrado ontem apontando para fora pode apontar
        # para dentro hoje. Checar só no cadastro deixaria isso passar.
        from apps.automation import http_call

        def _recusa(url):
            raise BlockedDestination("passou a apontar para dentro")

        monkeypatch.setattr(http_call, "check_public_url", _recusa)
        chamou = []
        monkeypatch.setattr(
            http_call.httpx, "Client", lambda **k: chamou.append(k) or _naoDeveria()
        )

        run = _run(flow_a, conversa)
        assert http_call.chamar(self._no(destination_id=destino.pk), run, conversa) is False
        assert chamou == [], "não pode nem abrir a conexão"

    def test_endpoint_fora_do_ar_nao_mata_a_conversa(
        self, monkeypatch, destino, conversa, flow_a
    ):
        from apps.automation import http_call

        class _Cliente:
            def __init__(self, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise httpx.ConnectError("recusou")

        monkeypatch.setattr(http_call.httpx, "Client", _Cliente)
        monkeypatch.setattr(http_call, "check_public_url", lambda u: ["1.2.3.4"])

        run = _run(flow_a, conversa)
        # Item i: devolve False em vez de estourar. Exceção aqui derrubaria o
        # avanço do fluxo e o paciente ficaria sem resposta nenhuma.
        assert http_call.chamar(self._no(destination_id=destino.pk), run, conversa) is False

    def test_destino_de_outra_clinica_nao_e_alcancavel(self, clinic_b, conversa, flow_a):
        from apps.automation import http_call

        de_outra = HttpDestination.objects.create(
            clinic=clinic_b, name="Alheio", url="https://outra.exemplo.com/hook"
        )
        run = _run(flow_a, conversa)
        assert http_call.chamar(self._no(destination_id=de_outra.pk), run, conversa) is False

    def test_auditoria_guarda_as_chaves_e_nunca_os_valores(
        self, monkeypatch, destino, conversa, flow_a
    ):
        # ⚠️ Item h. O log existe para responder ao titular O QUE saiu sobre
        # ele; guardar o conteúdo criaria uma segunda cópia do dado no lugar
        # onde ninguém procura na hora de expurgar.
        from apps.automation import http_call

        class _Cliente:
            def __init__(self, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                class _R:
                    status_code = 201

                    def read(self):
                        return b"{}"

                return _R()

        monkeypatch.setattr(http_call.httpx, "Client", _Cliente)
        monkeypatch.setattr(http_call, "check_public_url", lambda u: ["1.2.3.4"])

        run = _run(flow_a, conversa, vars={"nome": "Ana Paula", "cpf": "12345678909"})
        no = self._no(destination_id=destino.pk, send=["nome", "cpf"])
        http_call.chamar(no, run, conversa)

        log = AuditLog.objects.filter(action=AuditAction.HTTP_CALL).get()
        assert log.payload["campos_enviados"] == ["cpf", "nome"]
        assert log.payload["codigo"] == 201
        bruto = str(log.payload)
        assert "Ana Paula" not in bruto
        assert "12345678909" not in bruto

    def test_no_teste_do_fluxo_anuncia_e_nao_chama(
        self, monkeypatch, destino, conversa, flow_a
    ):
        # RF-FLW-25.4: disparar de verdade mandaria o contato de teste para
        # dentro do ERP da clínica.
        from apps.automation import http_call

        monkeypatch.setattr(
            http_call.httpx, "Client", lambda **k: _naoDeveria()
        )
        run = _run(flow_a, conversa, is_test=True)
        assert http_call.chamar(self._no(destination_id=destino.pk), run, conversa) is True


class TestValidacaoDoGrafo:
    def test_no_sem_destino_nao_publica(self, clinic_a):
        from apps.automation.graph import validate_graph

        problemas = validate_graph(_grafo_http(config={}), clinic=clinic_a)
        assert any("destino escolhido" in p for p in problemas)

    def test_destino_desligado_nao_publica(self, clinic_a, destino):
        from apps.automation.graph import validate_graph

        destino.is_active = False
        destino.save(update_fields=["is_active"])
        problemas = validate_graph(
            _grafo_http(config={"destination_id": destino.pk}), clinic=clinic_a
        )
        assert any("desligado" in p for p in problemas)

    def test_o_no_tem_saida_de_sucesso_e_de_falha(self, clinic_a, destino):
        from apps.automation.graph import required_conditions

        no = Node(id="http", type=FlowNodeType.HTTP_REQUEST, config={"destination_id": destino.pk})
        assert required_conditions(no) == {EDGE_TRUE, EDGE_FALSE}


def _grafo_http(config):
    return {
        "entry_node": "n1",
        "nodes": [
            {"id": "n1", "type": FlowNodeType.START, "label": "Início", "config": {}},
            {
                "id": "http",
                "type": FlowNodeType.HTTP_REQUEST,
                "label": "Avisar o ERP",
                "config": config,
            },
        ],
        "edges": [],
    }


def _run(flow, conversation, *, vars=None, is_test=False):
    from apps.automation.models import FlowRun

    return FlowRun.objects.create(
        clinic=conversation.clinic,
        flow=flow,
        version=flow.current_version,
        conversation=conversation,
        contact=conversation.contact,
        current_node="n1",
        vars=vars or {},
        is_test=is_test,
    )


def _naoDeveria():
    raise AssertionError("a chamada não podia ter saído")
