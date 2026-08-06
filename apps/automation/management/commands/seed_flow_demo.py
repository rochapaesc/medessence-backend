"""
Semeia o fluxo de agendamento (F2.6, §4.3.2) - o mesmo do protótipo que o
cliente aprovou, traduzido para os 12 nós da v1.

    python manage.py seed_flow_demo --clinic 3
    python manage.py seed_flow_demo --clinic 3 --ativar
    python manage.py seed_flow_demo --clinic 3 --limpar

O que MUDA em relação ao protótipo, e por quê:

- **Sem o nó de IA.** Onde ele "interpretava a data que o paciente escreveu",
  aqui há uma LISTA de horários. Não depende da P14 (LGPD) e converte melhor:
  lista não entende errado "quinta que vem de tardezinha".
- **Sem transcrição de áudio** (P14) e **sem requisição HTTP** (P15).
- **Marcar etiqueta usa `ConversationLabel`**, nunca `Tag` - a Tag sincroniza
  com a vSaúde e "agendamento" viraria tag no prontuário (RF-FLW-13.1).

Nasce em RASCUNHO de propósito: ativar é decisão de quem opera a clínica, e um
fluxo ativo responde no lugar dela para todo paciente que escrever. `--ativar`
existe para a calibração, e passa pelo MESMO validador da API.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.automation.choices import FlowNodeType, FlowStatus, FlowTrigger
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowVersion
from apps.inbox.models import ConversationLabel
from apps.tenants.models import Clinic

NOME = "Agendamento (demonstração)"
NOME_COMPLETO = "Validação completa (todos os passos)"
NOME_SIMPLES = "Teste rápido"
ETIQUETA = "Agendamento"


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


def montar_grafo(label_id):
    """
    Início → menu de 3 botões:
      Marcar consulta → especialidade (lista) → horário (lista) → confirma
      Remarcar        → humano
      Falar com gente → humano
    """
    nodes = [
        _no("inicio", FlowNodeType.START, "Início", 40, 240),
        _no(
            "saudacao",
            FlowNodeType.SEND_MESSAGE,
            "Saudação",
            340,
            240,
            text=("Olá! Sou o assistente da clínica. 👋\nPosso ajudar você a marcar sua consulta."),
        ),
        _no(
            "menu",
            FlowNodeType.SEND_BUTTONS,
            "Menu",
            680,
            240,
            text="O que você deseja fazer?",
            buttons=[
                {"id": "agendar", "title": "Marcar consulta"},
                {"id": "remarcar", "title": "Remarcar consulta"},
                {"id": "humano", "title": "Falar com atendente"},
            ],
        ),
        _no(
            "especialidade",
            FlowNodeType.SEND_LIST,
            "Especialidade",
            1040,
            160,
            text="Qual especialidade você procura?",
            button_label="Ver especialidades",
            section_title="Especialidades",
            rows=[
                {"id": "clinico", "title": "Clínico Geral"},
                {"id": "cardio", "title": "Cardiologia"},
                {"id": "derma", "title": "Dermatologia"},
                {"id": "pediatria", "title": "Pediatria"},
            ],
        ),
        _no(
            "horario",
            FlowNodeType.SEND_LIST,
            "Horário",
            1400,
            160,
            # No protótipo, aqui entrava um agente de IA para interpretar a
            # data digitada. A lista resolve o mesmo problema sem mandar
            # conversa de paciente para fora (P14) - e sem errar a data.
            text="Perfeito! Escolha o melhor horário para você:",
            button_label="Ver horários",
            section_title="Horários disponíveis",
            rows=[
                {"id": "manha", "title": "Amanhã de manhã"},
                {"id": "tarde", "title": "Amanhã à tarde"},
                {"id": "outro", "title": "Outro dia"},
            ],
        ),
        _no(
            "marca",
            FlowNodeType.SET_LABEL,
            "Marcar assunto",
            1760,
            120,
            label_id=label_id,
        ),
        _no(
            "confirma",
            FlowNodeType.SEND_MESSAGE,
            "Confirmação",
            2060,
            120,
            text=("Anotado! ✅\nNossa recepção vai confirmar o seu horário em instantes."),
        ),
        _no(
            "para_recepcao",
            FlowNodeType.HANDOFF,
            "Para a recepção",
            2400,
            240,
            note="Pedido de agendamento pelo assistente. Confirmar horário com o paciente.",
        ),
        _no(
            "humano",
            FlowNodeType.HANDOFF,
            "Falar com atendente",
            1040,
            420,
            note="O paciente pediu atendimento humano.",
        ),
    ]
    edges = [
        _liga("inicio", "saudacao"),
        _liga("saudacao", "menu"),
        _liga("menu", "especialidade", "button:agendar"),
        _liga("menu", "humano", "button:remarcar"),
        _liga("menu", "humano", "button:humano"),
        # Qualquer especialidade leva à escolha de horário.
        _liga("especialidade", "horario", "row:clinico"),
        _liga("especialidade", "horario", "row:cardio"),
        _liga("especialidade", "horario", "row:derma"),
        _liga("especialidade", "horario", "row:pediatria"),
        # "Outro dia" é caso de gente: a lista não cobre agenda aberta.
        _liga("horario", "marca", "row:manha"),
        _liga("horario", "marca", "row:tarde"),
        _liga("horario", "humano", "row:outro"),
        _liga("marca", "confirma"),
        _liga("confirma", "para_recepcao"),
    ]
    return {"entry_node": "inicio", "nodes": nodes, "edges": edges}


def montar_grafo_simples(label_id):
    """
    Fluxo pequeno para exercitar o COMPORTAMENTO, e não o catálogo.

    Sete passos que cobrem tudo o que se quer observar numa conversa de
    verdade: o disparo, a escolha por botão, a resposta que não casa (o
    reprompt), a variável coletada aparecendo na mensagem seguinte, a entrega
    ao humano e o fim. Dispara a QUALQUER hora de propósito: depender do
    horário da clínica tornaria o teste imprevisível.

        Início → Saudação → Menu
                              ├─ Agendar → "Qual o seu nome?" → "Prazer, X!" → Fim
                              └─ Atendente → recepção
    """
    nodes = [
        _no("inicio", FlowNodeType.START, "Início", 60, 300),
        _no(
            "saudacao",
            FlowNodeType.SEND_MESSAGE,
            "Saudação",
            360,
            280,
            text="Olá! Aqui é o assistente da clínica. 👋",
        ),
        _no(
            "menu",
            FlowNodeType.SEND_BUTTONS,
            "Menu",
            700,
            260,
            text="O que você prefere?",
            buttons=[
                {"id": "agendar", "title": "Marcar consulta"},
                {"id": "atendente", "title": "Falar com atendente"},
            ],
        ),
        _no(
            "pergunta_nome",
            FlowNodeType.COLLECT_INPUT,
            "Pergunta o nome",
            1060,
            160,
            prompt_text="Certo! Como você se chama?",
            var_key="nome",
        ),
        _no(
            "confirma",
            FlowNodeType.SEND_MESSAGE,
            "Confirmação",
            1400,
            160,
            text="Prazer, {{nome}}! A recepção vai falar com você em breve. ✅",
        ),
        _no("fim", FlowNodeType.END, "Fim", 1740, 180),
        _no(
            "atendente",
            FlowNodeType.HANDOFF,
            "Para a recepção",
            1060,
            440,
            note="O paciente pediu para falar com uma pessoa.",
        ),
    ]
    edges = [
        _liga("inicio", "saudacao"),
        _liga("saudacao", "menu"),
        _liga("menu", "pergunta_nome", "button:agendar"),
        _liga("menu", "atendente", "button:atendente"),
        _liga("pergunta_nome", "confirma"),
        _liga("confirma", "fim"),
    ]
    return {"entry_node": "inicio", "nodes": nodes, "edges": edges}


def montar_grafo_completo(label_id):
    """
    Fluxo de validação: usa os DOZE tipos de nó da v1, cada um pelo menos uma
    vez, com ramificação de verdade.

    Serve para exercitar o canvas inteiro (todo tipo de cartão, toda forma de
    porta) e o motor inteiro numa passada só. Não é o fluxo que a clínica vai
    usar: é o que prova que a tela e o motor aguentam o catálogo completo.

    O desenho:

        Início
          └─ A clínica está aberta?
               ├─ sim → vai direto para a recepção
               └─ não → saudação → menu de 3 botões
                        ├─ Marcar consulta → especialidade (lista)
                        │    └─ pergunta o melhor dia (coleta)
                        │         └─ o dia é hoje?
                        │              ├─ sim → recepção (é urgência)
                        │              └─ não → etiqueta → confirmação
                        │                        └─ espera 1 dia → lembrete → fim
                        ├─ Exames → manda o preparo em PDF → fim
                        └─ Falar com atendente → recepção
    """
    nodes = [
        _no("inicio", FlowNodeType.START, "Início", 40, 380),
        _no(
            "esta_aberta",
            FlowNodeType.CONDITION,
            "A clínica está aberta?",
            320,
            360,
            subject="business_hours",
            operator="present",
        ),
        _no(
            "recepcao_agora",
            FlowNodeType.HANDOFF,
            "Recepção atende",
            660,
            160,
            note="Chegou dentro do horário. A recepção assume.",
        ),
        _no(
            "saudacao",
            FlowNodeType.SEND_MESSAGE,
            "Saudação",
            660,
            460,
            text=(
                "Olá! Nossa recepção não está no momento, mas eu posso "
                "adiantar o seu atendimento. 👋"
            ),
        ),
        _no(
            "menu",
            FlowNodeType.SEND_BUTTONS,
            "Menu",
            1000,
            440,
            text="Com o que posso ajudar?",
            buttons=[
                {"id": "consulta", "title": "Marcar consulta"},
                {"id": "exames", "title": "Preparo de exame"},
                {"id": "humano", "title": "Falar com atendente"},
            ],
        ),
        _no(
            "especialidade",
            FlowNodeType.SEND_LIST,
            "Especialidade",
            1360,
            220,
            text="Qual especialidade você procura?",
            button_label="Ver especialidades",
            section_title="Especialidades",
            rows=[
                {"id": "clinico", "title": "Clínico Geral"},
                {"id": "cardio", "title": "Cardiologia"},
                {"id": "derma", "title": "Dermatologia"},
            ],
        ),
        _no(
            "melhor_dia",
            FlowNodeType.COLLECT_INPUT,
            "Melhor dia",
            1720,
            240,
            prompt_text="Qual o melhor dia para você? Pode escrever com as suas palavras.",
            var_key="dia_preferido",
        ),
        _no(
            "e_hoje",
            FlowNodeType.CONDITION,
            "Pediu para hoje?",
            2060,
            240,
            subject="var",
            subject_key="dia_preferido",
            operator="contains",
            value="hoje",
        ),
        _no(
            "urgencia",
            FlowNodeType.HANDOFF,
            "Pode ser urgência",
            2400,
            120,
            note="O paciente pediu para HOJE. Ligar assim que abrir.",
        ),
        _no(
            "marca",
            FlowNodeType.SET_LABEL,
            "Marcar assunto",
            2400,
            360,
            label_id=label_id,
        ),
        _no(
            "confirma",
            FlowNodeType.SEND_MESSAGE,
            "Confirmação",
            2740,
            360,
            text=("Anotei: {{dia_preferido}}. ✅\nA recepção confirma o horário assim que abrir."),
        ),
        _no("espera", FlowNodeType.WAIT, "Espera 1 dia", 3080, 360, amount=1, unit="days"),
        _no(
            "lembrete",
            FlowNodeType.SEND_TEMPLATE,
            "Lembrete",
            3400,
            360,
            template_name="lembrete_retorno",
        ),
        _no("fim_agendamento", FlowNodeType.END, "Fim", 3740, 360),
        _no(
            "preparo",
            FlowNodeType.SEND_MEDIA,
            "Preparo em PDF",
            1360,
            580,
            media_url="https://exemplo.com/preparo-de-exames.pdf",
            caption="Aqui está o preparo dos exames mais pedidos. 📄",
        ),
        _no("fim_exames", FlowNodeType.END, "Fim", 1720, 600),
        _no(
            "atendente",
            FlowNodeType.HANDOFF,
            "Falar com atendente",
            1360,
            760,
            note="O paciente pediu atendimento humano.",
        ),
    ]
    edges = [
        _liga("inicio", "esta_aberta"),
        _liga("esta_aberta", "recepcao_agora", "true"),
        _liga("esta_aberta", "saudacao", "false"),
        _liga("saudacao", "menu"),
        _liga("menu", "especialidade", "button:consulta"),
        _liga("menu", "preparo", "button:exames"),
        _liga("menu", "atendente", "button:humano"),
        _liga("especialidade", "melhor_dia", "row:clinico"),
        _liga("especialidade", "melhor_dia", "row:cardio"),
        _liga("especialidade", "melhor_dia", "row:derma"),
        _liga("melhor_dia", "e_hoje"),
        _liga("e_hoje", "urgencia", "true"),
        _liga("e_hoje", "marca", "false"),
        _liga("marca", "confirma"),
        _liga("confirma", "espera"),
        _liga("espera", "lembrete"),
        _liga("lembrete", "fim_agendamento"),
        _liga("preparo", "fim_exames"),
    ]
    return {"entry_node": "inicio", "nodes": nodes, "edges": edges}


class Command(BaseCommand):
    help = "Semeia o fluxo de agendamento de demonstração (F2.6)."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument("--ativar", action="store_true", help="Publica o fluxo (valida antes)")
        parser.add_argument("--limpar", action="store_true", help="Remove o fluxo semeado")
        parser.add_argument(
            "--simples",
            action="store_true",
            help="Semeia o fluxo de TESTE RÁPIDO, de sete passos.",
        )
        parser.add_argument(
            "--completo",
            action="store_true",
            help="Semeia o fluxo de VALIDAÇÃO, que usa os doze tipos de passo.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        clinic = Clinic.objects.filter(pk=options["clinic"]).first()
        if not clinic:
            raise CommandError(f"Clínica {options['clinic']} não encontrada.")

        completo = options["completo"]
        simples = options["simples"]
        nome = NOME_SIMPLES if simples else (NOME_COMPLETO if completo else NOME)

        if options["limpar"]:
            # O queryset do projeto faz SOFT delete em massa e devolve a
            # contagem, não a tupla do Django. Aqui o soft delete é o certo:
            # execuções antigas continuam apontando para o fluxo, e apagar de
            # verdade levaria o histórico junto.
            apagados = Flow.objects.filter(clinic=clinic, name=nome).delete()
            self.stdout.write(self.style.SUCCESS(f"Removido: {apagados} fluxo(s)."))
            return

        etiqueta, _ = ConversationLabel.objects.get_or_create(
            clinic=clinic, name=ETIQUETA, defaults={"color": "#12A150"}
        )
        montador = (
            montar_grafo_simples
            if simples
            else (montar_grafo_completo if completo else montar_grafo)
        )
        graph = montador(etiqueta.pk)

        problemas = validate_graph(graph)
        if problemas:
            # O fluxo semeado é o exemplo que o cliente vê primeiro: se ele
            # nasce quebrado, o defeito é meu e não do gestor.
            lista = "\n- ".join(problemas)
            raise CommandError(f"O grafo semeado não passa na validação:\n- {lista}")

        flow, criado = Flow.objects.get_or_create(
            clinic=clinic,
            name=nome,
            defaults={
                "trigger": FlowTrigger.FIRST_INBOUND,
                # O de teste dispara a qualquer hora: depender do horário da
                # clínica tornaria a observação imprevisível. Os outros nascem
                # restritos, que é o desenho certo para uma equipe pequena
                # (RF-FLW-5.1).
                "only_outside_hours": not simples,
                "priority": 10,
            },
        )

        ultima = flow.versions.order_by("-number").first()
        version = FlowVersion.objects.create(
            flow=flow, number=(ultima.number if ultima else 0) + 1, graph=graph
        )
        flow.current_version = version
        campos = ["current_version"]

        if options["ativar"]:
            flow.status = FlowStatus.ACTIVE
            flow.activated_at = timezone.now()
            version.published_at = timezone.now()
            version.save(update_fields=["published_at", "updated_at"])
            campos += ["status", "activated_at"]

        flow.save(update_fields=[*campos, "updated_at"])

        verbo = "Criado" if criado else "Atualizado"
        situacao = "ATIVO" if options["ativar"] else "rascunho"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verbo} '{nome}' na clínica {clinic.name} "
                f"(v{version.number}, {situacao}, {len(graph['nodes'])} nós)."
            )
        )
        if not options["ativar"]:
            self.stdout.write("Para publicar: acrescente --ativar")
        if flow.only_outside_hours and not clinic.business_hours.exists():
            self.stdout.write(
                self.style.WARNING(
                    "A clínica não tem horário de funcionamento cadastrado, então ela está "
                    "SEMPRE fechada para o motor e o fluxo vai atender a qualquer hora. "
                    "Cadastre os horários no admin da clínica."
                )
            )
