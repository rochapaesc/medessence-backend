"""
Criar template na Meta: o que ela cobra, cobrado ANTES de chamar (RF-INB-3.2).

⚠️ Este módulo é o par de `template_vars.py`, e os dois olham para lados
opostos do mesmo objeto. Lá é o ENVIO: quais componentes vão junto de cada
mensagem. Aqui é a CRIAÇÃO: o que a Meta aceita registrar como template. As
regras não são simétricas, e a que mais engana é o cabeçalho de mídia — na
criação ela exige o `header_handle` da Resumable Upload, e no envio esse mesmo
handle faz a mensagem ser recusada.

Validar aqui, e não na fronteira da Meta, é a diferença entre "o rodapé não
aceita variável" e um 400 genérico com `rejection_reason` opaco horas depois.
Os limites são os da Cloud API v21, conferidos no `template-validators` do
wacrm (MIT), que já tinha o inventário completo e testado.
"""

import re

#: `{{1}}`, `{{2}}` ... como a Meta numera.
_VARIAVEL = re.compile(r"\{\{\s*(\d+)\s*\}\}")

#: Nome de template é identificador: minúscula, dígito e underline.
_NOME = re.compile(r"^[a-z0-9_]{1,512}$")

TETO_DO_CORPO = 1024
TETO_DO_RODAPE = 60
TETO_DO_CABECALHO = 60
TETO_DO_TEXTO_DO_BOTAO = 25

MAX_BOTOES = 10
MAX_BOTOES_URL = 2
MAX_BOTOES_TELEFONE = 1
MAX_BOTOES_COPY_CODE = 1

#: Formatos de cabeçalho que a Meta aceita.
CABECALHOS = {"TEXT", "IMAGE", "VIDEO", "DOCUMENT"}

#: Categorias que esta tela cria. AUTHENTICATION fica de fora: tem regras
#: próprias de conteúdo e de código de verificação, e o wacrm também a bloqueia.
CATEGORIAS = {"MARKETING", "UTILITY"}

TIPOS_DE_BOTAO = {"QUICK_REPLY", "URL", "PHONE_NUMBER", "COPY_CODE"}


class TemplateInvalido(ValueError):
    """
    O template não passa numa regra da Meta.

    Erro de negócio e não de programação: a mensagem sai direto para a tela e
    precisa dizer QUAL campo e o que fazer.
    """


def variaveis(texto: str) -> list[int]:
    """Os números das variáveis, sem repetir e em ordem. `[1, 2, 4]`."""
    achadas = {int(m.group(1)) for m in _VARIAVEL.finditer(texto or "")}
    return sorted(n for n in achadas if n >= 1)


def _exigir_contiguas(numeros: list[int], onde: str, tem_cabecalho=False) -> None:
    """
    ⚠️ A Meta exige `{{1}}, {{2}}, {{3}}` sem buraco. E o buraco não é
    preciosismo dela: no envio os parâmetros casam por POSIÇÃO, então um
    `{{2}}` que não existe faria o valor do `{{3}}` chegar no lugar dele.

    ⚠️ Ela também numera CADA COMPONENTE separadamente: o `{{1}}` do
    cabeçalho e o `{{1}}` do corpo são variáveis diferentes. Quem usa `{{1}}`
    no título numera o corpo a partir do `{{2}}` por conta própria, e sem esta
    explicação fica procurando erro onde não há.
    """
    for i, numero in enumerate(numeros):
        if numero != i + 1:
            achadas = ", ".join(f"{{{{{n}}}}}" for n in numeros)
            extra = (
                " A contagem do texto é separada da do cabeçalho: mesmo com "
                "{{1}} lá em cima, aqui começa no {{1}} de novo."
                if tem_cabecalho
                else ""
            )
            raise TemplateInvalido(
                f"As variáveis {onde} precisam começar em {{{{1}}}} e seguir "
                f"sem pular número. Encontrei {achadas}.{extra}"
            )


def validar_nome(nome: str) -> None:
    nome = (nome or "").strip()
    if not nome:
        raise TemplateInvalido("O template precisa de um nome.")
    if not _NOME.match(nome):
        raise TemplateInvalido(
            "O nome só aceita letras minúsculas, números e underline "
            '(por exemplo: "retorno_paciente").'
        )


#: Quantos emojis a Meta aceita no corpo.
MAX_EMOJIS = 10

#: Faixas Unicode de emoji, o suficiente para contar o que a Meta conta.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF]"
)

#: Três ou mais quebras seguidas. A Meta aceita no máximo DUAS.
_QUEBRAS_DEMAIS = re.compile(r"\n{3,}")


