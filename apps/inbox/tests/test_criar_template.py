"""
Criar template pelo MedEssence (RF-INB-3.2).

⚠️ O que estes testes protegem: cada regra aqui é um jeito de a Meta recusar
HORAS depois, com `rejection_reason` opaco, e sem dizer qual campo estragou.
Os casos vêm do `template-validators` do wacrm (MIT), que já tinha o
inventário completo — inventar da cabeça foi o que custou um envio quebrado ao
vivo em 12/08/2026.
"""

import pytest

from apps.inbox.template_builder import (
    TemplateInvalido,
    montar_para_a_meta,
    status_normalizado,
    validar,
)


def _valido(**mudancas) -> dict:
    """Um template que passa, para cada teste estragar UMA coisa."""
    base = {
        "name": "retorno_paciente",
        "category": "MARKETING",
        "language": "pt_BR",
        "body": "Olá, {{1}}! Sentimos sua falta em {{2}}.",
        "examples": {"body": ["Ivanita", "Oeiras"]},
    }
    base.update(mudancas)
    return base


class TestONome:
    def test_sem_nome_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="precisa de um nome"):
            validar(_valido(name=""))

    @pytest.mark.parametrize(
        "nome",
        ["Retorno_Paciente", "retorno paciente", "retorno-paciente", "retorno!"],
    )
    def test_nome_fora_do_formato_da_meta(self, nome):
        """Só minúscula, dígito e underline. Maiúscula e espaço são os erros
        que quem digita comete sem saber."""
        with pytest.raises(TemplateInvalido, match="letras minúsculas"):
            validar(_valido(name=nome))

    def test_nome_com_numero_e_underline_passa(self):
        validar(_valido(name="comunicado_2026"))


class TestOCorpo:
    def test_sem_texto_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="precisa de um texto"):
            validar(_valido(body="   ", examples={"body": []}))

    def test_acima_de_1024_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="1024"):
            validar(_valido(body="a" * 1025, examples={"body": []}))

    def test_no_limite_exato_passa(self):
        validar(_valido(body="a" * 1024, examples={"body": []}))

    def test_variavel_com_BURACO_nao_passa(self):
        """
        ⚠️ Não é preciosismo da Meta: no envio os parâmetros casam por
        POSIÇÃO, então um `{{2}}` que não existe faria o valor do `{{3}}`
        chegar no lugar dele, sem erro nenhum.
        """
        with pytest.raises(TemplateInvalido, match="sem pular número"):
            validar(_valido(body="Olá {{1}}, veja {{3}}", examples={"body": ["a", "b"]}))

    def test_variavel_que_nao_comeca_no_1_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="começar em"):
            validar(_valido(body="Olá {{2}}", examples={"body": ["a"]}))

    def test_com_cabecalho_a_mensagem_EXPLICA_a_numeracao_separada(self):
        """
        ⚠️ A Meta numera cada componente por si: o `{{1}}` do cabeçalho e o
        `{{1}}` do corpo são campos diferentes. Quem usa `{{1}}` no título
        segue do `{{2}}` no corpo por conta própria — e sem esta frase fica
        procurando erro onde não há.
        """
        with pytest.raises(TemplateInvalido, match="separada da do cabeçalho"):
            validar(
                _valido(
                    header_format="TEXT",
                    header_text="Teste de Sistema - {{1}}",
                    body="Olá {{2}}! Código: {{3}}",
                    examples={"body": ["Willian", "123"], "header": ["234"]},
                )
            )

    def test_numerado_do_1_nos_DOIS_componentes_passa(self):
        validar(
            _valido(
                header_format="TEXT",
                header_text="Teste de Sistema - {{1}}",
                body="Olá {{1}}! Código: {{2}}",
                examples={"body": ["Willian", "123"], "header": ["234"]},
            )
        )


class TestORodape:
    def test_acima_de_60_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="60"):
            validar(_valido(footer="a" * 61))

    def test_com_variavel_nao_passa(self):
        """Ele é igual para todo mundo: a Meta não resolve variável ali."""
        with pytest.raises(TemplateInvalido, match="não aceita variável"):
            validar(_valido(footer="Equipe {{1}}"))


