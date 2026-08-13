"""
As variáveis de um template aprovado: quais ele pede, de onde cada uma sai e
como a mensagem fica montada.

⚠️ Um módulo só para os TRÊS lugares que mandam template (Inbox, nó de fluxo e
campanha de resgate). Antes cada um resolvia do seu jeito, e dois estavam
quebrados: o nó do fluxo mandava texto livre e o Inbox mandava template sem
parâmetro nenhum. Resolver variável em três lugares é como a mensagem começa a
sair diferente da prévia que a clínica aprovou.

O formato do mapa é o mesmo em todo lugar:

    {"1": {"source": "patient_first_name"},
     "2": {"source": "fixed", "value": "MedEssence"}}

`source` é fechado (`VariableSource`) porque a alternativa seria aceitar um
caminho de atributo digitado, e aí a mensagem que sai para milhares de pessoas
passa a depender de texto livre que ninguém valida.
"""

import re

from apps.inbox.choices import VariableSource

#: `{{1}}`, `{{2}}` ... na ordem em que a Meta numera.
_VARIAVEL = re.compile(r"\{\{\s*(\d+)\s*\}\}")

#: Os formatos de cabeçalho que exigem o component de mídia em todo envio.
_MIDIAS = {"IMAGE", "VIDEO", "DOCUMENT"}

#: Como cada um se chama para quem preenche.
_NOME_DA_MIDIA = {"IMAGE": "imagem", "VIDEO": "vídeo", "DOCUMENT": "documento"}

#: Espelha `Fmt._particulas` do front (`lib/core/utils/formatters.dart`). Os
#: dois lados precisam produzir o MESMO texto: a tela mostra o nome do
#: paciente e a prévia mostra a mensagem com ele dentro, lado a lado.
_PARTICULAS = {"de", "da", "do", "das", "dos", "e", "di", "du"}


def nome_proprio(nome: str | None) -> str:
    """
    Caixa de Título para leitura, mantendo o cadastro como está.

    Nome que JÁ vem com maiúsculas e minúsculas fica intacto: quem digitou
    escolheu, e "McArthur" não pode virar "Mcarthur". O prontuário da clínica
    real guarda tudo em caixa alta ("IVANITA DIAS DE SOUSA"), que é o caso que
    esta função existe para tratar.
    """
    limpo = (nome or "").strip()
    if not limpo or limpo != limpo.upper():
        return limpo
    partes = re.split(r"\s+", limpo.lower())
    return " ".join(
        parte if (i > 0 and parte in _PARTICULAS) else parte[:1].upper() + parte[1:]
        for i, parte in enumerate(partes)
    )


def corpo_do_template(template) -> str:
    """O texto do componente BODY, que é onde as variáveis moram."""
    for componente in (template.components if template else None) or []:
        if componente.get("type") == "BODY":
            return componente.get("text") or ""
    return ""


def _componente(template, tipo: str) -> dict | None:
    for c in (template.components if template else None) or []:
        if (c.get("type") or "").upper() == tipo:
            return c
    return None


