from datetime import time

import pytest
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError
from django.utils import timezone

from apps.automation.choices import FlowRunStatus
from apps.automation.models import Flow, FlowRun, FlowVersion
from apps.automation.tests.conftest import make_contact, make_flow
from apps.tenants.models import ClinicBusinessHours, Weekday

pytestmark = pytest.mark.django_db


def make_run(flow, contact, *, status=FlowRunStatus.ACTIVE):
    return FlowRun.objects.create(
        clinic=flow.clinic,
        flow=flow,
        version=flow.current_version,
        contact=contact,
        status=status,
    )


class TestTravaDeExecucaoUnica:
    """
    RF-FLW-6. A trava é do BANCO porque duas entregas do mesmo webhook são o
    caso normal, e um `if not exists` no Python perde essa corrida.
    """

    def test_segunda_execucao_ativa_para_o_mesmo_contato_e_recusada(self, flow_a, contact_a):
        make_run(flow_a, contact_a)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_run(flow_a, contact_a)

    def test_a_trava_vale_entre_fluxos_diferentes_da_mesma_clinica(self, clinic_a, contact_a):
        """
        O contato é um só, e ele não pode estar em dois fluxos ao mesmo
        tempo - senão recebe duas conversas de robô em paralelo.
        """
        primeiro = make_flow(clinic_a, name="Agendamento")
        segundo = make_flow(clinic_a, name="Orçamento")
        make_run(primeiro, contact_a)

        with pytest.raises(IntegrityError), transaction.atomic():
            make_run(segundo, contact_a)

    @pytest.mark.parametrize(
        "status_terminal",
        [
            FlowRunStatus.COMPLETED,
            FlowRunStatus.HANDED_OFF,
            FlowRunStatus.TIMED_OUT,
            FlowRunStatus.PAUSED_BY_AGENT,
            FlowRunStatus.FAILED,
        ],
    )
    def test_execucao_encerrada_libera_o_contato(self, flow_a, contact_a, status_terminal):
        """Terminada não ocupa a vaga: o paciente pode entrar num fluxo de novo."""
        make_run(flow_a, contact_a, status=status_terminal)

        nova = make_run(flow_a, contact_a)

        assert nova.pk

    def test_contatos_diferentes_nao_se_atrapalham(self, flow_a, clinic_a, contact_a):
        outro = make_contact(clinic_a, wa_id="5585900000099")
        make_run(flow_a, contact_a)

        assert make_run(flow_a, outro).pk

    def test_a_trava_nao_vaza_entre_clinicas(self, clinic_a, clinic_b):
        """
        Mesmo número em duas clínicas são dois `Contact` distintos, e uma
        clínica não pode travar a execução da outra.
        """
        fluxo_a = make_flow(clinic_a)
        fluxo_b = make_flow(clinic_b)
        make_run(fluxo_a, make_contact(clinic_a, wa_id="5585911112222"))

        assert make_run(fluxo_b, make_contact(clinic_b, wa_id="5585911112222")).pk


class TestNomeDoFluxo:
    def test_nome_repetido_na_mesma_clinica_e_recusado(self, clinic_a):
        make_flow(clinic_a, name="Agendamento")

        with pytest.raises(IntegrityError), transaction.atomic():
            Flow.objects.create(clinic=clinic_a, name="Agendamento")

    def test_o_mesmo_nome_em_outra_clinica_passa(self, clinic_a, clinic_b):
        make_flow(clinic_a, name="Agendamento")

        assert make_flow(clinic_b, name="Agendamento").pk

    def test_nome_liberado_depois_do_soft_delete(self, clinic_a):
        """
        O soft delete do projeto guarda a linha; a unicidade tem condição de
        `deleted_at` justamente para o nome não ficar preso a um fluxo morto.
        """
        make_flow(clinic_a, name="Agendamento").delete()

        assert make_flow(clinic_a, name="Agendamento").pk


class TestVersaoDoFluxo:
    def test_numero_de_versao_e_unico_por_fluxo(self, flow_a):
        with pytest.raises(IntegrityError), transaction.atomic():
            FlowVersion.objects.create(flow=flow_a, number=1)

    def test_fluxos_diferentes_tem_cada_um_a_sua_versao_1(self, clinic_a):
        make_flow(clinic_a, name="Agendamento")

        assert make_flow(clinic_a, name="Orçamento").current_version.number == 1

    def test_a_execucao_se_prende_a_versao_em_que_comecou(self, flow_a, contact_a):
        """
        RF-FLW-1.1 - o ponto que motivou o grafo versionado: editar o fluxo
        às 14h não pode mudar o desenho debaixo de quem parou no nó 5 às 11h.
        """
        run = make_run(flow_a, contact_a)
        v1 = flow_a.current_version

        v2 = FlowVersion.objects.create(flow=flow_a, number=2, published_at=timezone.now())
        flow_a.current_version = v2
        flow_a.save(update_fields=["current_version"])

        run.refresh_from_db()
        assert run.version_id == v1.pk

    def test_versao_em_uso_nao_pode_ser_apagada(self, flow_a, contact_a):
        """
        `on_delete=RESTRICT` - apagar a versão de uma execução em voo a
        deixaria sem desenho para continuar.
        """
        make_run(flow_a, contact_a)

        with pytest.raises(RestrictedError), transaction.atomic():
            flow_a.current_version.hard_delete()