def validar_corpo(corpo: str, tem_cabecalho: bool = False) -> list[int]:
    """
    ⚠️ As três últimas regras vieram de uma recusa ao vivo em 13/08/2026, com
    um template cujo corpo era só `{{1}}`. A Meta respondeu
    `subcode=2388047`, e o `user_msg` dizia o que ela não aceita: mais de duas
    quebras de linha seguidas, corpo SÓ com parâmetros, ou mais de 10 emojis.
    Nenhuma delas está na documentação de limites - só no erro.
    """
    corpo = corpo or ""
    if not corpo.strip():
        raise TemplateInvalido("O template precisa de um texto.")
    if len(corpo) > TETO_DO_CORPO:
        raise TemplateInvalido(
            f"O texto tem {len(corpo)} caracteres e o limite é {TETO_DO_CORPO}."
        )
    numeros = variaveis(corpo)
    _exigir_contiguas(numeros, "do texto", tem_cabecalho)

    # Corpo só de parâmetros: sobra nada quando se tiram os `{{n}}`.
    if numeros and not _VARIAVEL.sub("", corpo).strip():
        raise TemplateInvalido(
            "O texto não pode ser só campos variáveis. Escreva a mensagem em "
            "volta deles, como \"Olá, {{1}}! Sentimos sua falta.\"."
        )
    if _QUEBRAS_DEMAIS.search(corpo):
        raise TemplateInvalido(
            "O texto tem mais de duas linhas em branco seguidas, e o WhatsApp "
            "não aceita. Junte os parágrafos."
        )
    achados = len(_EMOJI.findall(corpo))
    if achados > MAX_EMOJIS:
        raise TemplateInvalido(
            f"O texto tem {achados} emojis e o WhatsApp aceita no máximo "
            f"{MAX_EMOJIS}."
        )
    # ⚠️ "Proporção entre palavras e parâmetros" NÃO é validada aqui, e é de
    # propósito. A Meta recusa por ela (visto ao vivo em 13/08/2026 com
    # `Ola, {{1}}`) e não publica o limite: bloquear por um palpite reprovaria
    # texto que ela aceita - `Olá {{1}}! Código: {{2}}` tem a MESMA proporção
    # e é mensagem legítima. A tela avisa, e quem decide é ela.
    return numeros


def validar_rodape(rodape: str | None) -> None:
    rodape = rodape or ""
    if not rodape:
        return
    if len(rodape) > TETO_DO_RODAPE:
        raise TemplateInvalido(
            f"O rodapé tem {len(rodape)} caracteres e o limite é {TETO_DO_RODAPE}."
        )
    if variaveis(rodape):
        raise TemplateInvalido(
            "O rodapé não aceita variável: ele é igual para todo mundo."
        )


def validar_cabecalho(formato: str | None, texto: str | None, midia: str | None) -> int:
    """Devolve quantas variáveis o cabeçalho tem (0 ou 1)."""
    formato = (formato or "").upper()
    if not formato:
        return 0
    if formato not in CABECALHOS:
        raise TemplateInvalido(f'Cabeçalho "{formato}" não existe no WhatsApp.')

    if formato == "TEXT":
        texto = texto or ""
        if not texto.strip():
            raise TemplateInvalido("O cabeçalho de texto precisa de um texto.")
        if len(texto) > TETO_DO_CABECALHO:
            raise TemplateInvalido(
                f"O cabeçalho tem {len(texto)} caracteres e o limite é "
                f"{TETO_DO_CABECALHO}."
            )
        numeros = variaveis(texto)
        if len(numeros) > 1:
            raise TemplateInvalido(
                f"O cabeçalho aceita no máximo uma variável, e tem {len(numeros)}."
            )
        if numeros and numeros[0] != 1:
            raise TemplateInvalido(
                "A variável do cabeçalho precisa ser {{1}}, e não "
                f"{{{{{numeros[0]}}}}}."
            )
        return len(numeros)

    # ⚠️ Imagem, vídeo e documento precisam do `header_handle` da Resumable
    # Upload: URL pública NÃO é aceita na criação, ao contrário do envio. Como
    # ainda não subimos mídia para a Meta, este caminho fica barrado com o
    # motivo em vez de falhar lá na frente.
    if not midia:
        raise TemplateInvalido(
            "Cabeçalho com imagem, vídeo ou documento ainda não dá para criar "
            "por aqui. Use cabeçalho de texto ou nenhum."
        )
    return 0


def _contar_por_tipo(botoes: list[dict]) -> dict[str, int]:
    contagem = dict.fromkeys(TIPOS_DE_BOTAO, 0)
    for botao in botoes:
        tipo = (botao.get("type") or "").upper()
        if tipo not in TIPOS_DE_BOTAO:
            raise TemplateInvalido(f'Botão do tipo "{tipo}" não existe no WhatsApp.')
        contagem[tipo] += 1
    return contagem


