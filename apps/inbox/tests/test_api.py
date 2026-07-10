"""API do inbox: escopo por tenant, contadores, ações e janela de 24h."""

from apps.inbox.choices import MessageDirection, SenderKind
from apps.inbox.models import Message
from apps.inbox.tests.conftest import make_message

CONVERSATIONS = "/api/v1/conversations/"
MESSAGES = "/api/v1/messages/"


def test_lista_conversas_escopada(api_client, manager_single_clinic, inbox_a, inbox_b):
    api_client.force_authenticate(manager_single_clinic)  # gestor da clinic_a
    response = api_client.get(CONVERSATIONS)
    assert response.status_code == 200
    ids = [c["id"] for c in response.data["results"]]
    assert inbox_a["conversation"].id in ids
    assert inbox_b["conversation"].id not in ids


def test_counters(api_client, manager_single_clinic, inbox_a):
    make_message(inbox_a["conversation"], sender_kind=SenderKind.CONTACT)
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(f"{CONVERSATIONS}counters/")
    assert response.status_code == 200
    assert response.data["total"] == 1
    assert response.data["unread"] == 1
    assert response.data["unassigned"] == 1


def test_read_zera_nao_lidas(api_client, manager_single_clinic, inbox_a):
    conversation = inbox_a["conversation"]
    make_message(conversation, sender_kind=SenderKind.CONTACT)
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(f"{CONVERSATIONS}{conversation.id}/read/")
    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.unread_count == 0


def test_assign_assume_atendimento(api_client, manager_single_clinic, inbox_a):
    conversation = inbox_a["conversation"]
    conversation.needs_agent = True
    conversation.save(update_fields=["needs_agent"])
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(f"{CONVERSATIONS}{conversation.id}/assign/")
    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.assigned_to_id == manager_single_clinic.id
    assert conversation.needs_agent is False


def test_link_patient(api_client, manager_single_clinic, inbox_a, clinic_a):
    from apps.patients.models import Patient, PatientContact

    conversation = inbox_a["conversation"]
    outro = Patient.objects.create(clinic=clinic_a, name="Responsável")
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{CONVERSATIONS}{conversation.id}/link-patient/",
        {"patient": outro.id},
        format="json",
    )
    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.patient_id == outro.id
    assert PatientContact.objects.filter(patient=outro, contact=conversation.contact).exists()


def test_link_patient_de_outra_clinica_recusado(
    api_client, manager_single_clinic, inbox_a, inbox_b
):
    conversation = inbox_a["conversation"]
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{CONVERSATIONS}{conversation.id}/link-patient/",
        {"patient": inbox_b["patient"].id},
        format="json",
    )
    assert response.status_code == 400


def test_criar_mensagem_com_janela_aberta(api_client, manager_single_clinic, inbox_a):
    conversation = inbox_a["conversation"]
    make_message(conversation, sender_kind=SenderKind.CONTACT)  # abre a janela
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {"conversation": conversation.id, "body": "Podemos sim!"},
        format="json",
    )
    assert response.status_code == 201
    message = Message.objects.get(pk=response.data["id"])
    assert message.direction == MessageDirection.OUT
    assert message.sender_kind == SenderKind.AGENT
    assert message.sent_by_id == manager_single_clinic.id


def test_texto_livre_bloqueado_fora_da_janela(api_client, manager_single_clinic, inbox_a):
    # Sem inbound → janela fechada.
    conversation = inbox_a["conversation"]
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {"conversation": conversation.id, "body": "oi"},
        format="json",
    )
    assert response.status_code == 400


def test_template_permitido_fora_da_janela(api_client, manager_single_clinic, inbox_a):
    conversation = inbox_a["conversation"]
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {"conversation": conversation.id, "template_name": "confirmacao_consulta"},
        format="json",
    )
    assert response.status_code == 201


def test_mensagens_escopadas_por_tenant(api_client, manager_single_clinic, inbox_a, inbox_b):
    make_message(inbox_a["conversation"], mid="a1")
    make_message(inbox_b["conversation"], mid="b1")
    api_client.force_authenticate(manager_single_clinic)  # clinic_a
    response = api_client.get(MESSAGES, {"conversation": inbox_b["conversation"].id})
    assert response.status_code == 200
    assert response.data["results"] == []
