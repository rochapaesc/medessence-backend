"""
Levar um fluxo de uma clínica para outra (RF-FLW-24).

O arquivo é o GRAFO, e o problema todo são as referências que ele carrega: um
nó que aponta para a sequência 12 e a etiqueta 7 não significa nada na clínica
de destino, onde esses números são outra coisa (ou não existem). Guardar id
cruzado é o jeito mais rápido de importar um fluxo que marca a etiqueta errada
no paciente errado.

Por isso a exportação **desreferencia**: id vira NOME, e a importação resolve o
nome do outro lado. O que não existe lá vira pendência declarada, não um
silêncio — o fluxo entra em RASCUNHO com a lista do que falta, no mesmo espírito
do checklist do painel de sequências.

⚠️ Nenhuma das três referências (`chatwoot`, `wacrm`, `whatomate`) exporta
fluxo: as duas que têm canvas guardam o grafo com ids do próprio banco e não
oferecem saída. O desenho abaixo é nosso, e o RF registra o porquê.
"""

from apps.automation.choices import FlowNodeType, FlowStatus, FlowTrigger

# A versão do FORMATO, não do produto. Ela sobe quando o arquivo deixa de ser
# legível por uma versão anterior; acrescentar campo opcional não conta.
FORMATO = 1

# Config que guarda id local → sob qual chave o NOME viaja no arquivo, e de
# qual catálogo ele sai.
#
# ⚠️ **Todo nó novo que aponte para uma linha do banco entra aqui.** Esquecer
# faz o id atravessar cru para a outra clínica, onde o mesmo número é outra
# coisa - foi por isso que este mapa existe.
REFERENCIAS = {
    FlowNodeType.SET_LABEL: ("label_id", "label_name", "etiqueta"),
    FlowNodeType.ENROLL_SEQUENCE: ("sequence_id", "sequence_name", "sequência"),
    FlowNodeType.UNENROLL_SEQUENCE: ("sequence_id", "sequence_name", "sequência"),
    # RF-FLW-16.1: o destino é um cadastro da clínica, e o de lá é outro. Pior
    # do que inútil: um id que existe no destino apontaria para o endpoint de
    # OUTRA clínica, mandando dado de paciente para a empresa errada.
    FlowNodeType.HTTP_REQUEST: ("destination_id", "destination_name", "destino"),
}


def _nomes_de(clinic) -> dict:
    """
    Os catálogos da clínica, numa consulta cada, indexados pela chave de
    config que os usa. Voltar por chave (e não por posição) é o que permite
    acrescentar referência nova sem mexer em quem chama.
    """
    from apps.automation.models import HttpDestination, Sequence
    from apps.inbox.models import ConversationLabel

    return {
        "sequence_id": {
            s.pk: s.name for s in Sequence.objects.filter(clinic=clinic).only("id", "name")
        },
        "label_id": {
            e.pk: e.name
            for e in ConversationLabel.objects.filter(clinic=clinic).only("id", "name")
        },
        "destination_id": {
            d.pk: d.name
            for d in HttpDestination.objects.filter(clinic=clinic).only("id", "name")
        },
    }


def exportar(flow) -> dict:
    """
    O fluxo em um arquivo, pronto para atravessar para outra clínica.

    ⚠️ Sai a versão PUBLICADA quando existe, e o rascunho quando não: é o
    desenho que está atendendo que a outra clínica quer copiar, não o que
    alguém deixou pela metade no canvas.
    """
    versao = flow.current_version or flow.versions.order_by("-number").first()
    grafo = dict(versao.graph) if versao else {"nodes": [], "edges": [], "entry_node": ""}
    catalogos = _nomes_de(flow.clinic)

    nos = []
    for no in grafo.get("nodes") or []:
        no = dict(no)
        config = dict(no.get("config") or {})
        referencia = REFERENCIAS.get(no.get("type"))
        if referencia:
            campo_id, campo_nome, _rotulo = referencia
            catalogo = catalogos[campo_id]
            # O nome ENTRA e o id SAI: id de outra clínica não é só inútil, é
            # perigoso, porque pode existir apontando para outra coisa.
            config[campo_nome] = catalogo.get(config.get(campo_id), "")
            config.pop(campo_id, None)
        no["config"] = config
        nos.append(no)
    grafo["nodes"] = nos

    return {
        "formato": FORMATO,
        "nome": flow.name,
        "gatilho": flow.trigger,
        "gatilho_config": flow.trigger_config,
        "so_fora_do_horario": flow.only_outside_hours,
        "fallback": flow.fallback,
        "grafo": grafo,
        # Só para quem abrir o arquivo entender de onde ele veio. A importação
        # ignora: confiar em nome de clínica para decidir algo seria dar
        # sentido a um campo que qualquer um edita no editor de texto.
        "origem": {"clinica": flow.clinic.name},
    }