class TestOCabecalho:
    def test_texto_acima_de_60_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="60"):
            validar(_valido(header_format="TEXT", header_text="a" * 61))

    def test_texto_com_DUAS_variaveis_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="no máximo uma variável"):
            validar(
                _valido(
                    header_format="TEXT",
                    header_text="{{1}} e {{2}}",
                    examples={"body": ["Ivanita", "Oeiras"], "header": ["a", "b"]},
                )
            )

    def test_a_variavel_do_cabecalho_tem_que_ser_a_1(self):
        with pytest.raises(TemplateInvalido, match=r"precisa ser \{\{1\}\}"):
            validar(
                _valido(
                    header_format="TEXT",
                    header_text="Aviso de {{2}}",
                    examples={"body": ["Ivanita", "Oeiras"], "header": ["hoje"]},
                )
            )

    def test_cabecalho_de_texto_com_uma_variavel_passa(self):
        validar(
            _valido(
                header_format="TEXT",
                header_text="Aviso de {{1}}",
                examples={"body": ["Ivanita", "Oeiras"], "header": ["hoje"]},
            )
        )

    def test_midia_sem_handle_diz_que_ainda_nao_da(self):
        """
        ⚠️ Na CRIAÇÃO a Meta exige o `header_handle` da Resumable Upload, e
        URL pública não serve - o oposto do ENVIO, onde o handle é justamente
        o que faz a mensagem ser recusada. Enquanto não subimos mídia para
        ela, o caminho fica barrado COM O MOTIVO.
        """
        with pytest.raises(TemplateInvalido, match="ainda não dá para criar"):
            validar(_valido(header_format="IMAGE"))


class TestOsBotoes:
    def test_mais_de_10_nao_passa(self):
        botoes = [{"type": "QUICK_REPLY", "text": f"b{i}"} for i in range(11)]
        with pytest.raises(TemplateInvalido, match="no máximo 10"):
            validar(_valido(buttons=botoes))

    def test_tres_botoes_de_link_nao_passam(self):
        botoes = [
            {"type": "URL", "text": f"Link {i}", "url": "https://x.com"}
            for i in range(3)
        ]
        with pytest.raises(TemplateInvalido, match="botões de link"):
            validar(_valido(buttons=botoes))

    def test_dois_telefones_nao_passam(self):
        botoes = [
            {"type": "PHONE_NUMBER", "text": "Ligar", "phone_number": "+5589999"},
            {"type": "PHONE_NUMBER", "text": "Ligar 2", "phone_number": "+5589998"},
        ]
        with pytest.raises(TemplateInvalido, match="botões de telefone"):
            validar(_valido(buttons=botoes))

    def test_resposta_rapida_INTERCALADA_nao_passa(self):
        """A Meta exige os de resposta rápida agrupados no começo."""
        botoes = [
            {"type": "URL", "text": "Site", "url": "https://x.com"},
            {"type": "QUICK_REPLY", "text": "Sim"},
        ]
        with pytest.raises(TemplateInvalido, match="todos juntos"):
            validar(_valido(buttons=botoes))

    def test_resposta_rapida_agrupada_no_comeco_passa(self):
        botoes = [
            {"type": "QUICK_REPLY", "text": "Sim"},
            {"type": "QUICK_REPLY", "text": "Não"},
            {"type": "URL", "text": "Site", "url": "https://x.com"},
        ]
        validar(_valido(buttons=botoes))

    def test_botao_sem_texto_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="sem texto"):
            validar(_valido(buttons=[{"type": "QUICK_REPLY", "text": "  "}]))

    def test_texto_de_botao_acima_de_25_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="25"):
            validar(_valido(buttons=[{"type": "QUICK_REPLY", "text": "a" * 26}]))

    def test_link_sem_http_nao_passa(self):
        botoes = [{"type": "URL", "text": "Site", "url": "x.com"}]
        with pytest.raises(TemplateInvalido, match="http"):
            validar(_valido(buttons=botoes))

    def test_link_com_variavel_exige_EXEMPLO(self):
        """É o que o revisor humano da Meta usa para ver aonde o botão leva."""
        botoes = [{"type": "URL", "text": "Ver", "url": "https://x.com/{{1}}"}]
        with pytest.raises(TemplateInvalido, match="endereço de exemplo"):
            validar(_valido(buttons=botoes))

    def test_copiar_codigo_exige_exemplo(self):
        with pytest.raises(TemplateInvalido, match="código de exemplo"):
            validar(_valido(buttons=[{"type": "COPY_CODE", "text": "Copiar"}]))


