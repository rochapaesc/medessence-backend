"""
Horário de funcionamento pela API (§9.1, RF-FLW-18.4).

O teste que mais importa aqui é o do ALMOÇO: até 05/08/2026 o modelo guardava
uma faixa por dia e o motor lia só a primeira, então a clínica que fecha das
12h às 14h contava como aberta o dia inteiro.
"""

from datetime import time

import pytest

from apps.tenants.models import ClinicBusinessHours

pytestmark = pytest.mark.django_db

URL = "/api/v1/clinic/business-hours/"


@pytest.fixture
def gestor(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def faixa(weekday, abre, fecha):
    return {"weekday": weekday, "opens_at": abre, "closes_at": fecha}


class TestLerEGravar:
    def test_clinica_sem_horario_devolve_lista_vazia_e_o_fuso(self, gestor, clinic_a):
        resposta = gestor.get(URL)

        assert resposta.status_code == 200
        assert resposta.data["hours"] == []
        # O fuso vai junto: a tela precisa dizer em que relógio as horas valem.
        assert resposta.data["timezone"] == clinic_a.timezone

    def test_grava_a_semana_e_devolve_ordenado(self, gestor, clinic_a):
        resposta = gestor.put(
            URL,
            {
                "hours": [
                    faixa(5, "08:00", "12:00"),
                    faixa(0, "14:00", "18:00"),
                    faixa(0, "08:00", "12:00"),
                ]
            },
            format="json",
        )

        assert resposta.status_code == 200
        assert resposta.data["hours"] == [
            faixa(0, "08:00", "12:00"),
            faixa(0, "14:00", "18:00"),
            faixa(5, "08:00", "12:00"),
        ]
        assert ClinicBusinessHours.objects.filter(clinic=clinic_a).count() == 3

    def test_salvar_de_novo_substitui_a_semana_inteira(self, gestor, clinic_a):
        gestor.put(URL, {"hours": [faixa(0, "08:00", "18:00")]}, format="json")
        gestor.put(URL, {"hours": [faixa(3, "09:00", "17:00")]}, format="json")

        assert list(
            ClinicBusinessHours.objects.filter(clinic=clinic_a).values_list(
                "weekday", flat=True
            )
        ) == [3]

    def test_nao_deixa_lixo_soft_deletado_para_tras(self, gestor, clinic_a):
        """
        Horário é configuração, não registro. Com soft delete cada salvamento
        deixaria sete linhas mortas, e o gestor mexe nisto várias vezes até
        acertar.
        """
        for _ in range(4):
            gestor.put(URL, {"hours": [faixa(0, "08:00", "18:00")]}, format="json")

        assert ClinicBusinessHours.all_objects.filter(clinic=clinic_a).count() == 1

    def test_semana_vazia_e_legitima(self, gestor, clinic_a):
        """Clínica que apaga tudo fica fechada, que é o default do modelo."""
        gestor.put(URL, {"hours": [faixa(0, "08:00", "18:00")]}, format="json")

        resposta = gestor.put(URL, {"hours": []}, format="json")

        assert resposta.status_code == 200
        assert not ClinicBusinessHours.objects.filter(clinic=clinic_a).exists()


def _relogio(monkeypatch, clinic, *, weekday: int, hora: str):
    """
    Para o relógio numa hora exata do FUSO DA CLÍNICA.

    Sem isto o teste depende do momento em que roda, e a versão anterior deste
    arquivo falhava por causa dos milissegundos que passavam entre criar a
    faixa e consultar o motor.

    2026-01-05 é uma segunda-feira, então somar o `weekday` cai no dia certo.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from apps.automation import engine

    h, m = (int(p) for p in hora.split(":"))
    parado = datetime(2026, 1, 5 + weekday, h, m, tzinfo=ZoneInfo(clinic.timezone))
    monkeypatch.setattr(engine.timezone, "now", lambda: parado)


def _expediente_com_almoco(clinic, weekday=2):
    """Quarta: 08:00 às 12:00 e 14:00 às 18:00."""
    ClinicBusinessHours.objects.create(
        clinic=clinic, weekday=weekday, opens_at=time(8, 0), closes_at=time(12, 0)
    )
    ClinicBusinessHours.objects.create(
        clinic=clinic, weekday=weekday, opens_at=time(14, 0), closes_at=time(18, 0)
    )


class TestOAlmoco:
    """
    A razão de a fatia existir: dois horários no MESMO dia.

    Antes de 05/08/2026 o modelo guardava um por dia e o motor lia só o
    primeiro, então a clínica que fecha para o almoço tinha de cadastrar
    08:00 às 18:00 e o robô calava justamente nas duas horas em que não havia
    ninguém na recepção.
    """

    def test_um_dia_aceita_dois_horarios(self, gestor, clinic_a):
        resposta = gestor.put(
            URL,
            {"hours": [faixa(0, "08:00", "12:00"), faixa(0, "14:00", "18:00")]},
            format="json",
        )

        assert resposta.status_code == 200
        assert ClinicBusinessHours.objects.filter(clinic=clinic_a, weekday=0).count() == 2

    def test_as_13h_a_clinica_esta_FECHADA(self, clinic_a, monkeypatch):
        """
        O ganho de PRODUTO da fatia. No modelo antigo esta clínica era
        obrigada a cadastrar 08:00 às 18:00, e às 13h o robô calava porque o
        sistema a dava por aberta.

        (Este teste sozinho não prova a correção do motor: com `.first()` ele
        passaria igual, porque a primeira faixa do dia é a da manhã e as 13h
        já caem fora dela. Quem prova é o das 16h, logo abaixo.)
        """
        from apps.automation.engine import _clinic_is_open

        _expediente_com_almoco(clinic_a)
        _relogio(monkeypatch, clinic_a, weekday=2, hora="13:00")

        assert _clinic_is_open(clinic_a) is False

    def test_as_10h_e_as_16h_a_clinica_esta_ABERTA(self, clinic_a, monkeypatch):
        """
        ⚠️ É ESTE que falha sem a correção do motor: com `.first()`, a faixa
        da manhã respondia sozinha, a da tarde nunca era consultada, e às 16h
        a clínica aberta contava como fechada.
        """
        from apps.automation.engine import _clinic_is_open

        _expediente_com_almoco(clinic_a)

        _relogio(monkeypatch, clinic_a, weekday=2, hora="10:00")
        assert _clinic_is_open(clinic_a) is True

        _relogio(monkeypatch, clinic_a, weekday=2, hora="16:00")
        assert _clinic_is_open(clinic_a) is True, "a SEGUNDA faixa do dia também vale"

    def test_fora_do_expediente_e_em_dia_sem_faixa(self, clinic_a, monkeypatch):
        from apps.automation.engine import _clinic_is_open

        _expediente_com_almoco(clinic_a)

        _relogio(monkeypatch, clinic_a, weekday=2, hora="22:00")
        assert _clinic_is_open(clinic_a) is False

        # Domingo não tem faixa nenhuma: fechado o dia inteiro.
        _relogio(monkeypatch, clinic_a, weekday=6, hora="10:00")
        assert _clinic_is_open(clinic_a) is False

    def test_o_fuso_da_clinica_e_que_manda(self, clinic_a, monkeypatch):
        """
        Comparar com o relógio do servidor poria a clínica de Fortaleza
        abrindo às 5h.
        """
        from apps.automation.engine import _clinic_is_open

        _expediente_com_almoco(clinic_a)
        _relogio(monkeypatch, clinic_a, weekday=2, hora="09:00")

        assert _clinic_is_open(clinic_a) is True


class TestOQueNaoSalva:
    def test_fechar_antes_de_abrir(self, gestor):
        resposta = gestor.put(
            URL, {"hours": [faixa(0, "18:00", "08:00")]}, format="json"
        )

        assert resposta.status_code == 400
        assert "Segunda" in str(resposta.data)

    def test_abrir_e_fechar_na_mesma_hora(self, gestor):
        """Intervalo de zero minuto não contém hora nenhuma."""
        resposta = gestor.put(
            URL, {"hours": [faixa(0, "08:00", "08:00")]}, format="json"
        )

        assert resposta.status_code == 400

    def test_dois_horarios_que_se_sobrepoem_no_mesmo_dia(self, gestor):
        """
        Duas faixas cobrindo a mesma hora dariam duas respostas para "está
        aberta agora", e a de trás nunca seria consultada.
        """
        resposta = gestor.put(
            URL,
            {"hours": [faixa(0, "08:00", "13:00"), faixa(0, "12:00", "18:00")]},
            format="json",
        )

        assert resposta.status_code == 400
        assert "sobrep" in str(resposta.data).lower()
        assert "Segunda" in str(resposta.data), "o erro diz QUAL dia"

    def test_o_mesmo_horario_em_dias_diferentes_passa(self, gestor):
        resposta = gestor.put(
            URL,
            {"hours": [faixa(0, "08:00", "18:00"), faixa(1, "08:00", "18:00")]},
            format="json",
        )

        assert resposta.status_code == 200

    def test_dia_da_semana_fora_da_faixa(self, gestor):
        assert (
            gestor.put(URL, {"hours": [faixa(7, "08:00", "18:00")]}, format="json").status_code
            == 400
        )


class TestQuemPodeMexer:
    def test_atendente_nao_le_nem_grava(self, api_client, attendant_a):
        """
        O horário decide se o robô atende ou se a conversa vai para a
        recepção. É a mesma régua dos fluxos: coisa de gestor.
        """
        api_client.force_authenticate(attendant_a)

        assert api_client.get(URL).status_code == 403
        assert (
            api_client.put(URL, {"hours": []}, format="json").status_code == 403
        )

    def test_o_horario_de_uma_clinica_nao_aparece_na_outra(
        self, gestor, clinic_a, clinic_b
    ):
        gestor.put(URL, {"hours": [faixa(0, "08:00", "18:00")]}, format="json")

        assert not ClinicBusinessHours.objects.filter(clinic=clinic_b).exists()
        assert ClinicBusinessHours.objects.filter(clinic=clinic_a).count() == 1
