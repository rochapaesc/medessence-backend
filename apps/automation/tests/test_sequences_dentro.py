"""
A lista de QUEM ESTÁ DENTRO de uma sequência (RF-SEQ-11.4).

Diferente de `dispatches/`, que é a amostra dos 25 mais próximos: aqui a
pergunta é "onde está a Maria" e "tira essa pessoa daqui". O que estes testes
protegem é a lista bater com a contagem do topo e a busca achar quem a tela
mostra, inclusive contato sem ficha de paciente.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentEndReason,
    EnrollmentSource,
    FlowStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
from apps.automation.sequences import inscrever, remover
from apps.automation.tests.conftest import make_contact, make_flow
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/"


@pytest.fixture
def trilha(clinic_a):
    sequence = Sequence.objects.create(clinic=clinic_a, name="Resgate", is_active=True)
    for i, nome in enumerate(["Primeiro convite", "Segunda tentativa", "Última"], start=1):
        SequenceStep.objects.create(
            sequence=sequence,
            order=i,
            name=nome,
            offset_days=(i - 1) * 7,
            send_time=time(9, 0),
            flow=make_flow(clinic_a, name=f"Fluxo {i}", status=FlowStatus.ACTIVE),
        )
    return sequence


def com_ficha(clinic, nome, wa_id):
    patient = Patient.objects.create(clinic=clinic, name=nome)
    contact = make_contact(clinic, wa_id=wa_id)
    PatientContact.objects.create(patient=patient, contact=contact, is_primary=True)
    return patient, contact


def test_os_tres_recortes_vem_sempre_com_as_contagens(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """As contagens ficam nos botões: saber que há parados muda o que se faz."""
    api_client.force_authenticate(manager_single_clinic)
    correndo, c1 = com_ficha(clinic_a, "Correndo", "5585900001001")
    inscrever(trilha, c1, source=EnrollmentSource.BATCH, patient=correndo)

    parado, c2 = com_ficha(clinic_a, "Parado", "5585900001002")
    e2 = inscrever(trilha, c2, source=EnrollmentSource.BATCH, patient=parado)
    e2.held_since = timezone.now()
    e2.hold_reason = "no_window"
    e2.save(update_fields=["held_since", "hold_reason"])

    saiu, c3 = com_ficha(clinic_a, "Saiu", "5585900001003")
    remover(
        inscrever(trilha, c3, source=EnrollmentSource.BATCH, patient=saiu),
        reason=EnrollmentEndReason.MANUAL,
    )

    response = api_client.get(f"{URL}{trilha.pk}/enrollments/")

    assert response.status_code == 200
    assert response.data["contagens"] == {"correndo": 1, "parados": 1, "sairam": 1}
    assert len(response.data["resultados"]) == 1
    assert response.data["resultados"][0]["quem"] == "Correndo"


def test_parado_nao_conta_como_correndo(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Somar os dois daria mais gente dentro do que existe."""
    api_client.force_authenticate(manager_single_clinic)
    parado, contact = com_ficha(clinic_a, "Parado", "5585900001004")
    enrollment = inscrever(trilha, contact, source=EnrollmentSource.BATCH, patient=parado)
    enrollment.held_since = timezone.now()
    enrollment.hold_reason = "sequence_off"
    enrollment.save(update_fields=["held_since", "hold_reason"])

    correndo = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"estado": "correndo"})
    parados = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"estado": "parados"})

    assert correndo.data["resultados"] == []
    assert len(parados.data["resultados"]) == 1
    assert parados.data["resultados"][0]["hold_reason"] == "sequence_off"