class ArquivoInvalido(ValueError):
    """O arquivo não é um fluxo exportado por aqui."""


def importar(clinic, arquivo: dict, *, nome: str = "") -> tuple:
    """
    Cria o fluxo na clínica de destino. Devolve `(flow, pendencias)`.

    ⚠️ Nasce sempre em RASCUNHO, e isso não é conservadorismo: o arquivo pode
    apontar para uma sequência que não existe aqui, e publicar sozinho poria no
    ar um fluxo que fala com paciente sem alguém ter olhado. As pendências são
    devolvidas para a tela dizer o que falta.
    """
    from apps.automation.models import Flow, FlowVersion, HttpDestination, Sequence
    from apps.inbox.models import ConversationLabel

    if not isinstance(arquivo, dict) or "grafo" not in arquivo:
        raise ArquivoInvalido(
            "Este arquivo não parece um fluxo exportado do MedEssence."
        )
    if arquivo.get("formato") != FORMATO:
        raise ArquivoInvalido(
            f"Este arquivo é da versão {arquivo.get('formato')}, e esta versão "
            f"do sistema lê a {FORMATO}."
        )

    grafo = dict(arquivo.get("grafo") or {})
    pendencias: list[str] = []

    # Resolve por nome, sem diferenciar maiúscula: quem cadastrou "Reclamação"
    # de um lado e "reclamação" do outro quis dizer a mesma coisa.
    catalogos = {
        "sequence_id": {
            s.name.casefold(): s.pk
            for s in Sequence.objects.filter(clinic=clinic).only("id", "name")
        },
        "label_id": {
            e.name.casefold(): e.pk
            for e in ConversationLabel.objects.filter(clinic=clinic).only("id", "name")
        },
        "destination_id": {
            d.name.casefold(): d.pk
            for d in HttpDestination.objects.filter(clinic=clinic).only("id", "name")
        },
    }

    nos = []
    for no in grafo.get("nodes") or []:
        no = dict(no)
        config = dict(no.get("config") or {})
        referencia = REFERENCIAS.get(no.get("type"))
        if referencia:
            campo_id, campo_nome, rotulo = referencia
            procurado = (config.pop(campo_nome, "") or "").strip()
            achado = catalogos[campo_id].get(procurado.casefold())
            if achado:
                config[campo_id] = achado
            else:
                # O nó FICA, com o campo vazio: apagá-lo mudaria o desenho por
                # baixo dos panos, e o canvas já sabe mostrar nó pendente.
                config.pop(campo_id, None)
                pendencias.append(
                    f'O passo "{no.get("label") or no.get("id")}" usa a {rotulo} '
                    f'"{procurado}", que não existe nesta clínica.'
                )
        no["config"] = config
        nos.append(no)
    grafo["nodes"] = nos

    # O template viaja por NOME desde sempre (é assim que a Meta o identifica),
    # então ele não precisa de tradução — mas precisa existir aprovado lá, e a
    # clínica de destino tem outra conta.
    for no in nos:
        if no.get("type") == FlowNodeType.SEND_TEMPLATE:
            modelo = ((no.get("config") or {}).get("template_name") or "").strip()
            if modelo:
                pendencias.append(
                    f'O passo "{no.get("label") or no.get("id")}" envia o modelo '
                    f'"{modelo}". Confira se ele está aprovado na conta desta clínica.'
                )

    flow = Flow.objects.create(
        clinic=clinic,
        name=(nome or arquivo.get("nome") or "Fluxo importado").strip()[:120],
        status=FlowStatus.DRAFT,
        trigger=arquivo.get("gatilho") or FlowTrigger.FIRST_INBOUND,
        trigger_config=arquivo.get("gatilho_config") or {},
        only_outside_hours=bool(arquivo.get("so_fora_do_horario")),
        fallback=arquivo.get("fallback") or {},
    )
    FlowVersion.objects.create(flow=flow, number=1, graph=grafo)
    return flow, pendencias