def validar_botoes(botoes: list[dict] | None) -> None:
    botoes = botoes or []
    if not botoes:
        return
    if len(botoes) > MAX_BOTOES:
        raise TemplateInvalido(
            f"São {len(botoes)} botões e o WhatsApp aceita no máximo {MAX_BOTOES}."
        )

    contagem = _contar_por_tipo(botoes)
    for tipo, teto, nome in [
        ("URL", MAX_BOTOES_URL, "de link"),
        ("PHONE_NUMBER", MAX_BOTOES_TELEFONE, "de telefone"),
        ("COPY_CODE", MAX_BOTOES_COPY_CODE, "de copiar código"),
    ]:
        if contagem[tipo] > teto:
            plural = "botão" if teto == 1 else "botões"
            raise TemplateInvalido(
                f"São {contagem[tipo]} botões {nome} e o WhatsApp aceita no "
                f"máximo {teto} {plural}."
            )

    # ⚠️ Os de resposta rápida vão AGRUPADOS no começo: a Meta recusa quando
    # eles aparecem depois de um botão de link ou de telefone.
    ja_viu_outro = False
    for botao in botoes:
        if (botao.get("type") or "").upper() == "QUICK_REPLY":
            if ja_viu_outro:
                raise TemplateInvalido(
                    "Os botões de resposta rápida precisam vir todos juntos, "
                    "antes dos de link e de telefone."
                )
        else:
            ja_viu_outro = True

    for i, botao in enumerate(botoes, start=1):
        tipo = (botao.get("type") or "").upper()
        texto = (botao.get("text") or "").strip()
        if not texto:
            raise TemplateInvalido(f"O botão {i} está sem texto.")
        if len(texto) > TETO_DO_TEXTO_DO_BOTAO:
            raise TemplateInvalido(
                f'O texto do botão {i} ("{texto}") tem {len(texto)} caracteres '
                f"e o limite é {TETO_DO_TEXTO_DO_BOTAO}."
            )
        if tipo == "URL":
            _validar_botao_de_link(i, botao)
        elif tipo == "PHONE_NUMBER" and not (botao.get("phone_number") or "").strip():
            raise TemplateInvalido(f"O botão {i} está sem o telefone.")
        elif tipo == "COPY_CODE" and not (botao.get("example") or "").strip():
            raise TemplateInvalido(
                f"O botão {i} copia um código, e a Meta pede um código de "
                "exemplo para revisar."
            )


def _validar_botao_de_link(i: int, botao: dict) -> None:
    url = (botao.get("url") or "").strip()
    if not url:
        raise TemplateInvalido(f"O botão {i} está sem o endereço.")
    if not url.lower().startswith(("http://", "https://")):
        raise TemplateInvalido(
            f"O endereço do botão {i} precisa começar com http:// ou https://."
        )
    numeros = variaveis(url)
    if len(numeros) > 1:
        raise TemplateInvalido(
            f"O endereço do botão {i} aceita no máximo uma variável."
        )
    if numeros:
        if numeros[0] != 1:
            raise TemplateInvalido(
                f"A variável do endereço do botão {i} precisa ser {{{{1}}}}."
            )
        if not (botao.get("example") or "").strip():
            raise TemplateInvalido(
                f"O endereço do botão {i} tem uma variável, e a Meta pede um "
                "endereço de exemplo para revisar."
            )


def validar_exemplos(
    exemplos: dict | None, no_corpo: int, no_cabecalho: int
) -> None:
    """
    ⚠️ Os exemplos não são enfeite: são o que o revisor HUMANO da Meta lê para
    decidir se aprova. Faltando um, o template é recusado por conteúdo, o que
    demora horas e não diz o que faltou.
    """
    exemplos = exemplos or {}
    for chave, quantas, onde in [
        ("body", no_corpo, "do texto"),
        ("header", no_cabecalho, "do cabeçalho"),
    ]:
        valores = exemplos.get(chave) or []
        if not isinstance(valores, list):
            raise TemplateInvalido(f"Os exemplos {onde} precisam ser uma lista.")
        if len(valores) != quantas:
            raise TemplateInvalido(
                f"O texto {onde} tem {quantas} "
                f"{'variável' if quantas == 1 else 'variáveis'}, e vieram "
                f"{len(valores)} {'exemplo' if len(valores) == 1 else 'exemplos'}."
            )
        for i, valor in enumerate(valores, start=1):
            if not (valor or "").strip():
                raise TemplateInvalido(f"O exemplo {i} {onde} está em branco.")