def test_contato_sem_ficha_aparece_com_o_numero(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    Quem entra na trilha é o CONTATO. Esconder as linhas sem paciente faria a
    lista não bater com a contagem do topo.
    """
    api_client.force_authenticate(manager_single_clinic)
    contact = make_contact(clinic_a, wa_id="5585900001005")
    inscrever(trilha, contact, source=EnrollmentSource.FLOW_NODE)

    response = api_client.get(f"{URL}{trilha.pk}/enrollments/")

    linha = response.data["resultados"][0]
    assert linha["sem_ficha"] is True
    assert linha["patient"] is None
    assert linha["numero"] == "5585900001005"


def test_a_linha_diz_o_passo_como_posicao(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = com_ficha(clinic_a, "Ana", "5585900001006")
    inscrever(trilha, contact, source=EnrollmentSource.BATCH, patient=patient)

    response = api_client.get(f"{URL}{trilha.pk}/enrollments/")

    linha = response.data["resultados"][0]
    assert linha["passo_numero"] == 1
    assert linha["passos_total"] == 3
    assert linha["passo_nome"] == "Primeiro convite"
    assert linha["source"] == EnrollmentSource.BATCH


def test_busca_acha_por_nome_e_por_numero(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    maria, c1 = com_ficha(clinic_a, "Maria Clara", "5585900001007")
    inscrever(trilha, c1, source=EnrollmentSource.BATCH, patient=maria)
    joao, c2 = com_ficha(clinic_a, "João Pedro", "5585900001008")
    inscrever(trilha, c2, source=EnrollmentSource.BATCH, patient=joao)

    por_nome = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"search": "Maria"})
    por_numero = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"search": "1008"})

    assert [r["quem"] for r in por_nome.data["resultados"]] == ["Maria Clara"]
    assert [r["quem"] for r in por_numero.data["resultados"]] == ["João Pedro"]


def test_busca_acha_contato_sem_ficha(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Procurar só em `patient__name` não acharia quem a tela exibe."""
    api_client.force_authenticate(manager_single_clinic)
    contact = make_contact(clinic_a, wa_id="5585900001009")
    contact.display_name = "Zé do WhatsApp"
    contact.save(update_fields=["display_name"])
    inscrever(trilha, contact, source=EnrollmentSource.FLOW_NODE)

    response = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"search": "Zé"})

    assert len(response.data["resultados"]) == 1


def test_pagina_e_diz_se_tem_mais(api_client, manager_single_clinic, clinic_a, trilha):
    api_client.force_authenticate(manager_single_clinic)
    for i in range(35):
        patient, contact = com_ficha(clinic_a, f"Fila {i:02d}", f"55859000020{i:02d}")
        inscrever(trilha, contact, source=EnrollmentSource.BATCH, patient=patient)

    primeira = api_client.get(f"{URL}{trilha.pk}/enrollments/")
    segunda = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"offset": 30})

    assert primeira.data["total"] == 35
    assert len(primeira.data["resultados"]) == 30
    assert primeira.data["tem_mais"] is True
    assert len(segunda.data["resultados"]) == 5
    assert segunda.data["tem_mais"] is False


def test_quem_saiu_traz_o_motivo(api_client, manager_single_clinic, clinic_a, trilha):
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = com_ficha(clinic_a, "Saiu", "5585900001010")
    remover(
        inscrever(trilha, contact, source=EnrollmentSource.BATCH, patient=patient),
        reason=EnrollmentEndReason.OPTED_OUT,
    )

    response = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"estado": "sairam"})

    linha = response.data["resultados"][0]
    assert linha["end_reason"] == EnrollmentEndReason.OPTED_OUT
    assert linha["saiu_em"] is not None


def test_recorte_invalido_e_recusado(api_client, manager_single_clinic, clinic_a, trilha):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"estado": "inventado"})
    assert response.status_code == 400