class TestPoliticaDeFallback:
    def test_fluxo_novo_ja_nasce_com_politica(self, flow_a):
        """
        Não pode ser `default=dict`: fluxo sem política nenhuma abandonaria a
        conversa em silêncio, que é o que o Inbox existe para impedir.
        """
        from apps.automation.models.flow import (
            GOODBYE_REPROMPT,
            GOODBYE_TIMEOUT,
            MAX_BOT_MESSAGES,
        )

        assert flow_a.fallback == {
            "max_reprompts": 2,
            # ⚠️ 20, e não 24: o timeout conta do último avanço da EXECUÇÃO e a
            # janela da Meta conta da última fala do PACIENTE. Com 24 a
            # despedida caía fora da janela e só sairia como template pago
            # (RF-FLW-11.3).
            "on_timeout_hours": 20,
            "on_exhaust": "handoff",
            "goodbye_reprompt": GOODBYE_REPROMPT,
            "goodbye_timeout": GOODBYE_TIMEOUT,
            # Trava de laço com outro robô (RF-FLW-23.1).
            "max_bot_messages": MAX_BOT_MESSAGES,
        }

    def test_a_politica_nao_e_compartilhada_entre_fluxos(self, clinic_a):
        """Default mutável em Django é o clássico: um dict para todo mundo."""
        primeiro = make_flow(clinic_a, name="Agendamento")
        segundo = make_flow(clinic_a, name="Orçamento")

        primeiro.fallback["max_reprompts"] = 9

        assert segundo.fallback["max_reprompts"] == 2


class TestHorarioDeFuncionamento:
    def test_o_mesmo_dia_aceita_MAIS_DE_UMA_faixa(self, clinic_a):
        """
        A clínica que fecha para o almoço (05/08/2026). Antes havia uma
        restrição de um registro por dia, e ela obrigava essa clínica a
        cadastrar 08:00 às 18:00: o sistema a dava por aberta justamente nas
        duas horas em que não há ninguém na recepção.
        """
        manha = ClinicBusinessHours.objects.create(
            clinic=clinic_a, weekday=Weekday.MONDAY, opens_at=time(8), closes_at=time(12)
        )
        tarde = ClinicBusinessHours.objects.create(
            clinic=clinic_a, weekday=Weekday.MONDAY, opens_at=time(14), closes_at=time(18)
        )

        assert manha.pk != tarde.pk
        assert clinic_a.business_hours.filter(weekday=Weekday.MONDAY).count() == 2

    def test_a_MESMA_hora_de_abertura_no_mesmo_dia_nao_repete(self, clinic_a):
        """
        Rede de segurança do banco contra a duplicata exata. A sobreposição de
        verdade é barrada na validação da API, que compara o intervalo inteiro.
        """
        ClinicBusinessHours.objects.create(
            clinic=clinic_a, weekday=Weekday.MONDAY, opens_at=time(8), closes_at=time(12)
        )

        with pytest.raises(IntegrityError), transaction.atomic():
            ClinicBusinessHours.objects.create(
                clinic=clinic_a, weekday=Weekday.MONDAY, opens_at=time(8), closes_at=time(18)
            )

    def test_o_mesmo_dia_em_outra_clinica_passa(self, clinic_a, clinic_b):
        ClinicBusinessHours.objects.create(
            clinic=clinic_a, weekday=Weekday.MONDAY, opens_at=time(8), closes_at=time(18)
        )

        assert ClinicBusinessHours.objects.create(
            clinic=clinic_b, weekday=Weekday.MONDAY, opens_at=time(8), closes_at=time(18)
        ).pk

    def test_segunda_e_zero_como_no_python(self):
        """
        O motor compara com `datetime.weekday()`; qualquer outra convenção
        exigiria conversão na hora de decidir se a clínica está aberta.
        """
        assert Weekday.MONDAY == 0
        assert Weekday.SUNDAY == 6

    def test_clinica_nova_nasce_sem_horario_nenhum(self, clinic_a):
        """
        Dia sem linha = fechado. Clínica nova não atende de madrugada por
        omissão, e um fluxo `only_outside_hours` nela dispara sempre.
        """
        assert clinic_a.business_hours.count() == 0
