"""
O fluxo de recepção que uma clínica de verdade usaria (F2.6, §4.3.2).

    python manage.py seed_flow_clinica --clinic 3
    python manage.py seed_flow_clinica --clinic 3 --ativar
    python manage.py seed_flow_clinica --clinic 3 --limpar

Difere do `seed_flow_demo` no propósito: aquele traduz o protótipo do cliente
e este é modelado no ATENDIMENTO DA MEDESSENCE como ele existe hoje, lido do
banco em 06/08/2026: **um profissional, atendimento particular**, com consulta
e retorno nas modalidades presencial e online, mais exames e receitas. Por isso
não há escolha de especialidade nem de médico, que seriam perguntas sem
resposta possível, e a lista oferece o TIPO de atendimento.

Cobre nove dos doze nós da v1. Ficaram de fora, de propósito:

- **enviar mídia**: exigiria uma URL de arquivo que a clínica ainda não tem, e
  uma URL quebrada faria o teste falhar por um motivo que não é o do fluxo.
- **enviar template**: só vale FORA da janela de 24h, e o teste acontece dentro
  dela. Testá-lo pede uma conversa parada há mais de um dia.
- **aguardar**: não há passo no atendimento de recepção que peça espera de
  relógio. Enfiá-lo aqui seria nó de teste disfarçado de fluxo.

Nasce em RASCUNHO. Ativar é decisão de quem opera a clínica.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.automation.choices import FlowNodeType, FlowStatus, FlowTrigger
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowVersion
from apps.inbox.models import ConversationLabel
from apps.tenants.models import Clinic

NOME = "Atendimento da recepção"


def _no(node_id, tipo, rotulo, x=0, y=0, **config):
    return {
        "id": node_id,
        "type": tipo,
        "label": rotulo,
        "position": {"x": x, "y": y},
        "config": config,
    }


def _liga(origem, destino, condicao="default"):
    return {"from": origem, "to": destino, "condition": condicao}


def montar_grafo(etiquetas: dict) -> dict:
    """
    O caminho que o paciente percorre:

        Início
          └─ A clínica está aberta agora?
               ├─ SIM  → avisa e entrega para a recepção
               └─ NÃO  → saudação → menu de 3 botões
                          ├─ Marcar consulta → tipo de atendimento (lista)
                          │    → nome → forma de pagamento (botões)
                          │      ├─ Particular      → como funciona
                          │      └─ Tenho convênio  → explica e marca "Convênio"
                          │    → resumo → marca "Agendamento" → recepção
                          ├─ Remarcar → nome → aviso → marca "Reagendamento"
                          │             → recepção
                          └─ Outro assunto → o que precisa
                               ├─ quer retorno → recepção
                               └─ era só isso  → despedida e FIM

    ⚠️ Onde a resposta é um conjunto FECHADO, o paciente escolhe em vez de
    digitar (06/08/2026). A forma de pagamento era pergunta aberta e começava
    com "você tem convênio?": o paciente respondia "Tenho" e a recepção
    recebia `Pagamento: Tenho`. Texto livre fica só para o que é mesmo aberto,
    como o nome e o assunto.

    ⚠️ **A etiqueta vem DEPOIS da fala que informa** e logo antes de entregar
    (06/08/2026, pedido do usuário). Ela é o registro do que aconteceu, então
    marcar antes de o robô falar deixaria etiquetada uma conversa que o
    paciente ainda pode abandonar no meio.

    ⚠️ O primeiro nó pergunta o horário em vez de o FLUXO ser marcado como
    `só fora do horário`. A diferença importa: marcado, ele nem dispararia
    dentro do expediente, e o paciente ficaria sem nenhuma resposta até alguém
    abrir o Inbox. Assim ele sempre responde, e quem decide o que dizer é o
    desenho.
    """
    nodes = [
        _no("inicio", FlowNodeType.START, "Início", 40, 420),
        # --- a bifurcação do expediente -------------------------------- #
        _no(
            "esta_aberta",
            FlowNodeType.CONDITION,
            "A clínica está aberta?",
            300,
            420,
            subject="business_hours",
            operator="present",
        ),
        _no(
            "aviso_aberto",
            FlowNodeType.SEND_MESSAGE,
            "Recepção atendendo",
            620,
            180,
            text=(
                "Olá! 👋\n\nRecebemos a sua mensagem e a recepção já vai falar "
                "com você por aqui."
            ),
        ),
        _no(
            "recepcao_agora",
            FlowNodeType.HANDOFF,
            "Para a recepção",
            940,
            180,
            note="Mensagem recebida dentro do expediente. O paciente está aguardando.",
        ),
        # --- fora do expediente ---------------------------------------- #
        _no(
            "saudacao",
            FlowNodeType.SEND_MESSAGE,
            "Saudação fora do horário",
            620,
            560,
            text=(
                "Olá! 👋 Você falou com a MedEssence fora do nosso horário de "
                "atendimento.\n\nPosso adiantar o seu pedido agora, e a "
                "recepção conclui assim que abrirmos."
            ),
        ),
        _no(
            "menu",
            FlowNodeType.SEND_BUTTONS,
            "Menu",
            940,
            560,
            text="Como posso ajudar você?",
            buttons=[
                {"id": "agendar", "title": "Marcar consulta"},
                {"id": "remarcar", "title": "Remarcar ou cancelar"},
                {"id": "outro", "title": "Outro assunto"},
            ],
        ),
        # --- ramo: marcar consulta ------------------------------------- #
        _no(
            "tipo_atendimento",
            FlowNodeType.SEND_LIST,
            "Tipo de atendimento",
            1280,
            420,
            text="Qual atendimento você precisa?",
            button_label="Ver opções",
            section_title="Atendimentos",
            # A escolha vira variável: sem isto, a recepção recebe "pedido de
            # agendamento" sem saber se é consulta, retorno ou exame.
            var_key="atendimento",
            # Os tipos são os que a clínica realmente pratica, lidos do
            # cadastro de procedimentos.
            rows=[
                {"id": "primeira", "title": "Consulta, primeira vez"},
                {"id": "presencial", "title": "Consulta presencial"},
                {"id": "online", "title": "Consulta online"},
                {"id": "retorno", "title": "Retorno"},
                {"id": "exame", "title": "Exames ou receita"},
            ],
        ),
        _no(
            "coleta_nome",
            FlowNodeType.COLLECT_INPUT,
            "Nome do paciente",
            1620,
            420,
            prompt_text="Certo! Qual o seu nome completo?",
            var_key="nome",
        ),
        _no(
            "forma_pagamento",
            FlowNodeType.SEND_BUTTONS,
            "Forma de pagamento",
            1940,
            420,
            # Era pergunta aberta, e a pergunta começava com "você tem
            # convênio?": o paciente respondia "Tenho" e a recepção recebia
            # `Pagamento: Tenho`. Escolha de conjunto fechado é BOTÃO, e aí
            # não há resposta possível que não sirva.
            text="Obrigado, {{nome}}! Como será o pagamento da consulta?",
            buttons=[
                {"id": "particular", "title": "Particular"},
                {"id": "convenio", "title": "Tenho convênio"},
            ],
            var_key="pagamento",
        ),
        _no(
            "info_particular",
            FlowNodeType.SEND_MESSAGE,
            "Como funciona o particular",
            2260,
            300,
            # Sem valor escrito aqui de propósito: preço muda, e preço errado
            # no WhatsApp vira discussão no balcão.
            text=(
                "Perfeito. O atendimento particular é agendado direto com a "
                "recepção, que passa os valores e as formas de pagamento na "
                "confirmação."
            ),
        ),
        _no(
            "marca_convenio",
            FlowNodeType.SET_LABEL,
            "Marcar Convênio",
            2580,
            540,
            label_id=etiquetas["Convênio"],
        ),
        _no(
            "info_convenio",
            FlowNodeType.SEND_MESSAGE,
            "Sobre o convênio",
            2260,
            540,
            text=(
                "Anotado. A recepção confere a cobertura do seu convênio antes "
                "de confirmar, e avisa se for preciso alguma autorização."
            ),
        ),
        _no(
            "marca_agendamento",
            FlowNodeType.SET_LABEL,
            "Marcar Agendamento",
            3220,
            420,
            label_id=etiquetas["Agendamento"],
        ),
        _no(
            "resumo",
            FlowNodeType.SEND_MESSAGE,
            "Resumo do pedido",
            2900,
            420,
            text=(
                "Tudo certo, {{nome}}! ✅\n\nSeu pedido já está com a nossa "
                "equipe e a recepção confirma o horário assim que abrirmos.\n\n"
                "Se precisar mudar alguma coisa, é só responder por aqui."
            ),
        ),
        _no(
            "recepcao_agenda",
            FlowNodeType.HANDOFF,
            "Agendamento para a recepção",
            3860,
            420,
            note=(
                "Pedido de agendamento fora do horário.\n"
                "Paciente: {{nome}}\n"
                "Atendimento: {{atendimento}}\n"
                "Pagamento: {{pagamento}}"
            ),
        ),
        # --- ramo: remarcar ou cancelar -------------------------------- #
        _no(
            "marca_reagendamento",
            FlowNodeType.SET_LABEL,
            "Marcar Reagendamento",
            2260,
            760,
            label_id=etiquetas["Reagendamento"],
        ),
        _no(
            "coleta_nome_remarcar",
            FlowNodeType.COLLECT_INPUT,
            "Nome para remarcar",
            1280,
            760,
            prompt_text=(
                "Sem problema. Me diga o seu nome completo para eu localizar a "
                "sua consulta."
            ),
            var_key="nome",
        ),
        _no(
            "aviso_remarcar",
            FlowNodeType.SEND_MESSAGE,
            "Aviso de remarcação",
            1620,
            760,
            text=(
                "Obrigado, {{nome}}. A recepção localiza a sua consulta e fala "
                "com você para remarcar."
            ),
        ),
        _no(
            "recepcao_remarcar",
            FlowNodeType.HANDOFF,
            "Remarcação para a recepção",
            2580,
            760,
            note="Pedido de remarcação ou cancelamento. Paciente: {{nome}}.",
        ),
        # --- ramo: outro assunto --------------------------------------- #
        _no(
            "coleta_assunto",
            FlowNodeType.COLLECT_INPUT,
            "Qual o assunto",
            1280,
            980,
            prompt_text=(
                "Claro. Me conte com as suas palavras o que você precisa, que "
                "eu deixo anotado para a recepção."
            ),
            var_key="assunto",
        ),
        _no(
            "precisa_retorno",
            FlowNodeType.SEND_BUTTONS,
            "Precisa de retorno?",
            1620,
            980,
            # Nem toda mensagem precisa de gente. Sem esta pergunta, quem só
            # queria avisar alguma coisa entra na fila da recepção do mesmo
            # jeito, e a fila da manhã abre cheia de conversa que já acabou.
            text=(
                "Anotado! Você quer que a recepção entre em contato sobre "
                "isso?"
            ),
            buttons=[
                {"id": "sim", "title": "Sim, por favor"},
                {"id": "nao", "title": "Não, era só isso"},
            ],
        ),
        _no(
            "aviso_assunto",
            FlowNodeType.SEND_MESSAGE,
            "Aviso do assunto",
            1940,
            900,
            text=(
                "Combinado! A recepção lê a sua mensagem e responde assim que "
                "abrirmos."
            ),
        ),
        _no(
            "recepcao_assunto",
            FlowNodeType.HANDOFF,
            "Assunto para a recepção",
            2260,
            900,
            note="Assunto livre, fora do horário: {{assunto}}",
        ),
        _no(
            "despedida",
            FlowNodeType.SEND_MESSAGE,
            "Despedida",
            1940,
            1120,
            text=(
                "Perfeito! Deixei o seu recado registrado. 🙂\n\nSe precisar de "
                "mais alguma coisa, é só chamar por aqui."
            ),
        ),
        _no(
            "fim",
            FlowNodeType.END,
            "Fim",
            2260,
            1120,
        ),
    ]

    edges = [
        _liga("inicio", "esta_aberta"),
        _liga("esta_aberta", "aviso_aberto", "true"),
        _liga("esta_aberta", "saudacao", "false"),
        _liga("aviso_aberto", "recepcao_agora"),
        _liga("saudacao", "menu"),
        _liga("menu", "tipo_atendimento", "button:agendar"),
        _liga("menu", "coleta_assunto", "button:outro"),
        # Todo tipo de atendimento segue o mesmo caminho: quem decide agenda é
        # a recepção, e o tipo já foi registrado na conversa.
        _liga("tipo_atendimento", "coleta_nome", "row:primeira"),
        _liga("tipo_atendimento", "coleta_nome", "row:presencial"),
        _liga("tipo_atendimento", "coleta_nome", "row:online"),
        _liga("tipo_atendimento", "coleta_nome", "row:retorno"),
        _liga("tipo_atendimento", "coleta_nome", "row:exame"),
        _liga("coleta_nome", "forma_pagamento"),
        _liga("forma_pagamento", "info_particular", "button:particular"),
        _liga("forma_pagamento", "info_convenio", "button:convenio"),
        # Os dois lados se reencontram na etiqueta de agendamento.
        _liga("info_particular", "resumo"),
        # A etiqueta do convênio vem DEPOIS da fala que explica a cobertura.
        _liga("info_convenio", "marca_convenio"),
        _liga("marca_convenio", "resumo"),
        # E a do agendamento é o último passo antes de entregar.
        _liga("resumo", "marca_agendamento"),
        _liga("marca_agendamento", "recepcao_agenda"),
        _liga("menu", "coleta_nome_remarcar", "button:remarcar"),
        _liga("coleta_nome_remarcar", "aviso_remarcar"),
        _liga("aviso_remarcar", "marca_reagendamento"),
        _liga("marca_reagendamento", "recepcao_remarcar"),
        _liga("coleta_assunto", "precisa_retorno"),
        _liga("precisa_retorno", "aviso_assunto", "button:sim"),
        _liga("precisa_retorno", "despedida", "button:nao"),
        _liga("aviso_assunto", "recepcao_assunto"),
        # O único caminho em que o robô resolve sozinho: a conversa volta para
        # a fila sem ninguém precisar abrir.
        _liga("despedida", "fim"),
    ]
    return {"entry_node": "inicio", "nodes": nodes, "edges": edges}


class Command(BaseCommand):
    help = "Semeia o fluxo de recepção modelado no atendimento real da clínica."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument(
            "--ativar",
            action="store_true",
            help="Publica o fluxo (passa pelo mesmo validador da API)",
        )
        parser.add_argument("--limpar", action="store_true", help="Apaga o fluxo semeado")

    @transaction.atomic
    def handle(self, *args, **options):
        clinic = Clinic.objects.filter(pk=options["clinic"]).first()
        if clinic is None:
            raise CommandError(f"Clínica {options['clinic']} não existe.")

        if options["limpar"]:
            apagados, _ = Flow.objects.filter(clinic=clinic, name=NOME).delete()
            self.stdout.write(self.style.WARNING(f"Apagado: {apagados} registro(s)."))
            return

        etiquetas = self._etiquetas(clinic)
        graph = montar_grafo(etiquetas)

        problemas = validate_graph(graph)
        if problemas:
            # Semear grafo inválido daria um fluxo que a tela recusa publicar,
            # e a pessoa descobriria só ao clicar em Publicar.
            raise CommandError("O grafo não passou no validador:\n  " + "\n  ".join(problemas))

        # ⚠️ A POLÍTICA só é escrita quando o fluxo NASCE (corrigido em
        # 06/08/2026). Semear de novo é atualizar o DESENHO, e um
        # `update_or_create` com defaults desfazia em silêncio o gatilho, a
        # prioridade e o fallback que alguém tinha ajustado pela tela. Foi o
        # que aconteceu no teste ao vivo: a palavra-chave configurada na gaveta
        # virou `primeira mensagem` de volta, e o fluxo parou de disparar sem
        # nada no log dizendo por quê.
        flow = Flow.objects.filter(clinic=clinic, name=NOME).first()
        if flow is None:
            flow = Flow.objects.create(
                clinic=clinic,
                name=NOME,
                trigger=FlowTrigger.FIRST_INBOUND,
                trigger_config={},
                # Não é `só fora do horário`: quem decide isso é o primeiro nó
                # do desenho, para o paciente nunca ficar sem resposta.
                only_outside_hours=False,
                priority=5,
                fallback={
                    "max_reprompts": 2,
                    "on_timeout_hours": 12,
                    "on_exhaust": "handoff",
                },
            )
        # A versão herda a clínica do fluxo, e por isso não recebe `clinic`.
        ultima = FlowVersion.objects.filter(flow=flow).order_by("-number").first()
        versao = FlowVersion.objects.create(
            flow=flow,
            number=(ultima.number if ultima else 0) + 1,
            graph=graph,
        )
        flow.current_version = versao
        flow.save(update_fields=["current_version", "updated_at"])

        if options["ativar"]:
            flow.status = FlowStatus.ACTIVE
            flow.activated_at = timezone.now()
            versao.published_at = timezone.now()
            versao.save(update_fields=["published_at"])
            flow.save(update_fields=["status", "activated_at", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"{NOME}: fluxo {flow.pk}, versão {versao.number}, "
                f"{len(graph['nodes'])} passos, status {flow.status}."
            )
        )
        if not clinic.business_hours.exists():
            self.stdout.write(
                self.style.WARNING(
                    "⚠️  A clínica não tem horário de funcionamento cadastrado, "
                    "então ela conta como FECHADA o tempo todo e só o caminho de "
                    "fora do expediente será exercitado. Cadastre pela tela para "
                    "testar os dois lados."
                )
            )

    def _etiquetas(self, clinic) -> dict:
        """
        As etiquetas precisam EXISTIR antes: `set_label` guarda o id, e id de
        etiqueta apagada faria o nó falhar em silêncio no meio do atendimento.
        """
        precisa = ["Agendamento", "Convênio", "Reagendamento"]
        encontradas = {
            nome: ConversationLabel.objects.filter(clinic=clinic, name=nome).values_list(
                "pk", flat=True
            ).first()
            for nome in precisa
        }
        faltando = [nome for nome, pk in encontradas.items() if pk is None]
        if faltando:
            raise CommandError(
                "A clínica não tem estas etiquetas: "
                + ", ".join(faltando)
                + ". Crie no Inbox antes de semear."
            )
        return encontradas
