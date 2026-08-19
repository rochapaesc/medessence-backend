"""
Inscrição em LOTE pela fila de resgate (RF-SEQ-3.3, RF-SEQ-9, RF-REA-2.5).

Duas coisas importam aqui: a prestação de contas (recusa em lote que não se
explica faz a pessoa achar que alcançou todo mundo) e o deslizamento dos
disparos, porque o teto diário da Meta é finito.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import EnrollmentSource, FlowStatus, SequenceEnrollmentStatus
from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
from apps.automation.sequences import (
    INSCRICOES_POR_MINUTO,
    contato_do_paciente,
    contatos_dos_pacientes,
    inscrever,
    inscrever_em_lote,
)
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.inbox.models import Conversation
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/"


@pytest.fixture
def trilha(clinic_a):
    sequence = Sequence.objects.create(clinic=clinic_a, name="Resgate", is_active=True)
    SequenceStep.objects.create(
        sequence=sequence,
        order=1,
        offset_days=0,
        send_time=time(8, 0),
        flow=make_flow(clinic_a, name="Convite", status=FlowStatus.ACTIVE),
    )
    return sequence


def paciente_com_numero(clinic, nome, wa_id):
    patient = Patient.objects.create(clinic=clinic, name=nome)
    contact = make_contact(clinic, wa_id=wa_id)
    PatientContact.objects.create(patient=patient, contact=contact, is_primary=True)
    return patient, contact


# ---- prestação de contas ----


def test_lote_conta_cada_motivo_de_ficar_de_fora(clinic_a, trilha):
    entra, _ = paciente_com_numero(clinic_a, "Entra", "5585900000101")
    calado, contato_calado = paciente_com_numero(clinic_a, "Pediu silêncio", "5585900000102")
    contato_calado.marketing_opt_out = True
    contato_calado.save(update_fields=["marketing_opt_out"])
    repetido, contato_repetido = paciente_com_numero(clinic_a, "Já estava", "5585900000103")
    inscrever(trilha, contato_repetido, source=EnrollmentSource.PATIENT_RECORD)
    sem_numero = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")

    contas = inscrever_em_lote(trilha, [entra, calado, repetido, sem_numero])

    assert contas == {
        "inscritos": 1,
        "sem_numero": 1,
        "opt_out": 1,
        "ja_inscritos": 1,
    }


def test_sequencia_operacional_nao_barra_quem_pediu_silencio(clinic_a, trilha):
    """O opt-out é de promoção: confirmação de consulta continua alcançando."""
    trilha.is_marketing = False
    trilha.save(update_fields=["is_marketing"])
    patient, contact = paciente_com_numero(clinic_a, "Pediu silêncio", "5585900000104")
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    contas = inscrever_em_lote(trilha, [patient])

    assert contas["inscritos"] == 1
    assert contas["opt_out"] == 0


# ---- deslizamento (RF-SEQ-9) ----


def test_lote_desliza_os_disparos_em_levas(clinic_a, trilha):
    """
    O primeiro passo de um lote grande não pode sair todo no mesmo minuto: o
    teto diário de conversas da Meta é finito e a régua de qualidade derruba o
    canal inteiro.
    """
    pacientes = [
        paciente_com_numero(clinic_a, f"P{i}", f"55859000002{i:02d}")[0]
        for i in range(INSCRICOES_POR_MINUTO + 3)
    ]

    inscrever_em_lote(trilha, pacientes)

    horarios = sorted(
        SequenceEnrollment.objects.filter(sequence=trilha).values_list(
            "next_dispatch_at", flat=True
        )
    )
    # Os primeiros 60 na mesma leva, os seguintes um minuto depois.
    assert horarios[0] == horarios[INSCRICOES_POR_MINUTO - 1]
    assert horarios[-1] - horarios[0] == timedelta(minutes=1)


# ---- o lote escolhe o MESMO número que o singular ----


def test_lote_e_singular_escolhem_o_mesmo_numero(clinic_a):
    """
    Duas regras para "por qual número falamos" acabariam divergindo, e a
    divergência só apareceria com o paciente do outro lado. Este teste prende
    as duas juntas.

    ⚠️ A expectativa MUDOU em 18/08/2026. Este teste dizia que o secundário
    ganhava por ter falado por último, e era o comportamento de verdade - até
    ele mandar a trilha de um paciente para o número de outra pessoa da mesma
    ficha, ao vivo. Agora o principal vence, e o que continua valendo é o
    propósito do teste: os dois caminhos escolhem o MESMO número.
    """
    patient = Patient.objects.create(clinic=clinic_a, name="Dois números")
    principal = make_contact(clinic_a, wa_id="5585900000301")
    novo = make_contact(clinic_a, wa_id="5585900000302")
    PatientContact.objects.create(patient=patient, contact=principal, is_primary=True)
    PatientContact.objects.create(patient=patient, contact=novo)

    canal = make_channel(clinic_a)
    Conversation.objects.create(
        clinic=clinic_a,
        channel=canal,
        contact=principal,
        last_message_at=timezone.now() - timedelta(days=5),
    )
    Conversation.objects.create(
        clinic=clinic_a, channel=canal, contact=novo, last_message_at=timezone.now()
    )

    # O PRINCIPAL ganha, mesmo com o outro tendo falado agora, nos dois
    # caminhos. Previsível vale mais aqui do que recente: quem olha a ficha
    # precisa saber para onde a mensagem vai.
    assert contato_do_paciente(patient) == principal
    assert contatos_dos_pacientes([patient])[patient.pk] == principal


def test_sem_conversa_nenhuma_vence_o_vinculo_principal(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Nunca falou")
    principal = make_contact(clinic_a, wa_id="5585900000401")
    outro = make_contact(clinic_a, wa_id="5585900000402")
    PatientContact.objects.create(patient=patient, contact=outro)
    PatientContact.objects.create(patient=patient, contact=principal, is_primary=True)

    assert contato_do_paciente(patient) == principal
    assert contatos_dos_pacientes([patient])[patient.pk] == principal


def test_paciente_sem_numero_nao_aparece_no_mapa(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")
    assert contatos_dos_pacientes([patient]) == {}


# ---- a API ----


def test_atendente_inscreve_a_selecao_e_recebe_as_contas(
    api_client, attendant_a, clinic_a, trilha
):
    entra, _ = paciente_com_numero(clinic_a, "Entra", "5585900000501")
    sem_numero = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")

    api_client.force_authenticate(attendant_a)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/",
        {"patients": [entra.pk, sem_numero.pk]},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["inscritos"] == 1
    assert response.data["sem_numero"] == 1


def test_paciente_de_outra_clinica_nao_some_calado(
    api_client, manager_single_clinic, clinic_a, clinic_b, trilha
):
    de_fora = Patient.objects.create(clinic=clinic_b, name="Da outra clínica")

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/", {"patients": [de_fora.pk]}, format="json"
    )

    assert response.status_code == 201
    assert response.data["nao_encontrados"] == 1
    assert response.data["inscritos"] == 0
    assert not SequenceEnrollment.objects.exists()


def test_selecao_acima_do_teto_e_recusada_com_o_numero(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    O teto é o mesmo lote de 1.000 da Cloud API (P17), e a recusa acontece onde
    a pessoa ainda pode estreitar os filtros - não no meio do envio.
    """
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/", {"patients": list(range(1, 1002))}, format="json"
    )

    assert response.status_code == 400
    assert "1000" in str(response.data)


