"""
Fluxo de AGENDAMENTO e CADASTRO, ativo e trancado por palavra-chave.

    python manage.py seed_fluxo_de_agendamento --clinic 3
    python manage.py seed_fluxo_de_agendamento --clinic 3 --palavra "agendar teste"
    python manage.py seed_fluxo_de_agendamento --clinic 3 --limpar

Simula o caminho inteiro de quem escreve para a clínica querendo marcar: separa
quem já é paciente de quem nunca veio, faz a ficha de quem é novo, escolhe o
atendimento e o período, confere tudo em voz alta e entrega para a recepção com
os dados na conversa.

⚠️ **Ele fica ATIVO numa clínica de verdade**, e por isso o gatilho é
**palavra-chave EXATA**: só entra quem escrever exatamente a frase combinada.
Paciente que mandar "oi", "quero marcar" ou qualquer outra coisa não cai nele.
A palavra some da conversa depois? Não: ela fica escrita, então escolha uma que
não constranja ninguém que leia a conversa depois.

⚠️ **O fluxo NÃO cria a consulta nem a ficha no sistema.** Nenhum nó faz isso
hoje, e inventar um seria escrever no prontuário a partir de texto de WhatsApp.
Ele COLETA e ENTREGA à recepção, com tudo anotado na conversa, que é o que a
clínica faz hoje na mão.

⚠️ **Não pede CPF nem documento.** Dado de identificação por WhatsApp é assunto
de LGPD (P14) e não entra num ensaio. Nome, nascimento e convênio bastam para a
recepção achar ou abrir a ficha.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.automation.choices import FlowNodeType, FlowStatus, FlowTrigger
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowVersion
from apps.inbox.models import ConversationLabel
from apps.tenants.models import Clinic

FLUXO = "Agendamento e cadastro (teste)"
PALAVRA = "agendar teste"


def no(node_id, tipo, label, **config):
    return {"id": node_id, "type": tipo, "label": label, "config": config}


def liga(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


class Command(BaseCommand):
    help = "Cria o fluxo de agendamento e cadastro, ativo e com palavra-chave."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True)
        parser.add_argument(
            "--palavra",
            default=PALAVRA,
            help=f"A frase EXATA que abre o fluxo (padrão: {PALAVRA!r}).",
        )
        parser.add_argument("--limpar", action="store_true")

    def handle(self, *args, **opts):
        try:
            clinic = Clinic.objects.get(pk=opts["clinic"])
        except Clinic.DoesNotExist:
            raise CommandError(f"Clínica {opts['clinic']} não existe.")

        if opts["limpar"]:
            n = Flow.objects.filter(clinic=clinic, name=FLUXO).hard_delete()
            self.stdout.write(self.style.SUCCESS(f"Fluxo apagado ({n})."))
            return

        self._criar(clinic, opts["palavra"].strip())

    @transaction.atomic
    def _criar(self, clinic, palavra):
        if not palavra:
            raise CommandError("A palavra-chave não pode ser vazia: sem ela, o fluxo abre para todo mundo.")

        etiqueta = ConversationLabel.objects.filter(
            clinic=clinic, name__icontains="agendamento"
        ).first() or ConversationLabel.objects.filter(clinic=clinic).first()
        if etiqueta is None:
            etiqueta = ConversationLabel.objects.create(
                clinic=clinic, name="Agendamento"
            )

        graph = self._grafo(etiqueta.pk)
        problemas = validate_graph(graph, clinic)
        if problemas:
            raise CommandError("O grafo saiu inválido: " + " · ".join(problemas))

        flow, _ = Flow.objects.get_or_create(clinic=clinic, name=FLUXO)
        ultima = flow.versions.order_by("-number").first()
        versao = FlowVersion.objects.create(
            flow=flow,
            number=(ultima.number if ultima else 0) + 1,
            graph=graph,
            published_at=timezone.now(),
        )
        flow.current_version = versao
        flow.status = FlowStatus.ACTIVE
        flow.activated_at = flow.activated_at or timezone.now()
        flow.trigger = FlowTrigger.KEYWORD
        # EXATO, e não "contém": com "contém", a frase dentro de qualquer
        # mensagem abriria o fluxo para um paciente de verdade.
        flow.trigger_config = {"keywords": [palavra], "match": "exact"}
        flow.priority = 10
        flow.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Fluxo '{FLUXO}' ATIVO na clínica {clinic.name} "
                f"({len(graph['nodes'])} nós, {len(graph['edges'])} ligações)."
            )
        )
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f'  A palavra que abre: "{palavra}"'))
        self.stdout.write(
            "  Casamento EXATO: a mensagem precisa ser só isso, sem mais nada.\n"
            "  Qualquer outra coisa que o paciente escrever não entra no fluxo."
        )
        self.stdout.write("")
        self.stdout.write("  O que ele pergunta, na ordem:")
        self.stdout.write("    1. já é paciente ou é a primeira vez")
        self.stdout.write("    2. primeira vez: nome completo, nascimento e forma de pagamento")
        self.stdout.write("    3. qual atendimento (lista) e qual período (botões)")
        self.stdout.write("    4. repete tudo para conferir e pede confirmação")
        self.stdout.write("    5. marca a etiqueta e entrega para a recepção")
        self.stdout.write("")
        self.stdout.write(
            "  ⚠️ Ele NÃO cria a consulta nem a ficha: coleta e entrega, que é o\n"
            "     que a recepção faz hoje na mão. E não pede CPF, de propósito."
        )

    def _grafo(self, label_id):
        nodes = [
            no("inicio", FlowNodeType.START, "Início"),
            no(
                "saudacao",
                FlowNodeType.SEND_MESSAGE,
                "Saudação",
                text=(
                    "Olá! Sou o assistente do Instituto MedEssence. "
                    "Vou te ajudar a marcar sua consulta, é rapidinho."
                ),
            ),
            no(
                "ja_e_paciente",
                FlowNodeType.SEND_BUTTONS,
                "Já é paciente?",
                text="Você já foi atendido aqui alguma vez?",
                var_key="ja_e_paciente",
                buttons=[
                    {"id": "ja_sou", "title": "Já sou paciente"},
                    {"id": "primeira_vez", "title": "É a primeira vez"},
                ],
            ),
            # ---- quem já é paciente ----
            no(
                "nome_busca",
                FlowNodeType.COLLECT_INPUT,
                "Nome para localizar",
                prompt_text="Me diga seu nome completo para eu localizar seu cadastro.",
                var_key="nome_completo",
            ),
            # ---- quem é novo: a ficha ----
            no(
                "aviso_cadastro",
                FlowNodeType.SEND_MESSAGE,
                "Aviso do cadastro",
                text=(
                    "Que bom ter você com a gente! Vou anotar três informações "
                    "para a recepção abrir a sua ficha."
                ),
            ),
            no(
                "nome_novo",
                FlowNodeType.COLLECT_INPUT,
                "Nome completo",
                prompt_text="Qual é o seu nome completo?",
                var_key="nome_completo",
            ),
            no(
                "nascimento",
                FlowNodeType.COLLECT_INPUT,
                "Data de nascimento",
                prompt_text="Qual é a sua data de nascimento? Pode escrever assim: 25/12/1990.",
                var_key="data_nascimento",
            ),
            no(
                "pagamento",
                FlowNodeType.SEND_BUTTONS,
                "Particular ou convênio",
                text="O atendimento vai ser particular ou por convênio?",
                var_key="forma_de_pagamento",
                buttons=[
                    {"id": "particular", "title": "Particular"},
                    {"id": "convenio", "title": "Convênio"},
                ],
            ),
            no(
                "qual_convenio",
                FlowNodeType.COLLECT_INPUT,
                "Qual convênio",
                prompt_text="Qual é o convênio e o número da carteirinha?",
                var_key="convenio",
            ),
            # ---- o que os dois caminhos têm em comum ----
            no(
                "atendimento",
                FlowNodeType.SEND_LIST,
                "Tipo de atendimento",
                text="Qual atendimento você precisa?",
                button_text="Ver os atendimentos",
                var_key="atendimento",
                rows=[
                    {"id": "clinico", "title": "Consulta clínica"},
                    {"id": "retorno", "title": "Retorno de consulta"},
                    {"id": "exame", "title": "Exame ou procedimento"},
                ],
            ),
            no(
                "periodo",
                FlowNodeType.SEND_BUTTONS,
                "Período",
                text="Qual período fica melhor para você?",
                var_key="periodo",
                buttons=[
                    {"id": "manha", "title": "De manhã"},
                    {"id": "tarde", "title": "À tarde"},
                    {"id": "qualquer", "title": "Tanto faz"},
                ],
            ),
            no(
                "confere",
                FlowNodeType.SEND_MESSAGE,
                "Conferir",
                text=(
                    "Deixa eu conferir com você:\n\n"
                    "Nome: {{nome_completo}}\n"
                    "Atendimento: {{atendimento}}\n"
                    "Período: {{periodo}}\n"
                    "Pagamento: {{forma_de_pagamento}}"
                ),
            ),
            no(
                "confirma",
                FlowNodeType.SEND_BUTTONS,
                "Está certo?",
                text="Está tudo certo?",
                buttons=[
                    {"id": "confirmo", "title": "Está certo"},
                    {"id": "corrigir", "title": "Preciso corrigir"},
                ],
            ),
            no("etiqueta", FlowNodeType.SET_LABEL, "Marcar agendamento", label_id=label_id),
            no(
                "entrega",
                FlowNodeType.HANDOFF,
                "Passar para a recepção",
                note=(
                    "Pedido de agendamento pelo WhatsApp. Nome: {{nome_completo}} · "
                    "Nascimento: {{data_nascimento}} · Atendimento: {{atendimento}} · "
                    "Período: {{periodo}} · Pagamento: {{forma_de_pagamento}} "
                    "{{convenio}}"
                ),
            ),
            no(
                "entrega_corrigir",
                FlowNodeType.HANDOFF,
                "Corrigir com a recepção",
                note=(
                    "O paciente pediu para corrigir os dados do agendamento. "
                    "O que ele informou está na conversa acima."
                ),
            ),
        ]
        edges = [
            liga("inicio", "saudacao"),
            liga("saudacao", "ja_e_paciente"),
            liga("ja_e_paciente", "nome_busca", "button:ja_sou"),
            liga("ja_e_paciente", "aviso_cadastro", "button:primeira_vez"),
            liga("nome_busca", "atendimento"),
            liga("aviso_cadastro", "nome_novo"),
            liga("nome_novo", "nascimento"),
            liga("nascimento", "pagamento"),
            liga("pagamento", "atendimento", "button:particular"),
            liga("pagamento", "qual_convenio", "button:convenio"),
            liga("qual_convenio", "atendimento"),
            liga("atendimento", "periodo", "row:clinico"),
            liga("atendimento", "periodo", "row:retorno"),
            liga("atendimento", "periodo", "row:exame"),
            liga("periodo", "confere", "button:manha"),
            liga("periodo", "confere", "button:tarde"),
            liga("periodo", "confere", "button:qualquer"),
            liga("confere", "confirma"),
            liga("confirma", "etiqueta", "button:confirmo"),
            liga("confirma", "entrega_corrigir", "button:corrigir"),
            liga("etiqueta", "entrega"),
        ]
        return {"entry_node": "inicio", "nodes": nodes, "edges": edges}
