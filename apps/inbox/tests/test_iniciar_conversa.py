"""Conversa iniciada pela clínica (RF-INB-11): nascimento, reuso e recusas."""

import pytest

from apps.inbox.choices import ActivityType, AttendedBy, ConversationStatus, MessageKind
from apps.inbox.models import Conversation, Message
from apps.inbox.services import ConversaSemDestino, iniciar_conversa
from apps.patients.models import Contact, Patient, PatientContact

URL = "/api/v1/conversations/start/"


def _paciente(clinic, name="Maria Silva", phone="5585999112233"):
    return Patient.objects.create(clinic=clinic, name=name, phone=phone)


# ------------------------- nascimento -------------------------


def test_nasce_aberta_com_quem_clicou(clinic_a, inbox_a, manager_single_clinic):
    paciente = _paciente(clinic_a)

    conversa, created = iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)

    assert created is True
    assert conversa.status == ConversationStatus.OPEN
    assert conversa.attended_by == AttendedBy.AGENT
    assert conversa.assigned_to_id == manager_single_clinic.pk
    assert conversa.patient_id == paciente.pk
    assert conversa.contact.wa_id == "5585999112233"  # canônico
    # O vínculo número↔paciente nasce junto, e o primeiro vira o principal.
    vinculo = PatientContact.objects.get(patient=paciente, contact=conversa.contact)
    assert vinculo.is_primary is True
    # E a timeline explica: "iniciou a conversa" (ASSIGNED com by=start).
    evento = Message.objects.get(conversation=conversa, kind=MessageKind.ACTIVITY)
    assert evento.activity_type == ActivityType.ASSIGNED
    assert evento.activity_data == {"from": AttendedBy.NONE, "by": "start"}


def test_reusa_contato_que_vive_na_grafia_sem_o_nove(clinic_a, inbox_a, manager_single_clinic):
    """O cadastro tem o 9; o contato da Meta vive sem o 9 — NÃO nasce segundo
    contato (é a metade outbound da autocura do §6.2)."""
    existente = Contact.objects.create(clinic=clinic_a, wa_id="558599112233")
    paciente = _paciente(clinic_a, phone="(85) 99911-2233")

    conversa, created = iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)

    assert created is True
    assert conversa.contact_id == existente.pk
    assert Contact.objects.filter(clinic=clinic_a, wa_id__contains="99112233").count() == 1


def test_segunda_chamada_devolve_a_mesma_sem_roubar_posse(
    clinic_a, inbox_a, manager_single_clinic, attendant_a
):
    paciente = _paciente(clinic_a)
    primeira, _ = iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)

    segunda, created = iniciar_conversa(clinic_a, attendant_a, patient=paciente)

    assert created is False
    assert segunda.pk == primeira.pk
    segunda.refresh_from_db()
    assert segunda.assigned_to_id == manager_single_clinic.pk  # posse intacta
    assert Conversation.objects.filter(clinic=clinic_a, contact=primeira.contact).count() == 1


def test_start_nao_reabre_resolvida(clinic_a, inbox_a, manager_single_clinic):
    from apps.inbox.attendance import resolve

    paciente = _paciente(clinic_a)
    conversa, _ = iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)
    resolve(conversa, manager_single_clinic)

    de_novo, created = iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)

    assert created is False
    de_novo.refresh_from_db()
    assert de_novo.status == ConversationStatus.RESOLVED  # navegar não reabre


def test_paciente_do_filho_vai_no_numero_do_responsavel(
    clinic_a, inbox_a, manager_single_clinic
):
    mae = _paciente(clinic_a, name="Maria Silva")
    contato_da_mae = Contact.objects.create(clinic=clinic_a, wa_id="5585999112233")
    PatientContact.objects.create(patient=mae, contact=contato_da_mae, is_primary=True)
    filho = Patient.objects.create(clinic=clinic_a, name="João Silva", phone="")
    PatientContact.objects.create(patient=filho, contact=contato_da_mae)

    conversa, created = iniciar_conversa(clinic_a, manager_single_clinic, patient=filho)

    assert created is True
    assert conversa.contact_id == contato_da_mae.pk  # número do responsável
    assert conversa.patient_id == filho.pk  # mas o assunto é o filho


# ------------------------- recusas -------------------------


def test_paciente_sem_telefone_nem_contato_recusa(clinic_a, inbox_a, manager_single_clinic):
    paciente = Patient.objects.create(clinic=clinic_a, name="Sem Fone")
    with pytest.raises(ConversaSemDestino):
        iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)


def test_telefone_fixo_recusa(clinic_a, inbox_a, manager_single_clinic):
    paciente = _paciente(clinic_a, phone="(85) 3244-1100")
    with pytest.raises(ConversaSemDestino, match="fixo"):
        iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)


def test_clinica_sem_canal_recusa(clinic_a, manager_single_clinic):
    paciente = _paciente(clinic_a)
    with pytest.raises(ConversaSemDestino, match="canal"):
        iniciar_conversa(clinic_a, manager_single_clinic, patient=paciente)


# ------------------------- API -------------------------


def test_api_cria_por_paciente(api_client, clinic_a, inbox_a, manager_single_clinic):
    paciente = _paciente(clinic_a)
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(URL, {"patient": paciente.pk}, format="json")

    assert response.status_code == 201
    assert response.data["created"] is True
    assert response.data["status"] == ConversationStatus.OPEN
    assert response.data["own_number"] is True  # o número é do próprio paciente

    de_novo = api_client.post(URL, {"patient": paciente.pk}, format="json")
    assert de_novo.status_code == 200
    assert de_novo.data["created"] is False
    assert de_novo.data["id"] == response.data["id"]


def test_api_avisa_quando_o_numero_e_do_responsavel(
    api_client, clinic_a, inbox_a, manager_single_clinic
):
    mae = _paciente(clinic_a, name="Maria Silva")
    contato = Contact.objects.create(clinic=clinic_a, wa_id="5585999112233")
    PatientContact.objects.create(patient=mae, contact=contato, is_primary=True)
    filho = Patient.objects.create(clinic=clinic_a, name="João Silva", phone="")
    PatientContact.objects.create(patient=filho, contact=contato)
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(URL, {"patient": filho.pk}, format="json")

    assert response.status_code == 201
    assert response.data["own_number"] is False  # vai no número da mãe


def test_api_cria_por_telefone_sem_paciente(api_client, clinic_a, inbox_a, manager_single_clinic):
    """O caminho do vCard: número de um terceiro, ainda sem cadastro."""
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(URL, {"phone": "+55 85 98876-5432"}, format="json")

    assert response.status_code == 201
    conversa = Conversation.objects.get(pk=response.data["id"])
    assert conversa.patient_id is None
    assert conversa.contact.wa_id == "5585988765432"


def test_api_exige_exatamente_um_dos_dois(api_client, clinic_a, inbox_a, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.post(URL, {}, format="json").status_code == 400
    assert (
        api_client.post(URL, {"patient": 1, "phone": "5585988765432"}, format="json").status_code
        == 400
    )


def test_api_recusa_com_motivo_legivel(api_client, clinic_a, inbox_a, manager_single_clinic):
    paciente = Patient.objects.create(clinic=clinic_a, name="Sem Fone")
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(URL, {"patient": paciente.pk}, format="json")
    assert response.status_code == 400
    assert "telefone" in str(response.data).lower()