def test_selecao_vazia_e_recusada(api_client, manager_single_clinic, clinic_a, trilha):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(f"{URL}{trilha.pk}/enroll-batch/", {"patients": []}, format="json")
    assert response.status_code == 400


def test_lote_marca_a_porta_de_entrada_como_lote(clinic_a, trilha):
    """O painel precisa dizer por onde cada um entrou (RF-SEQ-11)."""
    patient, _ = paciente_com_numero(clinic_a, "Entra", "5585900000601")
    inscrever_em_lote(trilha, [patient])

    enrollment = SequenceEnrollment.objects.get(sequence=trilha)
    assert enrollment.source == EnrollmentSource.BATCH
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


# ---- o recorte inteiro, sem os ids ----


def test_recorte_inteiro_sai_do_filterset_da_listagem(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    A fila real tem quase 1.900 pessoas e a tela só carrega 30 por vez: os ids
    não cabem no pedido. Quem expande é o servidor, pelo MESMO filterset da
    listagem, senão a lista e o lote passam a discordar de quem foi alcançado.
    """
    api_client.force_authenticate(manager_single_clinic)
    fortaleza, _ = paciente_com_numero(clinic_a, "Da cidade", "5585900000701")
    fortaleza.city = "Fortaleza"
    fortaleza.save(update_fields=["city"])
    outra, _ = paciente_com_numero(clinic_a, "De fora do recorte", "5585900000702")
    outra.city = "Sobral"
    outra.save(update_fields=["city"])

    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/",
        {"filtros": {"city": "Fortaleza"}},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["inscritos"] == 1
    inscritos = SequenceEnrollment.objects.filter(sequence=trilha)
    assert inscritos.count() == 1
    assert inscritos.first().contact.patient_contacts.first().patient_id == fortaleza.pk


def test_recorte_inteiro_respeita_quem_foi_desmarcado(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    Com o recorte inteiro marcado, desmarcar alguém vira EXCLUSÃO: a tela manda
    o filtro e a lista de quem tirou, e ninguém tirado pode entrar.
    """
    api_client.force_authenticate(manager_single_clinic)
    fica, _ = paciente_com_numero(clinic_a, "Fica", "5585900000703")
    tirado, _ = paciente_com_numero(clinic_a, "Desmarcado", "5585900000704")

    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/",
        {"filtros": {}, "excluir": [tirado.pk]},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["inscritos"] == 1
    contato = SequenceEnrollment.objects.get(sequence=trilha).contact
    assert contato.patient_contacts.first().patient_id == fica.pk


def test_recorte_maior_que_o_lote_entra_em_parte_e_diz_quanto_sobrou(
    api_client, manager_single_clinic, clinic_a, trilha, monkeypatch
):
    """
    ⚠️ Recusar 1.891 seco é bloqueio sem saída, e cortar em 1.000 calado é pior:
    a pessoa acha que alcançou todo mundo. Entra o que cabe, e o que sobrou vem
    escrito na resposta para a tela conseguir dizer.
    """
    from apps.automation.api.viewsets import sequence as viewset

    monkeypatch.setattr(viewset, "MAX_POR_LOTE", 2)
    api_client.force_authenticate(manager_single_clinic)
    for indice in range(4):
        paciente_com_numero(clinic_a, f"Fila {indice}", f"585900000{80 + indice}")

    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/", {"filtros": {}}, format="json"
    )

    assert response.status_code == 201
    assert response.data["inscritos"] == 2
    assert response.data["fora_do_lote"] == 2


def test_recorte_entra_pelos_que_sumiram_ha_mais_tempo(
    api_client, manager_single_clinic, clinic_a, trilha, monkeypatch
):
    """Quando não cabe todo mundo, o corte é por urgência, não por acaso."""
    from apps.automation.api.viewsets import sequence as viewset

    monkeypatch.setattr(viewset, "MAX_POR_LOTE", 1)
    api_client.force_authenticate(manager_single_clinic)
    agora = timezone.now()
    recente, _ = paciente_com_numero(clinic_a, "Veio semana passada", "5585900000705")
    recente.last_appointment_at = agora - timedelta(days=7)
    recente.save(update_fields=["last_appointment_at"])
    antigo, _ = paciente_com_numero(clinic_a, "Sumiu faz um ano", "5585900000706")
    antigo.last_appointment_at = agora - timedelta(days=365)
    antigo.save(update_fields=["last_appointment_at"])

    response = api_client.post(
        f"{URL}{trilha.pk}/enroll-batch/", {"filtros": {}}, format="json"
    )

    assert response.status_code == 201
    assert response.data["inscritos"] == 1
    contato = SequenceEnrollment.objects.get(sequence=trilha).contact
    assert contato.patient_contacts.first().patient_id == antigo.pk
