"""
Os atalhos que a tela de sequências usa para não obrigar a clínica a montar
fluxo no canvas nem trilha do zero (RF-SEQ-1.2 e RF-SEQ-12).

Duas coisas moram aqui e são parentes: as duas existem para pagar o custo de
uma decisão de desenho. O passo só dispara FLUXO, então um lembrete simples
precisaria de um fluxo montado à mão; e uma trilha nasce vazia, então a tela
em branco não ensina o que é uma sequência.
"""

from django.db import transaction
from django.utils import timezone

from apps.automation.choices import FlowNodeType, FlowStatus, FlowTrigger

# Os modelos de trilha oferecidos na criação (RF-SEQ-12).
#
# ⚠️ Eles trazem a FORMA (prazos e nomes) e NÃO a mensagem: o texto depende de
# template aprovado na conta da clínica, que só ela tem. Prometer mensagem
# pronta criaria passo que nunca sai, que é exatamente o defeito que os avisos
# do editor existem para evitar.
MODELOS = {
    "pos_consulta": {
        "nome": "Pós-consulta",
        "descricao": "Da confirmação da véspera ao convite de retorno.",
        "marketing": False,
        "por_consulta": True,
        "passos": [
            ("Confirmação de presença", -1, "08:00",
             "Olá! Amanhã é a sua consulta. Podemos confirmar a sua presença?"),
            ("Orientações do protocolo", 0, "07:00",
             "Bom dia! Hoje é o dia da sua consulta. Chegue 10 minutos antes e "
             "traga um documento com foto."),
            ("Avaliação no Google", 1, "10:00",
             "Como foi a sua consulta ontem? Se puder, deixe uma avaliação para "
             "a clínica. Ajuda muito!"),
            ("Feedback de resultados", 15, "10:00",
             "Já faz duas semanas da sua consulta. Como você está se sentindo "
             "com o tratamento?"),
            ("Convite de retorno", 55, "09:00",
             "Está chegando a hora do seu retorno. Quer que eu já veja um "
             "horário para você?"),
        ],
    },
    "resgate": {
        "nome": "Resgate de inativos",
        "descricao": "Três tentativas espaçadas para quem não volta há um tempo.",
        "marketing": True,
        "por_consulta": False,
        "passos": [
            ("Primeiro convite", 0, "09:00",
             "Olá! Já faz um tempo desde a sua última consulta. Que tal "
             "agendar uma avaliação?"),
            ("Segunda tentativa", 7, "09:00",
             "Passando de novo por aqui. Temos horários abertos esta semana. "
             "Posso reservar um para você?"),
            ("Última tentativa", 21, "09:00",
             "Última chamada: sua saúde merece atenção. Se quiser voltar, é "
             "só responder esta mensagem."),
        ],
    },
    "pre_consulta": {
        "nome": "Pré-consulta",
        "descricao": "Preparo e orientações antes de quem já tem hora marcada.",
        "marketing": False,
        "por_consulta": True,
        "passos": [
            ("Preparo do exame", -3, "09:00",
             "Sua consulta está chegando. Confira o preparo: jejum de 8 horas "
             "e traga os exames anteriores."),
            ("Lembrete da véspera", -1, "18:00",
             "Amanhã é a sua consulta. Qualquer imprevisto, responda por aqui "
             "que a recepção te ajuda."),
        ],
    },
    "aniversario": {
        "nome": "Aniversário",
        "descricao": "Uma mensagem no dia, para quem entra por um fluxo.",
        "marketing": True,
        "por_consulta": False,
        "passos": [
            ("Parabéns", 0, "09:00",
             "Feliz aniversário! A equipe da clínica deseja um ano cheio de "
             "saúde para você."),
        ],
    },
}


def catalogo() -> list[dict]:
    """Os modelos, como a tela de criação os mostra."""
    return [
        {
            "slug": slug,
            "nome": m["nome"],
            "descricao": m["descricao"],
            "marketing": m["marketing"],
            "por_consulta": m["por_consulta"],
            "passos": [
                {"nome": nome, "offset_days": offset, "send_time": hora}
                for nome, offset, hora, _mensagem in m["passos"]
            ],
        }
        for slug, m in MODELOS.items()
    ]


