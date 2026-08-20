"""
Os dois defeitos que o usuário achou em 20/08/2026, na tela de Pacientes.

Eles têm a mesma família: **duas partes da tela respondendo perguntas
diferentes sem dizer isso**. O contador contava a clínica inteira enquanto a
lista mostrava um recorte, e o selo de cada linha usava uma janela enquanto o
filtro usava outra.

Medido na clínica real antes do conserto:
  - filtrando por uma etiqueta, a lista caía para 350 e o contador continuava
    dizendo 465 ativos, quando a verdade daquele recorte era 57;
  - com "Ativo em 12 meses", o filtro devolvia 1394 pacientes e 929 deles
    apareciam na linha marcados como INATIVO.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.patients.models import Patient, Tag

pytestmark = pytest.mark.django_db

URL = "/api/v1/patients/"


@pytest.fixture
def carteira(clinic_a):
    """
    Quatro pacientes em dois eixos: dentro/fora da janela de 90 dias, com/sem
    a etiqueta. É o mínimo para os dois filtros se cruzarem de verdade.
    """
    agora = timezone.now()
    etiqueta = Tag.objects.create(clinic=clinic_a, name="VIP")
    gente = {}
    for nome, dias, marcado in [
        ("Recente com etiqueta", 30, True),
        ("Recente sem etiqueta", 30, False),
        ("Sumido com etiqueta", 200, True),
        ("Sumido sem etiqueta", 200, False),
    ]:
        p = Patient.objects.create(
            clinic=clinic_a,
            name=nome,
            last_appointment_at=agora - timedelta(days=dias),
        )
        if marcado:
            p.patient_tags.create(tag=etiqueta)
        gente[nome] = p
    return {"etiqueta": etiqueta, "gente": gente}


class TestContadorSegueOFiltro:
    def test_etiqueta_recorta_o_contador_tambem(
        self, api_client, manager_single_clinic, carteira
    ):
        # ⚠️ Este é o defeito. Sem o recorte, o contador responde pela clínica
        # inteira enquanto a lista mostra outra coisa, e os dois números
        # convivem na MESMA tela.
        api_client.force_authenticate(manager_single_clinic)
        etiqueta = carteira["etiqueta"]

        inteiro = api_client.get(f"{URL}counters/")
        assert inteiro.data["total"] == 4
        assert inteiro.data["active"] == 2

        recortado = api_client.get(f"{URL}counters/", {"tag": etiqueta.pk})
        assert recortado.data["total"] == 2, "o contador ignorou a etiqueta"
        assert recortado.data["active"] == 1

        # E bate com a lista, que é o ponto inteiro.
        lista = api_client.get(URL, {"tag": etiqueta.pk})
        assert lista.data["count"] == recortado.data["total"]

    def test_busca_tambem_recorta(self, api_client, manager_single_clinic, carteira):
        api_client.force_authenticate(manager_single_clinic)

        contador = api_client.get(f"{URL}counters/", {"search": "Sumido"})
        lista = api_client.get(URL, {"search": "Sumido"})

        assert contador.data["total"] == 2
        assert contador.data["active"] == 0
        assert lista.data["count"] == contador.data["total"]

    def test_o_status_NAO_recorta_o_contador(
        self, api_client, manager_single_clinic, carteira
    ):
        # ⚠️ De propósito, e é a única exceção. O contador responde "quantos
        # DESTES estão ativos"; filtrando por ativo ele diria "2 de 2", que é
        # verdade e não serve para nada. Mesma regra facetada do resumo da
        # fila de resgate.
        api_client.force_authenticate(manager_single_clinic)

        resposta = api_client.get(f"{URL}counters/", {"status": "active"})

        assert resposta.data["total"] == 4
        assert resposta.data["inactive"] == 2


class TestSeloBateComOFiltro:
    def test_janela_maior_nao_devolve_linha_marcada_de_inativo(
        self, api_client, manager_single_clinic, carteira
    ):
        # ⚠️ O defeito que o usuário viu: o filtro honrava o `?window=` e o
        # selo ia sempre na janela da clínica. Pedir "ativo em 12 meses"
        # trazia gente com a etiqueta "Inativo" na própria linha.
        api_client.force_authenticate(manager_single_clinic)

        resposta = api_client.get(URL, {"status": "active", "window": 360})

        nomes = sorted(i["name"] for i in resposta.data["results"])
        assert nomes == [
            "Recente com etiqueta",
            "Recente sem etiqueta",
            "Sumido com etiqueta",
            "Sumido sem etiqueta",
        ], "o filtro de 360 dias não trouxe quem sumiu há 200"

        marcados = {i["name"]: i["status"] for i in resposta.data["results"]}
        assert all(s == "active" for s in marcados.values()), (
            f"o filtro disse ativo e a linha disse outra coisa: {marcados}"
        )

    def test_sem_janela_pedida_continua_valendo_a_da_clinica(
        self, api_client, manager_single_clinic, carteira
    ):
        api_client.force_authenticate(manager_single_clinic)

        resposta = api_client.get(URL)

        selos = {i["name"]: i["status"] for i in resposta.data["results"]}
        assert selos["Recente com etiqueta"] == "active"
        assert selos["Sumido com etiqueta"] == "inactive"

    def test_a_janela_maior_tambem_muda_o_contador(
        self, api_client, manager_single_clinic, carteira
    ):
        # As duas metades do conserto se encontram aqui: o contador tem de
        # contar na mesma janela que o selo e o filtro usam.
        api_client.force_authenticate(manager_single_clinic)

        assert api_client.get(f"{URL}counters/").data["active"] == 2
        assert api_client.get(f"{URL}counters/", {"window": 360}).data["active"] == 4


class TestVariosProfissionais:
    """
    A tela de Reativação deixa marcar VÁRIOS profissionais na coluna.

    ⚠️ O campo era `NumberFilter` e o método dele já sabia dividir por vírgula
    desde 11/08/2026: o método mudou e o tipo do campo não. Medido antes do
    conserto, com dois marcados, o MESMO parâmetro dava três respostas
    diferentes: a lista recusava com 400, o resumo devolvia 200 ignorando o
    filtro em silêncio, e o contador estourava em 500 num `filter(pk="1,2")`.
    """

    @pytest.fixture
    def dois_profissionais(self, clinic_a):
        from apps.scheduling.models import Practitioner

        return [
            Practitioner.objects.create(clinic=clinic_a, name="Dra. Alana"),
            Practitioner.objects.create(clinic=clinic_a, name="Dr. Matheus"),
        ]

    @pytest.mark.parametrize(
        "caminho",
        ["", "counters/", "reactivation-summary/"],
        ids=["lista", "contador", "resumo-do-resgate"],
    )
    def test_dois_profissionais_nao_quebram(
        self, api_client, manager_single_clinic, dois_profissionais, caminho
    ):
        api_client.force_authenticate(manager_single_clinic)
        ids = ",".join(str(p.pk) for p in dois_profissionais)

        resposta = api_client.get(f"{URL}{caminho}", {"practitioner": ids})

        assert resposta.status_code == 200, resposta.data

    def test_o_filtro_de_verdade_recorta_pelos_dois(
        self, api_client, manager_single_clinic, clinic_a, dois_profissionais, carteira
    ):
        # ⚠️ Sem isto o teste acima passaria com o filtro sendo IGNORADO, que
        # era exatamente o que o resumo fazia: 200 e número errado.
        from datetime import timedelta

        from apps.scheduling.models import Appointment

        api_client.force_authenticate(manager_single_clinic)
        alana, matheus = dois_profissionais
        agora = timezone.now()
        gente = carteira["gente"]
        for paciente, quem in [
            (gente["Recente com etiqueta"], alana),
            (gente["Sumido com etiqueta"], matheus),
        ]:
            Appointment.objects.create(
                clinic=clinic_a,
                patient=paciente,
                practitioner=quem,
                starts_at=agora - timedelta(days=10),
                duration_min=30,
            )

        so_alana = api_client.get(URL, {"practitioner": str(alana.pk)})
        os_dois = api_client.get(URL, {"practitioner": f"{alana.pk},{matheus.pk}"})
        assert so_alana.data["count"] == 1
        assert os_dois.data["count"] == 2, "a lista ignorou o segundo profissional"

        contador = api_client.get(
            f"{URL}counters/", {"practitioner": f"{alana.pk},{matheus.pk}"}
        )
        assert contador.data["total"] == 2, "o contador não recortou pelos dois"

    def test_com_UM_escolhido_vale_a_janela_dele(
        self, api_client, manager_single_clinic, clinic_a, carteira
    ):
        # A regra antiga continua: um profissional traz a janela efetiva DELE.
        # Com vários, "a janela de qual" não tem resposta e cai na da clínica.
        from datetime import timedelta

        from apps.scheduling.models import Appointment, Practitioner

        api_client.force_authenticate(manager_single_clinic)
        agora = timezone.now()
        clinic_a.active_window_days = 30
        clinic_a.save(update_fields=["active_window_days"])
        generoso = Practitioner.objects.create(
            clinic=clinic_a, name="Dr. Janela Longa", active_window_days=360
        )
        paciente = carteira["gente"]["Sumido sem etiqueta"]  # sumiu há 200 dias
        Appointment.objects.create(
            clinic=clinic_a,
            patient=paciente,
            practitioner=generoso,
            starts_at=agora - timedelta(days=200),
            duration_min=30,
        )

        resposta = api_client.get(
            URL, {"practitioner": str(generoso.pk), "status": "active"}
        )

        assert [i["name"] for i in resposta.data["results"]] == ["Sumido sem etiqueta"]
        assert resposta.data["results"][0]["status"] == "active", (
            "o selo não acompanhou a janela do profissional"
        )

    def test_com_VÁRIOS_escolhidos_cai_na_janela_da_clinica(
        self, api_client, manager_single_clinic, clinic_a, carteira
    ):
        """
        ⚠️ A regra fácil de errar: com vários marcados, é tentador usar a
        janela do PRIMEIRO da lista. Isso faria o mesmo recorte responder
        coisas diferentes conforme a ordem em que a pessoa clicou nos nomes.

        "A janela de qual deles" não tem resposta, então vale a da clínica.
        """
        from datetime import timedelta

        from apps.scheduling.models import Appointment, Practitioner

        api_client.force_authenticate(manager_single_clinic)
        agora = timezone.now()
        clinic_a.active_window_days = 30
        clinic_a.save(update_fields=["active_window_days"])

        generoso = Practitioner.objects.create(
            clinic=clinic_a, name="Dr. Janela Longa", active_window_days=360
        )
        outro = Practitioner.objects.create(clinic=clinic_a, name="Dr. Outro")
        paciente = carteira["gente"]["Sumido sem etiqueta"]  # sumiu há 200 dias
        for quem in (generoso, outro):
            Appointment.objects.create(
                clinic=clinic_a,
                patient=paciente,
                practitioner=quem,
                starts_at=agora - timedelta(days=200),
                duration_min=30,
            )

        # Só o generoso: 360 dias valem, e ele aparece como ativo.
        so_ele = api_client.get(
            URL, {"practitioner": str(generoso.pk), "status": "active"}
        )
        assert [i["name"] for i in so_ele.data["results"]] == ["Sumido sem etiqueta"]

        # Os dois juntos: janela da clínica (30 dias), então ele NÃO é ativo.
        os_dois = api_client.get(
            URL,
            {"practitioner": f"{generoso.pk},{outro.pk}", "status": "active"},
        )
        assert os_dois.data["results"] == [], (
            "usou a janela do primeiro da lista em vez da da clínica"
        )