def test_trilha_de_outra_clinica_nao_vaza(
    api_client, manager_single_clinic, clinic_b, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    de_fora = Sequence.objects.create(clinic=clinic_b, name="De outra")
    response = api_client.get(f"{URL}{de_fora.pk}/enrollments/")
    assert response.status_code == 404


def test_recepcao_enxerga_a_lista(api_client, attendant_a, clinic_a, trilha):
    api_client.force_authenticate(attendant_a)
    response = api_client.get(f"{URL}{trilha.pk}/enrollments/")
    assert response.status_code == 200


def test_tira_pela_INSCRICAO_para_contato_sem_ficha_poder_sair(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    ⚠️ O furo que o desenho da lista mostrou: o `unenroll` só entendia paciente,
    e contato sem ficha não teria como sair por lugar nenhum.
    """
    api_client.force_authenticate(manager_single_clinic)
    contact = make_contact(clinic_a, wa_id="5585900001011")
    enrollment = inscrever(trilha, contact, source=EnrollmentSource.FLOW_NODE)

    response = api_client.post(
        f"{URL}{trilha.pk}/unenroll/", {"enrollment": enrollment.pk}, format="json"
    )

    assert response.status_code == 200
    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.MANUAL


def test_tirar_inscricao_de_outra_trilha_nao_derruba_nada(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Idempotente e escopado: id que não é desta trilha não faz nada."""
    api_client.force_authenticate(manager_single_clinic)
    outra = Sequence.objects.create(clinic=clinic_a, name="Outra", is_active=True)
    SequenceStep.objects.create(
        sequence=outra,
        order=1,
        offset_days=0,
        send_time=time(9, 0),
        flow=make_flow(clinic_a, name="Fluxo outro", status=FlowStatus.ACTIVE),
    )
    contact = make_contact(clinic_a, wa_id="5585900001012")
    de_outra = inscrever(outra, contact, source=EnrollmentSource.FLOW_NODE)

    response = api_client.post(
        f"{URL}{trilha.pk}/unenroll/", {"enrollment": de_outra.pk}, format="json"
    )

    assert response.status_code == 200
    de_outra.refresh_from_db()
    assert de_outra.status == SequenceEnrollmentStatus.ACTIVE


def test_a_busca_recorta_as_CONTAGENS_tambem(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    ⚠️ Conserto de 20/08/2026. As três contagens eram calculadas sobre a
    trilha INTEIRA enquanto a lista mostrava o resultado da busca: procurar por
    alguém deixava os botões dizendo "14 parados" com uma linha na tela.

    É a mesma família dos contadores de Pacientes: duas partes da tela
    respondendo perguntas diferentes sem dizer isso.
    """
    api_client.force_authenticate(manager_single_clinic)
    procurada, c1 = com_ficha(clinic_a, "Marcia Reijane", "5585900001010")
    inscrever(trilha, c1, source=EnrollmentSource.BATCH, patient=procurada)

    outra, c2 = com_ficha(clinic_a, "Joana Prado", "5585900001011")
    e2 = inscrever(trilha, c2, source=EnrollmentSource.BATCH, patient=outra)
    e2.held_since = timezone.now()
    e2.hold_reason = "no_window"
    e2.save(update_fields=["held_since", "hold_reason"])

    terceira, c3 = com_ficha(clinic_a, "Joana Ribeiro", "5585900001012")
    remover(
        inscrever(trilha, c3, source=EnrollmentSource.BATCH, patient=terceira),
        reason=EnrollmentEndReason.MANUAL,
    )

    inteiro = api_client.get(f"{URL}{trilha.pk}/enrollments/")
    assert inteiro.data["contagens"] == {"correndo": 1, "parados": 1, "sairam": 1}

    # Procurando "Marcia": só ela existe, e é uma que está CORRENDO.
    achou = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"search": "Marcia"})
    assert achou.data["contagens"] == {"correndo": 1, "parados": 0, "sairam": 0}
    assert [r["quem"] for r in achou.data["resultados"]] == ["Marcia Reijane"]

    # E "Joana" pega duas, em recortes diferentes: uma parada e uma que saiu.
    joanas = api_client.get(f"{URL}{trilha.pk}/enrollments/", {"search": "Joana"})
    assert joanas.data["contagens"] == {"correndo": 0, "parados": 1, "sairam": 1}


def test_a_busca_pelo_NUMERO_tambem_recorta_as_contagens(
    api_client, manager_single_clinic, clinic_a, trilha
):
    # A busca olha nome da ficha, nome do WhatsApp e número; a contagem tem de
    # acompanhar os três, senão ela recorta por um e a lista por outro.
    api_client.force_authenticate(manager_single_clinic)
    quem, contact = com_ficha(clinic_a, "Pelo Numero", "5585900001020")
    inscrever(trilha, contact, source=EnrollmentSource.BATCH, patient=quem)
    outro, c2 = com_ficha(clinic_a, "Outro Qualquer", "5585900001021")
    inscrever(trilha, c2, source=EnrollmentSource.BATCH, patient=outro)

    resposta = api_client.get(
        f"{URL}{trilha.pk}/enrollments/", {"search": "5585900001020"}
    )

    assert resposta.data["contagens"]["correndo"] == 1
    assert [r["quem"] for r in resposta.data["resultados"]] == ["Pelo Numero"]