def aplicar_modelo(sequence, slug: str) -> int:
    """
    Cria os passos de um modelo numa sequência recém-nascida.

    Cada passo nasce apontando para um fluxo EM RASCUNHO que **abre com
    modelo aprovado** (nó de template, ainda sem template escolhido), porque
    TODA conversa que a sequência inicia sai de template (18/08, correção do
    usuário): o público de campanha está fora da janela de 24h, e texto livre
    ali fica segurado para sempre. É o passo 1 do assistente do wacrm e o
    `template_params` obrigatório do Chatwoot.

    O texto do modelo não morre: vai em `suggested_body`, a sugestão de corpo
    que a clínica leva para aprovar na Meta e que o editor mostra. Fluxo em
    rascunho continua: publicar é decisão de quem escolheu o template.
    """
    from apps.automation.models import Flow, FlowVersion, SequenceStep

    modelo = MODELOS.get(slug)
    if modelo is None:
        return 0

    criados = 0
    with transaction.atomic():
        for ordem, (nome, offset, hora, mensagem) in enumerate(
            modelo["passos"], start=1
        ):
            rascunho = Flow.objects.create(
                clinic=sequence.clinic,
                name=f"{sequence.name}: {nome}",
                trigger=FlowTrigger.MANUAL,
                priority=50,
            )
            versao = FlowVersion.objects.create(
                flow=rascunho,
                number=1,
                graph={
                    "entry_node": "inicio",
                    "nodes": [
                        {"id": "inicio", "type": "start", "label": "Início", "config": {}},
                        {
                            "id": "fala",
                            # ⚠️ Template, nunca texto: quem entra por campanha
                            # está fora da janela, e o motor seguraria o texto
                            # para sempre (RF-SEQ-5.3).
                            "type": "send_template",
                            "label": nome,
                            "config": {
                                "template_name": "",
                                "variables": {},
                                "suggested_body": mensagem,
                            },
                        },
                        {"id": "fim", "type": "end", "label": "Fim", "config": {}},
                    ],
                    "edges": [
                        {"from": "inicio", "to": "fala", "condition": "default"},
                        {"from": "fala", "to": "fim", "condition": "default"},
                    ],
                },
            )
            rascunho.current_version = versao
            rascunho.save(update_fields=["current_version"])
            SequenceStep.objects.create(
                sequence=sequence,
                order=ordem,
                name=nome,
                offset_days=offset,
                send_time=hora,
                flow=rascunho,
            )
            criados += 1
    return criados


def criar_fluxo_de_aviso(clinic, *, nome: str, template_name: str, variables: dict, flow=None):
    """
    Cria (ou atualiza) um fluxo de UM nó que manda um modelo aprovado, e o
    publica (RF-SEQ-1.2).

    É o que paga o custo da decisão "o passo só dispara fluxo": sem isto a
    clínica abriria o canvas para cada lembrete simples. Quem quiser
    transformar o aviso em conversa depois é só abrir o mesmo fluxo lá.

    ⚠️ Publica direto, sem passar pelo canvas, mas NÃO sem validação: usa o
    mesmo `validate_graph` da API, porque um nó de template com variável
    faltando é recusado pela Meta na hora do envio, com o paciente do outro
    lado esperando.
    """
    from apps.automation.graph import validate_graph
    from apps.automation.models import Flow, FlowVersion

    # ⚠️ TRÊS nós, e não um: o validador cobra nó de início e toda saída
    # ligada, e ele está certo - fluxo sem fim é fluxo que gira. Para quem
    # monta continua sendo "um aviso", porque só um deles fala.
    graph = {
        "entry_node": "inicio",
        "nodes": [
            {"id": "inicio", "type": FlowNodeType.START, "label": "Início", "config": {}},
            {
                "id": "aviso",
                "type": FlowNodeType.SEND_TEMPLATE,
                "label": nome,
                "config": {"template_name": template_name, "variables": variables or {}},
            },
            {"id": "fim", "type": FlowNodeType.END, "label": "Fim", "config": {}},
        ],
        "edges": [
            {"from": "inicio", "to": "aviso", "condition": "default"},
            {"from": "aviso", "to": "fim", "condition": "default"},
        ],
    }
    problemas = validate_graph(graph, clinic)
    if problemas:
        raise ValueError(problemas)

    with transaction.atomic():
        if flow is None:
            flow = Flow.objects.create(
                clinic=clinic, name=nome, trigger=FlowTrigger.MANUAL, priority=50
            )
        ultima = flow.versions.order_by("-number").first()
        version = FlowVersion.objects.create(
            flow=flow,
            number=(ultima.number if ultima else 0) + 1,
            graph=graph,
            published_at=timezone.now(),
        )
        flow.current_version = version
        flow.status = FlowStatus.ACTIVE
        flow.activated_at = flow.activated_at or timezone.now()
        flow.name = nome
        flow.save(
            update_fields=["current_version", "status", "activated_at", "name", "updated_at"]
        )
    return flow
