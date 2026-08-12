"""
A fila de resgate (RF-REA-1): quem entra, as faixas de ausência, o filtro por
etiqueta e as contagens facetadas.

O que estes testes protegem, acima de tudo: o contador e a listagem precisam
sair da MESMA definição de "quem está na fila". Eles divergiam - o
`status_counters` separava inativo de inativo-com-histórico desde a F1 e a
listagem só sabia filtrar `status=inactive`, então o número do topo da tela
nunca poderia corresponder à lista embaixo dele.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.patients.models import Patient, PatientTag, Tag

URL = "/api/v1/patients/"
SUMMARY = f"{URL}reactivation-summary/"


@pytest.fixture
def fila(clinic_a):
    """
    Quatro pacientes cobrindo os casos que decidem o recorte.

    `nunca_veio` é o ponto todo do RF-REA-1.1: ele é inativo (nunca consultou),
    entra em `?status=inactive` e NÃO pode entrar na fila de resgate.
    """
    agora = timezone.now()
    return {
        "recente": Patient.objects.create(
            clinic=clinic_a,
            name="Ativa Recente",
            last_appointment_at=agora - timedelta(days=30),
        ),
        "sumiu_4_meses": Patient.objects.create(
            clinic=clinic_a,
            name="Sumiu Quatro Meses",
            last_appointment_at=agora - timedelta(days=120),
        ),
        "sumiu_8_meses": Patient.objects.create(
            clinic=clinic_a,
            name="Sumiu Oito Meses",
            last_appointment_at=agora - timedelta(days=240),
        ),
        "sumiu_2_anos": Patient.objects.create(
            clinic=clinic_a,
            name="Sumiu Dois Anos",
            last_appointment_at=agora - timedelta(days=730),
        ),
        "nunca_veio": Patient.objects.create(
            clinic=clinic_a,
            name="Nunca Veio",
            last_appointment_at=None,
        ),
    }


@pytest.fixture
def etiquetas(clinic_a, fila):
    oeiras = Tag.objects.create(clinic=clinic_a, name="OEIRAS")
    sjp = Tag.objects.create(clinic=clinic_a, name="SJP")
    PatientTag.objects.create(patient=fila["sumiu_4_meses"], tag=oeiras)
    PatientTag.objects.create(patient=fila["sumiu_8_meses"], tag=sjp)
    PatientTag.objects.create(patient=fila["nunca_veio"], tag=oeiras)
    return {"oeiras": oeiras, "sjp": sjp}


def nomes(response):
    return sorted(item["name"] for item in response.data["results"])


def test_quem_nunca_consultou_fica_de_fora_da_fila(
    api_client, manager_single_clinic, fila
):
    """
    O coração do RF-REA-1.1. `nunca_veio` é inativo e aparece em
    `?status=inactive`, mas não é resgate: nunca houve o que resgatar.
    """
    api_client.force_authenticate(manager_single_clinic)

    inativos = api_client.get(URL, {"status": "inactive"})
    assert "Nunca Veio" in nomes(inativos)

    resgate = api_client.get(URL, {"segment": "to_reactivate"})
    assert nomes(resgate) == ["Sumiu Dois Anos", "Sumiu Oito Meses", "Sumiu Quatro Meses"]
    assert "Nunca Veio" not in nomes(resgate)
    assert "Ativa Recente" not in nomes(resgate)


def test_a_listagem_bate_com_o_contador(api_client, manager_single_clinic, fila):
    """
    A regressão que motivou a fatia: os dois números precisam ser o mesmo.
    """
    api_client.force_authenticate(manager_single_clinic)

    contador = api_client.get(f"{URL}counters/").data["to_reactivate"]
    listagem = api_client.get(URL, {"segment": "to_reactivate"}).data["count"]
    assert contador == listagem == 3


@pytest.mark.parametrize(
    ("faixa", "esperado"),
    [
        ("3_6", ["Sumiu Quatro Meses"]),
        ("6_12", ["Sumiu Oito Meses"]),
        ("12_plus", ["Sumiu Dois Anos"]),
    ],
)
def test_faixa_de_ausencia_recorta_e_exclui(
    api_client, manager_single_clinic, fila, faixa, esperado
):
    """
    Afirma a EXCLUSÃO, não só a presença: filtro que não filtra nada passaria
    num teste que só conferisse que o esperado está lá.
    """
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, {"segment": "to_reactivate", "absence": faixa})
    assert nomes(response) == esperado


def test_faixa_desconhecida_e_recusada_com_erro(
    api_client, manager_single_clinic, fila
):
    """
    Faixa inventada devolve 400, e não uma lista silenciosamente completa que
    a pessoa acharia que está filtrada. Mesma régua do `?status=`, que também
    é ChoiceFilter.
    """
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, {"segment": "to_reactivate", "absence": "ontem"})
    assert response.status_code == 400
    assert "absence" in response.data


def test_etiquetas_somam_em_vez_de_cruzar(
    api_client, manager_single_clinic, fila, etiquetas
):
    """
    RF-REA-1.3: duas etiquetas trazem quem tem UMA OU OUTRA. Cruzar devolveria
    zero aqui, e é justamente o engano que o teste tranca.
    """
    api_client.force_authenticate(manager_single_clinic)

    so_oeiras = api_client.get(
        URL, {"segment": "to_reactivate", "tag": str(etiquetas["oeiras"].pk)}
    )
    assert nomes(so_oeiras) == ["Sumiu Quatro Meses"]

    as_duas = api_client.get(
        URL,
        {"segment": "to_reactivate", "tag": f"{etiquetas['oeiras'].pk},{etiquetas['sjp'].pk}"},
    )
    assert nomes(as_duas) == ["Sumiu Oito Meses", "Sumiu Quatro Meses"]


def test_etiqueta_nao_traz_quem_esta_fora_do_segmento(
    api_client, manager_single_clinic, fila, etiquetas
):
    """`nunca_veio` tem a etiqueta OEIRAS e mesmo assim não entra."""
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(
        URL, {"segment": "to_reactivate", "tag": str(etiquetas["oeiras"].pk)}
    )
    assert "Nunca Veio" not in nomes(response)


def test_etiqueta_invalida_e_ignorada(api_client, manager_single_clinic, fila):
    """A querystring vem da URL: texto no lugar do id não pode virar 500."""
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, {"segment": "to_reactivate", "tag": "abc"})
    assert response.status_code == 200
    assert response.data["count"] == 3


def test_contagens_sao_facetadas(api_client, manager_single_clinic, fila, etiquetas):
    """
    RF-REA-1.4: cada dimensão conta ignorando o PRÓPRIO filtro e respeitando
    os outros. É o que impede o número do topo de divergir da lista.
    """
    api_client.force_authenticate(manager_single_clinic)

    # ⚠️ A base é a CLÍNICA INTEIRA desde 11/08/2026, e não a fila de
    # resgate: os cinco da fixture entram, inclusive o ativo e o que nunca
    # consultou. As faixas cobrem todos, e por isso a soma delas fecha com o
    # total.
    livre = api_client.get(SUMMARY).data
    assert livre["total"] == 5
    faixas = {f["key"]: f["count"] for f in livre["by_absence"]}
    assert faixas == {
        "all": 5,
        "0_3": 1,  # o ativo
        "3_6": 1,
        "6_12": 1,
        "12_plus": 1,
        "never": 1,  # o que nunca consultou
    }
    assert sum(v for k, v in faixas.items() if k != "all") == faixas["all"]

    # Com a faixa escolhida, as FAIXAS não mudam (ignoram o próprio filtro)...
    com_faixa = api_client.get(SUMMARY, {"absence": "3_6"}).data
    assert com_faixa["total"] == 1
    assert {f["key"]: f["count"] for f in com_faixa["by_absence"]} == faixas
    # ...e as ETIQUETAS passam a contar dentro dela.
    assert {t["name"]: t["count"] for t in com_faixa["by_tag"]} == {"OEIRAS": 1}

    # Com a etiqueta escolhida, o inverso.
    com_tag = api_client.get(SUMMARY, {"tag": str(etiquetas["sjp"].pk)}).data
    assert com_tag["total"] == 1
    assert {f["key"]: f["count"] for f in com_tag["by_absence"]} == {
        "all": 1,
        "0_3": 0,
        "3_6": 0,
        "6_12": 1,
        "12_plus": 0,
        "never": 0,
    }


class TestCatalogoDeEtiquetas:
    """
    O catálogo (RF-REA-1.3) é a LISTA de etiquetas; a faceta é o NÚMERO.

    Confundir as duas foi o defeito da primeira versão da tela: com a faixa
    "3 a 6 meses" aplicada, 20 das 57 etiquetas da clínica real sumiam do
    seletor e ficavam inalcançáveis.
    """

    URL = f"{URL}rescue-tags/"

    def test_nada_some_quando_a_faixa_recorta(
        self, api_client, manager_single_clinic, fila, etiquetas
    ):
        api_client.force_authenticate(manager_single_clinic)

        livre = api_client.get(self.URL).data
        assert {t["name"] for t in livre["results"]} == {"OEIRAS", "SJP"}

        # SJP só tem gente entre 6 e 12 meses. Com a faixa 3_6 ela precisa
        # continuar na lista, com zero, em vez de desaparecer.
        recortado = api_client.get(self.URL, {"absence": "3_6"}).data
        assert {t["name"] for t in recortado["results"]} == {"OEIRAS", "SJP"}
        contagens = {t["name"]: t["count"] for t in recortado["results"]}
        assert contagens == {"OEIRAS": 1, "SJP": 0}

    def test_quem_tem_gente_no_recorte_vem_primeiro(
        self, api_client, manager_single_clinic, fila, etiquetas
    ):
        """Os zerados descem, mas continuam na lista e escolhíveis."""
        api_client.force_authenticate(manager_single_clinic)
        nomes = [
            t["name"]
            for t in api_client.get(self.URL, {"absence": "3_6"}).data["results"]
        ]
        assert nomes == ["OEIRAS", "SJP"]

    def test_a_busca_e_por_ETIQUETA_e_nao_por_paciente(
        self, api_client, manager_single_clinic, fila, etiquetas
    ):
        """
        ⚠️ O `search` do filterset de paciente busca NOME DE PACIENTE. Deixá-lo
        vazar para o recorte fazia a fila filtrar por gente chamada "oeiras",
        esvaziar, e o catálogo voltar vazio para toda e qualquer busca.
        """
        api_client.force_authenticate(manager_single_clinic)

        achou = api_client.get(self.URL, {"search": "oei"}).data
        assert [t["name"] for t in achou["results"]] == ["OEIRAS"]
        # Dois: `sumiu_4_meses` e `nunca_veio`, que entra agora que a base é
        # a clínica inteira.
        assert achou["results"][0]["count"] == 2

        # Afirma a EXCLUSÃO: busca sem correspondência devolve zero.
        assert api_client.get(self.URL, {"search": "zzz"}).data["count"] == 0

    def test_a_busca_convive_com_a_faixa(
        self, api_client, manager_single_clinic, fila, etiquetas
    ):
        api_client.force_authenticate(manager_single_clinic)
        dados = api_client.get(self.URL, {"search": "sjp", "absence": "3_6"}).data
        assert [(t["name"], t["count"]) for t in dados["results"]] == [("SJP", 0)]

    def test_etiqueta_escolhida_nao_zera_as_outras(
        self, api_client, manager_single_clinic, fila, etiquetas
    ):
        """
        Faceta: a dimensão não conta a si mesma. Sem isto, marcar OEIRAS
        zeraria SJP e a pessoa não conseguiria somar as duas.
        """
        api_client.force_authenticate(manager_single_clinic)
        dados = api_client.get(
            self.URL, {"tag": str(etiquetas["oeiras"].pk)}
        ).data
        contagens = {t["name"]: t["count"] for t in dados["results"]}
        assert contagens == {"OEIRAS": 2, "SJP": 1}

    def test_com_o_segmento_pedido_a_etiqueta_de_fora_some(
        self, api_client, manager_single_clinic, clinic_a, fila
    ):
        """
        A base agora é a clínica inteira, então a etiqueta do `nunca_veio`
        APARECE. Ela some quando alguém pede o segmento de resgate, que
        continua existindo como filtro.
        """
        so_do_forasteiro = Tag.objects.create(clinic=clinic_a, name="SO NUNCA VEIO")
        PatientTag.objects.create(patient=fila["nunca_veio"], tag=so_do_forasteiro)
        api_client.force_authenticate(manager_single_clinic)

        na_base = [t["name"] for t in api_client.get(self.URL).data["results"]]
        assert "SO NUNCA VEIO" in na_base

        so_resgate = [
            t["name"]
            for t in api_client.get(
                self.URL, {"segment": "to_reactivate"}
            ).data["results"]
        ]
        assert "SO NUNCA VEIO" not in so_resgate


class TestCidadeEGenero:
    """
    Cidade e gênero na fila (RF-REA-1.8).

    A cidade é comparada CRUA, sem normalizar caixa nem acento: decisão do
    usuário em 11/08/2026. Juntar grafias por regra automática mutila nome de
    verdade - a tentativa transformou `São João do Piauí` em `São João do`.
    """

    CIDADES = f"{URL}rescue-cities/"

    @pytest.fixture
    def com_cidade(self, clinic_a, fila):
        fila["sumiu_4_meses"].city = "Oeiras"
        fila["sumiu_4_meses"].gender = "female"
        fila["sumiu_4_meses"].save()
        fila["sumiu_8_meses"].city = "OEIRAS"  # a MESMA cidade, outra grafia
        fila["sumiu_8_meses"].gender = "male"
        fila["sumiu_8_meses"].save()
        fila["sumiu_2_anos"].city = "Teresina"
        fila["sumiu_2_anos"].gender = "female"
        fila["sumiu_2_anos"].save()
        return fila

    def test_as_grafias_ficam_separadas(
        self, api_client, manager_single_clinic, com_cidade
    ):
        api_client.force_authenticate(manager_single_clinic)
        nomes = [c["name"] for c in api_client.get(self.CIDADES).data["results"]]
        # Se a clínica digitou assim, fica assim. São duas linhas, não uma.
        assert sorted(nomes) == ["OEIRAS", "Oeiras", "Teresina"]

    def test_filtrar_por_cidade_ignora_a_caixa_do_VALOR(
        self, api_client, manager_single_clinic, com_cidade
    ):
        """
        O catálogo separa as grafias, mas o filtro casa sem diferenciar caixa:
        quem clica em `Oeiras` não deveria perder quem está como `oeiras`.
        Para juntar as DUAS entradas, marca as duas - é multi-valor.
        """
        api_client.force_authenticate(manager_single_clinic)

        uma = api_client.get(URL, {"segment": "to_reactivate", "city": "Oeiras"})
        assert nomes(uma) == ["Sumiu Oito Meses", "Sumiu Quatro Meses"]

        # Afirma a EXCLUSÃO.
        outra = api_client.get(URL, {"segment": "to_reactivate", "city": "Teresina"})
        assert nomes(outra) == ["Sumiu Dois Anos"]

    def test_varias_cidades_somam(
        self, api_client, manager_single_clinic, com_cidade
    ):
        api_client.force_authenticate(manager_single_clinic)
        resposta = api_client.get(
            URL, {"segment": "to_reactivate", "city": "Oeiras,Teresina"}
        )
        assert len(nomes(resposta)) == 3

    def test_com_o_segmento_pedido_a_cidade_de_fora_some(
        self, api_client, manager_single_clinic, clinic_a, com_cidade, fila
    ):
        """Mesma regra do catálogo de etiquetas depois da base abrir."""
        fila["nunca_veio"].city = "Cidade Fantasma"
        fila["nunca_veio"].save()
        api_client.force_authenticate(manager_single_clinic)

        na_base = [c["name"] for c in api_client.get(self.CIDADES).data["results"]]
        assert "Cidade Fantasma" in na_base

        so_resgate = [
            c["name"]
            for c in api_client.get(
                self.CIDADES, {"segment": "to_reactivate"}
            ).data["results"]
        ]
        assert "Cidade Fantasma" not in so_resgate

    def test_cidade_zerada_no_recorte_continua_na_lista(
        self, api_client, manager_single_clinic, com_cidade
    ):
        """Mesma regra das etiquetas: a lista é da fila, o número é do recorte."""
        api_client.force_authenticate(manager_single_clinic)
        dados = api_client.get(self.CIDADES, {"absence": "3_6"}).data
        contagens = {c["name"]: c["count"] for c in dados["results"]}
        assert contagens == {"Oeiras": 1, "OEIRAS": 0, "Teresina": 0}

    def test_a_busca_de_cidade_e_no_servidor(
        self, api_client, manager_single_clinic, com_cidade
    ):
        api_client.force_authenticate(manager_single_clinic)
        achou = api_client.get(self.CIDADES, {"search": "oei"}).data
        assert {c["name"] for c in achou["results"]} == {"Oeiras", "OEIRAS"}
        assert api_client.get(self.CIDADES, {"search": "zzz"}).data["count"] == 0

    def test_genero_filtra_e_exclui(
        self, api_client, manager_single_clinic, com_cidade
    ):
        api_client.force_authenticate(manager_single_clinic)
        mulheres = api_client.get(URL, {"segment": "to_reactivate", "gender": "female"})
        assert nomes(mulheres) == ["Sumiu Dois Anos", "Sumiu Quatro Meses"]

        homens = api_client.get(URL, {"segment": "to_reactivate", "gender": "male"})
        assert nomes(homens) == ["Sumiu Oito Meses"]

    def test_genero_invalido_e_ignorado(
        self, api_client, manager_single_clinic, com_cidade
    ):
        """A querystring vem da URL e qualquer um pode digitar."""
        api_client.force_authenticate(manager_single_clinic)
        resposta = api_client.get(URL, {"segment": "to_reactivate", "gender": "xpto"})
        assert resposta.status_code == 200
        assert resposta.data["count"] == 3

    def test_o_genero_vem_contado_no_resumo(
        self, api_client, manager_single_clinic, com_cidade
    ):
        api_client.force_authenticate(manager_single_clinic)
        dados = api_client.get(SUMMARY).data
        contagens = {g["label"]: g["count"] for g in dados["by_gender"]}
        # Os três vêm sempre. "Não informado" são o ativo e o que nunca veio,
        # que entram agora que a base é a clínica inteira.
        assert contagens == {"Feminino": 2, "Masculino": 1, "Não informado": 2}

    def test_o_genero_ignora_o_proprio_filtro(
        self, api_client, manager_single_clinic, com_cidade
    ):
        """Faceta: sem isto, marcar Feminino zeraria Masculino na tela."""
        api_client.force_authenticate(manager_single_clinic)
        dados = api_client.get(SUMMARY, {"gender": "female"}).data
        contagens = {g["label"]: g["count"] for g in dados["by_gender"]}
        assert contagens == {"Feminino": 2, "Masculino": 1, "Não informado": 2}
        assert dados["total"] == 2  # o total, esse sim, respeita


def test_o_profissional_da_ultima_consulta_vem_na_fila(
    api_client, manager_single_clinic, clinic_a, fila
):
    """
    RF-REA-1: a linha mostra com quem a pessoa se consultava.

    Vem só no segmento de resgate: é uma subquery por linha, e as outras
    listagens de paciente não pagam por um dado que não mostram.
    """
    from apps.scheduling.models import Appointment, Practitioner

    profissional = Practitioner.objects.create(clinic=clinic_a, name="Dra. Amélia")
    Appointment.objects.create(
        clinic=clinic_a,
        patient=fila["sumiu_4_meses"],
        practitioner=profissional,
        starts_at=fila["sumiu_4_meses"].last_appointment_at,
    )
    api_client.force_authenticate(manager_single_clinic)

    na_fila = api_client.get(URL, {"segment": "to_reactivate"}).data["results"]
    achado = next(p for p in na_fila if p["name"] == "Sumiu Quatro Meses")
    assert achado["last_practitioner"] == "Dra. Amélia"

    # Na listagem comum o campo vem nulo, e a subquery nem roda.
    comum = api_client.get(URL).data["results"]
    assert all(p["last_practitioner"] is None for p in comum)


def test_ordem_padrao_traz_quem_sumiu_ha_mais_tempo(
    api_client, manager_single_clinic, fila
):
    """RF-REA-1.5, servido pelo OrderingFilter que o viewset já tinha."""
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(
        URL, {"segment": "to_reactivate", "ordering": "last_appointment_at"}
    )
    assert [item["name"] for item in response.data["results"]] == [
        "Sumiu Dois Anos",
        "Sumiu Oito Meses",
        "Sumiu Quatro Meses",
    ]