def validar(dados: dict) -> tuple[int, int]:
    """
    Todas as regras, na ordem em que a pessoa preenche.

    Para na PRIMEIRA falha, com a mensagem daquele campo: uma lista de sete
    erros de uma vez não diz por onde começar. Devolve quantas variáveis o
    corpo e o cabeçalho têm, que é o que a montagem precisa depois.
    """
    validar_nome(dados.get("name"))

    categoria = (dados.get("category") or "").upper()
    if categoria not in CATEGORIAS:
        raise TemplateInvalido(
            "A categoria precisa ser MARKETING (promoção, convite, resgate) ou "
            "UTILITY (confirmação, lembrete, aviso de algo que a pessoa pediu)."
        )
    if not (dados.get("language") or "").strip():
        raise TemplateInvalido("O template precisa de um idioma.")

    no_cabecalho = validar_cabecalho(
        dados.get("header_format"),
        dados.get("header_text"),
        dados.get("header_handle"),
    )
    # O corpo depois do cabeçalho: a mensagem de numeração precisa saber se há
    # variável lá em cima para explicar que a contagem é separada.
    no_corpo = len(validar_corpo(dados.get("body"), no_cabecalho > 0))
    validar_rodape(dados.get("footer"))
    validar_botoes(dados.get("buttons"))
    validar_exemplos(dados.get("examples"), no_corpo, no_cabecalho)
    return no_corpo, no_cabecalho


def _componente_do_cabecalho(dados: dict, exemplos: dict) -> dict | None:
    formato = (dados.get("header_format") or "").upper()
    if not formato:
        return None
    if formato == "TEXT":
        componente = {"type": "HEADER", "format": "TEXT", "text": dados["header_text"]}
        do_cabecalho = exemplos.get("header") or []
        if do_cabecalho:
            # ⚠️ `header_text` é uma lista SIMPLES, enquanto o do corpo é uma
            # lista DE LISTAS. Trocar os dois é recusa na hora.
            componente["example"] = {"header_text": list(do_cabecalho)}
        return componente
    return {
        "type": "HEADER",
        "format": formato,
        "example": {"header_handle": [dados["header_handle"]]},
    }


def _componente_dos_botoes(botoes: list[dict]) -> dict:
    saida = []
    for botao in botoes:
        tipo = (botao.get("type") or "").upper()
        item = {"type": tipo, "text": (botao.get("text") or "").strip()}
        if tipo == "URL":
            item["url"] = (botao.get("url") or "").strip()
            exemplo = (botao.get("example") or "").strip()
            # Só vai quando a URL tem variável: exemplo em botão de link fixo
            # é recusado.
            if exemplo and variaveis(item["url"]):
                item["example"] = [exemplo]
        elif tipo == "PHONE_NUMBER":
            item["phone_number"] = (botao.get("phone_number") or "").strip()
        elif tipo == "COPY_CODE":
            item["example"] = [(botao.get("example") or "").strip()]
        saida.append(item)
    return {"type": "BUTTONS", "buttons": saida}


def montar_para_a_meta(dados: dict) -> dict:
    """
    O corpo do `POST /{waba_id}/message_templates`.

    Valida antes: montar um payload que a Meta vai recusar só troca um erro
    nosso, específico, por um erro dela, genérico.
    """
    validar(dados)
    exemplos = dados.get("examples") or {}

    componentes: list[dict] = []
    cabecalho = _componente_do_cabecalho(dados, exemplos)
    if cabecalho:
        componentes.append(cabecalho)

    corpo = {"type": "BODY", "text": dados["body"]}
    do_corpo = exemplos.get("body") or []
    if do_corpo:
        # ⚠️ Lista DE LISTAS: a de fora é o conjunto de exemplos, a de dentro
        # são os valores de uma passagem. Mandar uma lista simples é recusa.
        corpo["example"] = {"body_text": [list(do_corpo)]}
    componentes.append(corpo)

    rodape = (dados.get("footer") or "").strip()
    if rodape:
        componentes.append({"type": "FOOTER", "text": rodape})

    if dados.get("buttons"):
        componentes.append(_componente_dos_botoes(dados["buttons"]))

    return {
        "name": (dados["name"] or "").strip(),
        "category": (dados["category"] or "").upper(),
        "language": (dados["language"] or "").strip(),
        "components": componentes,
    }


#: Status que a Meta devolve. `PENDING_REVIEW` é apelido de `PENDING`, e o que
#: não reconhecemos vira `PENDING` para a linha não sumir da tela de quem
#: acabou de criar.
STATUS_CONHECIDOS = {
    "PENDING",
    "APPROVED",
    "REJECTED",
    "PAUSED",
    "DISABLED",
    "IN_APPEAL",
    "PENDING_DELETION",
}


def status_normalizado(cru: str | None) -> str:
    valor = (cru or "").upper()
    if valor == "PENDING_REVIEW":
        return "PENDING"
    return valor if valor in STATUS_CONHECIDOS else "PENDING"