def variaveis_do_template(template) -> list[str]:
    """
    TODAS as variáveis que o template pede, qualificadas por componente.

    ⚠️ A Meta numera cada componente SEPARADAMENTE: o `{{1}}` do corpo e o
    `{{1}}` da URL de um botão são variáveis diferentes, e cada uma vai num
    `component` próprio no envio. Tratar só as do corpo foi o que produziu o
    `(#131008) Button at index 0 of type Url requires a parameter` ao vivo em
    12/08/2026 - o corpo ia certo e o botão ia vazio.

    As chaves saem assim:

        "1", "2"          o corpo (sem prefixo, que era o formato antigo)
        "header:1"        o cabeçalho, quando ele tem variável
        "button:0:1"      o botão de índice 0

    Quais botões pedem parâmetro, e quais não, vem do `template-send-builder`
    do wacrm (MIT), que já tinha isto resolvido:

      - URL **sem** variável não entra: o template carrega a URL inteira.
      - COPY_CODE entra SEMPRE, com o `example` do template como padrão.
      - QUICK_REPLY e PHONE_NUMBER nunca aceitam parâmetro no envio.
    """
    chaves: list[str] = []

    cabecalho = _componente(template, "HEADER")
    formato = (cabecalho or {}).get("format") or "TEXT"
    if cabecalho and formato.upper() == "TEXT":
        achadas = {m.group(1) for m in _VARIAVEL.finditer(cabecalho.get("text") or "")}
        chaves += [f"header:{n}" for n in sorted(achadas, key=int)]
    elif cabecalho and formato.upper() in _MIDIAS:
        # ⚠️ Header de MÍDIA vai em TODO envio, mesmo sem variável e mesmo com
        # a imagem inalterada desde a aprovação: a Meta recusa a mensagem
        # inteira sem ele. Vira uma variável como as outras para reusar a
        # mesma tela e a mesma cobrança - do contrário seria o único campo
        # obrigatório que ninguém vê faltando.
        chaves.append("header:media")

    achadas = {m.group(1) for m in _VARIAVEL.finditer(corpo_do_template(template))}
    chaves += sorted(achadas, key=int)

    # ⚠️ O índice é a posição no array INTEIRO de botões, contando os que não
    # pedem parâmetro: um quick-reply antes do URL desloca o índice, e a Meta
    # casa o component por ele. (wacrm: "uses the correct index when QR
    # buttons precede the URL button".)
    botoes = _componente(template, "BUTTONS")
    for indice, botao in enumerate((botoes or {}).get("buttons") or []):
        tipo = (botao.get("type") or "").upper()
        if tipo == "URL":
            achadas = {m.group(1) for m in _VARIAVEL.finditer(botao.get("url") or "")}
            chaves += [f"button:{indice}:{n}" for n in sorted(achadas, key=int)]
        elif tipo == "COPY_CODE":
            # Sempre pede valor, e o `example` do template serve de padrão:
            # o caso comum é um código promocional fixo.
            chaves.append(f"button:{indice}:1")

    return chaves


def _botao(template, chave: str) -> dict:
    """O botão a que uma chave `button:i:n` se refere, ou `{}`."""
    if not chave.startswith("button:"):
        return {}
    _, indice, _ = chave.split(":")
    botoes = (_componente(template, "BUTTONS") or {}).get("buttons") or []
    return botoes[int(indice)] if int(indice) < len(botoes) else {}


def formato_do_cabecalho(template) -> str:
    """`TEXT`, `IMAGE`, `VIDEO`, `DOCUMENT`, ou vazio se não há cabeçalho."""
    cabecalho = _componente(template, "HEADER")
    if not cabecalho:
        return ""
    return (cabecalho.get("format") or "TEXT").upper()


def rotulo_da_variavel(template, chave: str) -> str:
    """Como a tela chama esta variável, para a pessoa saber onde ela cai."""
    if chave == "header:media":
        formato = formato_do_cabecalho(template)
        return f"{_NOME_DA_MIDIA.get(formato, 'mídia')} do cabeçalho"
    if chave.startswith("header:"):
        return "cabeçalho"
    if chave.startswith("button:"):
        botao = _botao(template, chave)
        titulo = botao.get("text") or ""
        # ⚠️ "final do link", e não "link": na Meta a URL do botão é fixa até
        # a variável (`https://.../agenda/{{1}}`), então quem digita o endereço
        # inteiro monta `https://.../agenda/https://...` e o botão leva a
        # lugar nenhum. O modelo vai junto em `modelo_do_link` para a tela
        # mostrar o endereço se formando.
        if (botao.get("type") or "").upper() == "COPY_CODE":
            return f'código do botão "{titulo}"' if titulo else "código do botão"
        return f'final do link do botão "{titulo}"' if titulo else "final do link"
    return f"{{{{{chave}}}}}"