class TestOsExemplos:
    def test_faltando_exemplo_nao_passa(self):
        """
        ⚠️ Não é enfeite: é o que o revisor HUMANO da Meta lê para decidir se
        aprova. Faltando um, o template é recusado por conteúdo, horas depois
        e sem dizer o que faltou.
        """
        with pytest.raises(TemplateInvalido, match="vieram 1"):
            validar(_valido(examples={"body": ["Ivanita"]}))

    def test_exemplo_em_branco_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="em branco"):
            validar(_valido(examples={"body": ["Ivanita", "   "]}))

    def test_exemplo_a_mais_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="vieram 3"):
            validar(_valido(examples={"body": ["a", "b", "c"]}))


class TestACategoria:
    @pytest.mark.parametrize("categoria", ["MARKETING", "UTILITY"])
    def test_as_duas_que_criamos(self, categoria):
        validar(_valido(category=categoria))

    def test_authentication_fica_de_fora(self):
        """Tem regras próprias de conteúdo e de código de verificação."""
        with pytest.raises(TemplateInvalido, match="MARKETING"):
            validar(_valido(category="AUTHENTICATION"))


class TestOPayloadDaMeta:
    def test_a_ordem_e_cabecalho_corpo_rodape_botoes(self):
        payload = montar_para_a_meta(
            _valido(
                header_format="TEXT",
                header_text="Aviso",
                footer="MedEssence",
                buttons=[{"type": "QUICK_REPLY", "text": "Sim"}],
            )
        )
        assert [c["type"] for c in payload["components"]] == [
            "HEADER",
            "BODY",
            "FOOTER",
            "BUTTONS",
        ]

    def test_o_exemplo_do_corpo_e_lista_DE_LISTAS(self):
        """
        ⚠️ `body_text` é lista de listas (a de fora é o conjunto de exemplos, a
        de dentro os valores de uma passagem) e `header_text` é lista simples.
        Trocar os dois é recusa na hora.
        """
        payload = montar_para_a_meta(
            _valido(
                header_format="TEXT",
                header_text="Aviso de {{1}}",
                examples={"body": ["Ivanita", "Oeiras"], "header": ["hoje"]},
            )
        )
        corpo = next(c for c in payload["components"] if c["type"] == "BODY")
        cabecalho = next(c for c in payload["components"] if c["type"] == "HEADER")
        assert corpo["example"] == {"body_text": [["Ivanita", "Oeiras"]]}
        assert cabecalho["example"] == {"header_text": ["hoje"]}

    def test_botao_de_link_FIXO_nao_leva_exemplo(self):
        """Exemplo em botão sem variável é recusado."""
        payload = montar_para_a_meta(
            _valido(
                buttons=[
                    {
                        "type": "URL",
                        "text": "Site",
                        "url": "https://x.com/fixo",
                        "example": "sobra",
                    }
                ]
            )
        )
        botoes = next(c for c in payload["components"] if c["type"] == "BUTTONS")
        assert "example" not in botoes["buttons"][0]

    def test_template_sem_variavel_nao_leva_example(self):
        payload = montar_para_a_meta(
            _valido(body="Sua consulta está confirmada.", examples={"body": []})
        )
        corpo = next(c for c in payload["components"] if c["type"] == "BODY")
        assert "example" not in corpo

    def test_montar_valida_antes(self):
        """Montar um payload que a Meta vai recusar só troca o nosso erro,
        específico, pelo dela, genérico."""
        with pytest.raises(TemplateInvalido):
            montar_para_a_meta(_valido(name="Nome Errado"))


