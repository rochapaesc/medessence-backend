"""
Um fluxo GRANDE de recepção, para exercitar o modo de teste (RF-FLW-25).

    python manage.py seed_fluxo_de_recepcao --clinic 1
    python manage.py seed_fluxo_de_recepcao --clinic 1 --limpar

Os fluxos que os ensaios de sequência criam são de um nó, porque ali o que
interessa é o calendário. Este é o contrário: dezenove nós e quatro caminhos,
para dar o que conversar ao simulador e fazer cada tipo de nó aparecer.

O que cada caminho exercita:

  MENU        abre com TEMPLATE e pergunta em BOTÕES, porque escolha de
              conjunto fechado é botão e não texto digitado.

  Agendar  →  LISTA do tipo de consulta, COLETA o nome de quem vai ser
              atendido, e uma CONDIÇÃO de expediente decide entre ETIQUETAR e
              TRANSFERIR agora, ou avisar que a recepção retorna. ⚠️ É aqui
              que o simulador mostra a diferença dele: no teste o expediente
              não trava, e ele DIZ isso em vez de fingir que está aberto.

  Dúvida   →  MÍDIA com as orientações, AGUARDA a pessoa ler, e volta a
              perguntar em botões se resolveu.

  Não quero → REMOVE DA SEQUÊNCIA e se despede. ⚠️ No teste este nó é
              ANUNCIADO e nunca executado (RF-FLW-25.4): é a trava que impede
              um ensaio de tirar gente de verdade de uma trilha de verdade.

⚠️ Nasce em RASCUNHO de propósito: o modo de teste conversa com o rascunho, e
publicar é decisão de quem montou.

⚠️ Só roda em clínica com canal FAKE, e o nó de remover aponta para uma trilha
DE TESTE. Sem o ensaio montado, ele avisa em vez de apontar para uma trilha de
verdade.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.automation.choices import (
    ConditionOperator,
    ConditionSubject,
    FlowNodeType,
    FlowStatus,
    FlowTrigger,
)
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowVersion, Sequence
from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel, ConversationLabel, WhatsAppTemplate
from apps.tenants.models import Clinic

FLUXO = "Recepção completa (teste)"
TEMPLATE = "recepcao_teste"
TRILHA_DO_ENSAIO = "Ensaio A: divulgação (teste)"
MIDIA = "https://exemplo.invalido/orientacoes-de-preparo.pdf"


def no(node_id, tipo, label, **config):
    return {"id": node_id, "type": tipo, "label": label, "config": config}


def liga(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


class Command(BaseCommand):
    help = "Cria um fluxo grande de recepção para validar o modo de teste."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument("--limpar", action="store_true", help="Apaga o fluxo.")

    def handle(self, *args, **opts):
        clinic = self._clinica(opts["clinic"])
        if opts["limpar"]:
            apagados = Flow.objects.filter(clinic=clinic, name=FLUXO).hard_delete()
            WhatsAppTemplate.objects.filter(clinic=clinic, name=TEMPLATE).hard_delete()
            self.stdout.write(self.style.SUCCESS(f"Fluxo apagado ({apagados})."))
            return

        self._criar(clinic)

    def _clinica(self, pk):
        try:
            clinic = Clinic.objects.get(pk=pk)
        except Clinic.DoesNotExist:
            raise CommandError(f"Clínica {pk} não existe.")
        # A mesma trava do ensaio: confere o canal que um DISPARO usaria
        # (`is_test=False`), e não "existe algum canal falso" - a clínica real
        # tem o canal do modo de teste e passaria na checagem ingênua.
        canal = Channel.objects.filter(clinic=clinic, is_test=False).first()
        if canal is None:
            raise CommandError(
                f"A clínica {clinic.name} não tem canal de WhatsApp configurado."
            )
        if canal.provider != WhatsAppProviderKind.FAKE:
            raise CommandError(
                f"O canal da clínica {clinic.name} é '{canal.provider}' "
                f"({canal.display_number}), e não um canal de mentira. Este fluxo "
                "é de teste: rode numa clínica de teste."
            )
        return clinic

    @transaction.atomic
    def _criar(self, clinic):
        WhatsAppTemplate.objects.update_or_create(
            clinic=clinic,
            name=TEMPLATE,
            defaults={"category": "UTILITY", "status": "APPROVED", "language": "pt_BR"},
        )
        etiqueta = ConversationLabel.objects.filter(clinic=clinic).first()
        if etiqueta is None:
            etiqueta = ConversationLabel.objects.create(
                clinic=clinic, name="Agendamento (teste)"
            )

        # O nó de remover precisa apontar para uma trilha que EXISTE, e o
        # validador confere. Aponta para a do ensaio, que é de teste; nunca
        # para uma trilha de verdade, que é o que faria um ensaio tirar gente
        # de uma campanha real.
        trilha = Sequence.objects.filter(clinic=clinic, name=TRILHA_DO_ENSAIO).first()
        if trilha is None:
            trilha = Sequence.objects.filter(clinic=clinic, name__icontains="teste").first()
        if trilha is None:
            raise CommandError(
                "Não achei nenhuma sequência de teste nesta clínica para o nó de "
                "remover apontar. Rode antes: manage.py ensaio_de_sequencia "
                f"--clinic {clinic.pk}"
            )

        graph = self._grafo(etiqueta.pk, trilha.pk)
        problemas = validate_graph(graph, clinic)
        if problemas:
            raise CommandError("O grafo saiu inválido: " + " · ".join(problemas))

        flow, _ = Flow.objects.get_or_create(
            clinic=clinic,
            name=FLUXO,
            defaults={"trigger": FlowTrigger.MANUAL, "priority": 50},
        )
        ultima = flow.versions.order_by("-number").first()
        versao = FlowVersion.objects.create(
            flow=flow, number=(ultima.number if ultima else 0) + 1, graph=graph
        )
        flow.current_version = versao
        # Rascunho: o modo de teste conversa com o rascunho (RF-FLW-25), e
        # publicar é decisão de quem montou.
        flow.status = FlowStatus.DRAFT
        flow.save(update_fields=["current_version", "status", "updated_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Fluxo '{FLUXO}' criado em RASCUNHO na clínica {clinic.name}: "
                f"{len(graph['nodes'])} nós, {len(graph['edges'])} ligações."
            )
        )
        self.stdout.write("")
        self.stdout.write("Os quatro caminhos:")
        self.stdout.write("  Agendar dentro do expediente: lista, nome, etiqueta e transferência")
        self.stdout.write("  Agendar fora do expediente: a condição desvia e o fluxo avisa")
        self.stdout.write("  Tenho uma dúvida: mídia, espera e volta a perguntar")
        self.stdout.write("  Não quero receber: remove da trilha e se despede")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("Para testar:"))
        self.stdout.write(
            "  Abra Fluxos, escolha 'Recepção completa (teste)' e use o Testar.\n"
            "  Repare em duas coisas que só o teste faz: ele DIZ que o expediente\n"
            "  não está travando, e no caminho 'não quero' ele ANUNCIA a remoção\n"
            "  da trilha em vez de executar."
        )

    def _grafo(self, label_id, sequence_id):
        nodes = [
            no("inicio", FlowNodeType.START, "Início"),
            no(
                "abertura",
                FlowNodeType.SEND_TEMPLATE,
                "Abertura",
                template_name=TEMPLATE,
                variables={},
            ),
            no(
                "menu",
                FlowNodeType.SEND_BUTTONS,
                "Menu",
                text="Oi! Como posso ajudar você hoje?",
                buttons=[
                    {"id": "agendar", "title": "Quero agendar"},
                    {"id": "duvida", "title": "Tenho uma dúvida"},
                    {"id": "sair", "title": "Não quero receber"},
                ],
            ),
            # ---- caminho de agendar ----
            no(
                "tipo",
                FlowNodeType.SEND_LIST,
                "Tipo de consulta",
                text="Qual é o tipo de atendimento?",
                button_text="Ver opções",
                rows=[
                    {"id": "avaliacao", "title": "Primeira avaliação"},
                    {"id": "retorno", "title": "Retorno"},
                    {"id": "exame", "title": "Exame"},
                ],
            ),
            no(
                "nome",
                FlowNodeType.COLLECT_INPUT,
                "Nome do paciente",
                prompt_text="Qual é o nome de quem vai ser atendido?",
                var_key="nome_do_paciente",
            ),
            no(
                "expediente",
                FlowNodeType.CONDITION,
                "A clínica está aberta?",
                subject=ConditionSubject.BUSINESS_HOURS,
                operator=ConditionOperator.PRESENT,
            ),
            no("etiqueta", FlowNodeType.SET_LABEL, "Marcar agendamento", label_id=label_id),
            no(
                "entrega",
                FlowNodeType.HANDOFF,
                "Passar para a recepção",
                note="Quer agendar. O tipo e o nome estão na conversa.",
            ),
            no(
                "fora_de_hora",
                FlowNodeType.SEND_MESSAGE,
                "Fora do expediente",
                text="Anotei! A recepção retorna no próximo horário de atendimento.",
            ),
            no("fim_fora", FlowNodeType.END, "Fim, fora de hora"),
            # ---- caminho da dúvida ----
            no(
                "orientacoes",
                FlowNodeType.SEND_MEDIA,
                "Orientações",
                media_url=MIDIA,
                caption="Separei as orientações de preparo aqui.",
            ),
            no("espera", FlowNodeType.WAIT, "Dar tempo de ler", amount=2, unit="minutes"),
            no(
                "resolveu",
                FlowNodeType.SEND_BUTTONS,
                "Resolveu?",
                text="Isso respondeu a sua dúvida?",
                buttons=[
                    {"id": "sim", "title": "Sim, obrigada"},
                    {"id": "nao", "title": "Ainda tenho dúvida"},
                ],
            ),
            no(
                "agradece",
                FlowNodeType.SEND_MESSAGE,
                "Agradecer",
                text="Que bom! Qualquer coisa é só chamar por aqui.",
            ),
            no("fim_duvida", FlowNodeType.END, "Fim, dúvida resolvida"),
            no(
                "entrega2",
                FlowNodeType.HANDOFF,
                "Passar a dúvida",
                note="A orientação não resolveu a dúvida.",
            ),
            # ---- caminho de sair ----
            no(
                "sair",
                FlowNodeType.UNENROLL_SEQUENCE,
                "Tirar da trilha",
                sequence_id=sequence_id,
            ),
            no(
                "despedida",
                FlowNodeType.SEND_MESSAGE,
                "Despedida",
                text="Tudo bem, não mando mais convites. Se precisar, é só escrever.",
            ),
            no("fim_saiu", FlowNodeType.END, "Fim, saiu da trilha"),
        ]
        edges = [
            liga("inicio", "abertura"),
            liga("abertura", "menu"),
            liga("menu", "tipo", "button:agendar"),
            liga("menu", "orientacoes", "button:duvida"),
            liga("menu", "sair", "button:sair"),
            liga("tipo", "nome", "row:avaliacao"),
            liga("tipo", "nome", "row:retorno"),
            liga("tipo", "nome", "row:exame"),
            liga("nome", "expediente"),
            liga("expediente", "etiqueta", "true"),
            liga("expediente", "fora_de_hora", "false"),
            liga("etiqueta", "entrega"),
            liga("fora_de_hora", "fim_fora"),
            liga("orientacoes", "espera"),
            liga("espera", "resolveu"),
            liga("resolveu", "agradece", "button:sim"),
            liga("resolveu", "entrega2", "button:nao"),
            liga("agradece", "fim_duvida"),
            liga("sair", "despedida"),
            liga("despedida", "fim_saiu"),
        ]
        return {"entry_node": "inicio", "nodes": nodes, "edges": edges}