def modelo_do_link(template, chave: str) -> str:
    """
    A URL do botão como está no template aprovado, com o `{{n}}` no lugar.

    A tela substitui a variável pelo que a pessoa digita e mostra o endereço
    final embaixo do campo, que é como se enxerga na hora que o link ficou
    errado. Vazio para o que não é botão de URL.
    """
    botao = _botao(template, chave)
    if (botao.get("type") or "").upper() != "URL":
        return ""
    return botao.get("url") or ""


class Contexto:
    """
    O que existe na hora de resolver uma variável.

    Cada lugar preenche o que tem: a campanha tem paciente e clínica, o Inbox
    tem a conversa (com paciente OU só contato) e o fluxo tem, além disso, o
    que coletou. Campo ausente é `None`, e a fonte que depende dele devolve
    vazio em vez de estourar - a prévia precisa continuar montando para a
    pessoa VER o buraco.
    """

    def __init__(self, *, clinic=None, patient=None, contact=None, flow_vars=None):
        self.clinic = clinic
        self.patient = patient
        self.contact = contact
        self.flow_vars = flow_vars or {}

    @classmethod
    def da_conversa(cls, conversation, flow_vars=None):
        """O contexto do Inbox e do fluxo, montado da conversa."""
        return cls(
            clinic=conversation.clinic,
            patient=conversation.patient,
            contact=conversation.contact,
            flow_vars=flow_vars,
        )


def valor_da_variavel(config: dict, contexto: Contexto) -> str:
    """
    O valor de UMA variável, pela fonte configurada.

    Fonte desconhecida ou dado ausente devolvem string vazia em vez de
    estourar: a prévia precisa continuar montando para quem configura VER o
    buraco (cidade em branco no meio da frase) em vez de receber um erro.
    """
    config = config or {}
    fonte = config.get("source")
    patient = contexto.patient

    if fonte == VariableSource.FIXED:
        return (config.get("value") or "").strip()
    if fonte == VariableSource.FLOW_VAR:
        # `value` guarda a CHAVE da variável coletada, não o valor: o valor só
        # existe na execução.
        return str(contexto.flow_vars.get(config.get("value") or "") or "").strip()
    if fonte == VariableSource.CLINIC_NAME:
        return (getattr(contexto.clinic, "name", "") or "").strip()
    if fonte == VariableSource.CONTACT_NAME:
        # Como a pessoa escolheu no WhatsApp, com emoji e apelido: é o nome
        # dela, e "corrigir" seria inventar outro.
        return (getattr(contexto.contact, "display_name", "") or "").strip()
    if fonte == VariableSource.PATIENT_CITY:
        # Mesma Caixa de Título do nome: o prontuário guarda "DOM INOCÊNCIO" e
        # a mensagem sairia gritando o nome da cidade no meio da frase.
        return nome_proprio(getattr(patient, "city", ""))
    if fonte == VariableSource.PATIENT_FULL_NAME:
        return nome_proprio(getattr(patient, "name", ""))
    if fonte == VariableSource.PATIENT_FIRST_NAME:
        completo = nome_proprio(getattr(patient, "name", ""))
        return completo.split(" ")[0] if completo else ""
    return ""


def valores(template, mapa: dict, contexto: Contexto) -> dict[str, str]:
    """O mapa `{"1": "Ivanita", "2": "MedEssence"}` resolvido."""
    mapa = mapa or {}
    return {
        chave: valor_da_variavel(mapa.get(chave), contexto)
        for chave in variaveis_do_template(template)
    }


def parametros(template, mapa: dict, contexto: Contexto) -> dict[str, str]:
    """Todas as variáveis resolvidas, pela chave qualificada."""
    return valores(template, mapa, contexto)


def montar(template, mapa: dict, contexto: Contexto) -> str:
    """A mensagem como vai chegar, com os valores no lugar dos `{{n}}`."""
    resolvidos = valores(template, mapa, contexto)
    return _VARIAVEL.sub(lambda m: resolvidos.get(m.group(1), ""), corpo_do_template(template))