class TestOStatus:
    def test_pending_review_e_apelido_de_pending(self):
        assert status_normalizado("PENDING_REVIEW") == "PENDING"

    def test_status_desconhecido_vira_pending_e_nao_some(self):
        """A linha precisa aparecer para quem acabou de criar o template."""
        assert status_normalizado("COISA_NOVA_DA_META") == "PENDING"
        assert status_normalizado(None) == "PENDING"

    def test_os_conhecidos_passam_como_estao(self):
        for status in ("APPROVED", "REJECTED", "PAUSED"):
            assert status_normalizado(status.lower()) == status


class TestOsCasosQueSOAMETADIZ:
    """
    ⚠️ Três regras que NÃO estão na documentação de limites: só aparecem no
    `user_msg` de uma recusa. Descobertas ao vivo em 13/08/2026 com um
    template cujo corpo era `{{1}}` — a Meta respondeu `subcode=2388047`, e o
    custo de descobrir assim é o nome já ocupado na conta.
    """

    def test_corpo_SO_com_variavel_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="só campos variáveis"):
            validar(_valido(body="{{1}}", examples={"body": ["Ana"]}))

    def test_variavel_com_texto_em_volta_passa(self):
        validar(_valido(body="Olá, {{1}}!", examples={"body": ["Ana"]}))

    def test_mais_de_duas_linhas_em_branco_seguidas_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="linhas em branco"):
            validar(
                _valido(body="Olá {{1}}\n\n\n\nTchau", examples={"body": ["Ana"]})
            )

    def test_duas_linhas_em_branco_passam(self):
        validar(_valido(body="Olá {{1}}\n\nTchau", examples={"body": ["Ana"]}))

    def test_mais_de_dez_emojis_nao_passa(self):
        with pytest.raises(TemplateInvalido, match="11 emojis"):
            validar(
                _valido(body="Oi {{1}} " + "😀" * 11, examples={"body": ["Ana"]})
            )


class TestOErroDaMetaNaTela:
    """
    ⚠️ `str(exc)` do PyWa é o repr INTEIRO da exceção. Ele vazou para a tela em
    13/08/2026 num balão que dizia "WhatsAppError(code=100, message='Invalid
    parameter', details=None, fbtrace_id='AAmtmv...', raw_response=<Response
    [400 Bad Request]>, subcode=2388047, ...)" — a clínica não tem como ler
    isso, e o que ela precisa (`user_title`) vem TRADUZIDO pela própria Meta.
    """

    def _erro(self, **kwargs):
        from pywa import errors as pywa_errors

        return pywa_errors.WhatsAppError(
            raw={}, code=100, message="Invalid parameter", **kwargs
        )

    def test_usa_o_titulo_traduzido_que_a_meta_manda(self):
        from apps.integrations.whatsapp.meta.adapter import _motivo

        exc = self._erro(
            user_title="O formato do corpo da mensagem está incorreto",
            user_msg="The message body can't have more than...",
        )
        assert _motivo(exc) == "O formato do corpo da mensagem está incorreto."

    def test_sem_titulo_cai_na_mensagem_e_nunca_no_repr(self):
        from apps.integrations.whatsapp.meta.adapter import _motivo

        motivo = _motivo(self._erro())
        assert motivo == "Invalid parameter"
        assert "fbtrace_id" not in motivo
        assert "raw_response" not in motivo


def test_payload_pede_recategorizacao_a_meta():
    """
    ⚠️ Sem `allow_category_change` a Meta RECUSA quando discorda da categoria
    ("Modelo de mensagem com categoria inválida"), sem dizer qual seria a
    certa. Aconteceu em produção em 18/08. Com a chave, ela recategoriza e
    segue para revisão, que é melhor para a clínica em qualquer caso.
    """
    from apps.inbox.template_builder import montar_para_a_meta

    payload = montar_para_a_meta(
        {
            "name": "aviso_de_retorno",
            "category": "UTILITY",
            "language": "pt_BR",
            "body": "Olá {{1}}, seu retorno está próximo.",
            "examples": {"body": ["Ana"]},
        }
    )

    assert payload["allow_category_change"] is True