def componentes_para_a_meta(template, resolvidos: dict[str, str]) -> list | None:
    """
    Os `components` no formato que o `send_template` recebe.

    Um por componente do template, e a Meta casa cada um por POSIÇÃO dentro
    dele - por isso a lista de cada bloco é preenchida do 1 até o maior número
    usado, com vazio nos buracos. Sem isso, um `{{2}}` faltando faria o valor
    do `{{3}}` chegar no lugar dele, sem erro nenhum.

    Lista vazia devolve `None`: template sem variável não pode levar um
    `components` vazio, que a Meta recusa tanto quanto parâmetro faltando.

    ⚠️ Header de MÍDIA sai em TODO envio, mesmo sem variável e mesmo com a
    imagem inalterada desde a aprovação: sem ele a Meta recusa a mensagem
    inteira. O valor vem em `header:media` como qualquer outra variável.
    """

    def _em_ordem(prefixo: str) -> list[str]:
        numeros = {}
        for chave, valor in resolvidos.items():
            if prefixo and not chave.startswith(prefixo):
                continue
            if not prefixo and (":" in chave):
                continue
            numero = chave.split(":")[-1]
            if numero.isdigit():
                numeros[int(numero)] = valor
        if not numeros:
            return []
        return [numeros.get(i, "") for i in range(1, max(numeros) + 1)]

    def _texto(valores_: list[str]) -> list[dict]:
        return [{"type": "text", "text": str(v or "")} for v in valores_]

    def _tipo_do_botao(indice: int) -> str:
        botoes = (_componente(template, "BUTTONS") or {}).get("buttons") or []
        if indice < len(botoes):
            return (botoes[indice].get("type") or "").upper()
        return ""

    # Ordem header → body → buttons, como a Meta espera e o wacrm faz.
    components: list[dict] = []

    midia = (resolvidos.get("header:media") or "").strip()
    if midia:
        # ⚠️ O `header_handle` do template NÃO serve aqui: ele é o exemplo do
        # momento da CRIAÇÃO, e passá-lo como id faz a Meta recusar (wacrm:
        # "it is NOT a reusable send-time media id"). O que vale é uma URL
        # pública, ou um id de upload real - que ainda não temos.
        # A guarda importa: o gestor pode ter trocado o cabeçalho de imagem
        # para texto na Meta DEPOIS de o nó ter sido configurado, e um mapa
        # velho ainda traria esta chave. Sem ela sairia `{"type": "text_"}`,
        # que a Meta recusa sem dizer por quê.
        formato = formato_do_cabecalho(template)
        if formato in _MIDIAS:
            tipo = formato.lower()
            components.append(
                {
                    "type": "header",
                    "parameters": [{"type": tipo, tipo: {"link": midia}}],
                }
            )

    do_cabecalho = _em_ordem("header:")
    if do_cabecalho:
        components.append({"type": "header", "parameters": _texto(do_cabecalho)})

    do_corpo = _em_ordem("")
    if do_corpo:
        components.append({"type": "body", "parameters": _texto(do_corpo)})

    # ⚠️ Cada botão é um component PRÓPRIO, com `index` e `sub_type`. Foi o
    # que faltou no teste ao vivo: o corpo ia certo e a Meta recusava a
    # mensagem inteira por causa do botão vazio.
    indices = sorted(
        {
            int(chave.split(":")[1])
            for chave in resolvidos
            if chave.startswith("button:")
        }
    )
    for indice in indices:
        do_botao = _em_ordem(f"button:{indice}:")
        if not do_botao:
            continue
        tipo = _tipo_do_botao(indice)
        if tipo == "COPY_CODE":
            components.append(
                {
                    "type": "button",
                    "sub_type": "copy_code",
                    "index": str(indice),
                    "parameters": [
                        {"type": "coupon_code", "coupon_code": do_botao[0]}
                    ],
                }
            )
        elif tipo == "URL":
            components.append(
                {
                    "type": "button",
                    "sub_type": "url",
                    "index": str(indice),
                    "parameters": _texto(do_botao),
                }
            )
        # QUICK_REPLY e PHONE_NUMBER não entram: a Meta não aceita parâmetro
        # neles, e mandar um faz o envio inteiro ser recusado.

    return components or None
