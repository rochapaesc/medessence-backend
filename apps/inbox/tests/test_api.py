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
    """Assumir tira da fila e carimba a posse (F2.5: `needs_agent` virou
    `status` + `attended_by`)."""
    from apps.inbox.choices import AttendedBy, ConversationStatus

    conversation = inbox_a["conversation"]
    assert conversation.status == ConversationStatus.WAITING

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(f"{CONVERSATIONS}{conversation.id}/assign/")

    assert response.status_code == 200
    conversation.refresh_from_db()
    assert conversation.assigned_to_id == manager_single_clinic.id
    assert conversation.status == ConversationStatus.OPEN
    assert conversation.attended_by == AttendedBy.AGENT


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
    """
    O template precisa EXISTIR entre os aprovados (desde 12/08/2026): nome que
    não está na conta é recusado aqui em vez de morrer na Meta com 132001,
    cujo texto nem menciona que o problema é o nome.
    """
    from apps.inbox.models import WhatsAppTemplate

    conversation = inbox_a["conversation"]
    WhatsAppTemplate.objects.create(
        clinic=conversation.clinic,
        name="confirmacao_consulta",
        language="pt_BR",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Sua consulta está confirmada."}],
    )
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {"conversation": conversation.id, "template_name": "confirmacao_consulta"},
        format="json",
    )
    assert response.status_code == 201


def test_template_que_nao_existe_na_conta_e_recusado(
    api_client, manager_single_clinic, inbox_a
):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {"conversation": inbox_a["conversation"].id, "template_name": "inventado"},
        format="json",
    )
    assert response.status_code == 400
    assert "não está aprovado" in str(response.data)


def test_template_com_variavel_e_recusado_com_o_motivo(
    api_client, manager_single_clinic, inbox_a
):
    """
    ⚠️ O defeito que isto tranca: o envio ia SEM parâmetro nenhum e a Meta
    recusava por contagem (132000) todo template com variável - quatro dos
    cinco aprovados na clínica real. O atendente via falha genérica depois,
    sem nada dizendo que o problema era o template.
    """
    from apps.inbox.models import WhatsAppTemplate

    conversation = inbox_a["conversation"]
    WhatsAppTemplate.objects.create(
        clinic=conversation.clinic,
        name="comunicado",
        language="pt_BR",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Olá, {{1}}! Aviso: {{2}}."}],
    )
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {"conversation": conversation.id, "template_name": "comunicado"},
        format="json",
    )
    assert response.status_code == 400
    assert "pede 2 variáveis" in str(response.data)
    assert "{{1}}" in str(response.data)


def test_campo_escolhido_sem_dado_no_cadastro_diz_ISSO(
    api_client, manager_single_clinic, inbox_a
):
    """
    ⚠️ Dois erros diferentes, e dizê-los igual manda a pessoa procurar na tela
    um campo que ela VÊ preenchido. 1.061 dos 5.185 pacientes da clínica real
    não têm cidade: quem escolheu "cidade do paciente" preencheu o campo, e o
    que falta é o cadastro. "{{2}} não foi preenchida" faria o atendente
    clicar no campo, achar tudo certo e tentar de novo.
    """
    from apps.patients.models import Patient
    from apps.inbox.models import WhatsAppTemplate

    conversation = inbox_a["conversation"]
    conversation.patient = Patient.objects.create(
        clinic=conversation.clinic, name="IVANITA DIAS DE SOUSA", city=""
    )
    conversation.save(update_fields=["patient"])
    WhatsAppTemplate.objects.create(
        clinic=conversation.clinic,
        name="comunicado",
        language="pt_BR",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Olá, {{1}}! Você é de {{2}}."}],
    )
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {
            "conversation": conversation.id,
            "template_name": "comunicado",
            "template_variables": {
                "1": {"source": "patient_first_name"},
                "2": {"source": "patient_city"},
            },
        },
        format="json",
    )

    assert response.status_code == 400
    texto = str(response.data)
    assert "cadastro desta pessoa não tem o dado" in texto
    assert "não foi preenchida" not in texto


def test_template_com_variavel_passa_quando_as_FONTES_vem(
    api_client, manager_single_clinic, inbox_a
):
    """
    ⚠️ A tela manda a FONTE, não o valor: quem resolve é o servidor, com o
    contexto da conversa, pelo mesmo caminho da campanha e do nó de fluxo.
    Resolver no cliente é como a mensagem enviada começa a divergir da prévia
    que o atendente conferiu.
    """
    from apps.inbox.models import Message, WhatsAppTemplate

    conversation = inbox_a["conversation"]
    conversation.contact.display_name = "Amandinha 💚"
    conversation.contact.save(update_fields=["display_name"])
    WhatsAppTemplate.objects.create(
        clinic=conversation.clinic,
        name="comunicado",
        language="pt_BR",
        status="APPROVED",
        components=[{"type": "BODY", "text": "Olá, {{1}}! Aviso: {{2}}."}],
    )
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        MESSAGES,
        {
            "conversation": conversation.id,
            "template_name": "comunicado",
            "template_variables": {
                "1": {"source": "contact_name"},
                "2": {"source": "fixed", "value": "consulta remarcada"},
            },
        },
        format="json",
    )
    assert response.status_code == 201
    # A ORDEM é o que a Meta usa para casar {{1}} e {{2}}.
    message = Message.objects.get(pk=response.data["id"])
    assert message.content_data["template_params"] == {
        "1": "Amandinha 💚",
        "2": "consulta remarcada",
    }
    # ⚠️ A thread mostra o corpo MONTADO. Ela exibia "Olá, {{1}}! Aviso:
    # {{2}}." para a equipe, que é texto de programador na cara de quem
    # atende.
    assert message.body == "Olá, Amandinha 💚! Aviso: consulta remarcada."


def test_as_fontes_da_conversa_dizem_o_que_NAO_esta_disponivel(
    api_client, manager_single_clinic, inbox_a
):
    """
    17 das 25 conversas da clínica real não têm paciente vinculado. A fonte
    continua na lista, marcada como indisponível: sumir dela esconderia por
    que ela não serve, e escolhê-la mandaria a mensagem com buraco.
    """
    conversation = inbox_a["conversation"]
    conversation.patient = None
    conversation.save(update_fields=["patient"])
    conversation.contact.display_name = "Amandinha 💚"
    conversation.contact.save(update_fields=["display_name"])

    api_client.force_authenticate(manager_single_clinic)
    dados = api_client.get(f"/api/v1/conversations/{conversation.id}/template-context/").data

    fontes = {f["key"]: f for f in dados["sources"]}
    assert dados["has_patient"] is False
    assert fontes["contact_name"]["value"] == "Amandinha 💚"
    assert fontes["contact_name"]["available"] is True
    assert fontes["patient_first_name"]["available"] is False
    # O texto fixo não tem valor pronto, mas está sempre disponível.
    assert fontes["fixed"]["needs_text"] is True


def test_mensagens_escopadas_por_tenant(api_client, manager_single_clinic, inbox_a, inbox_b):
    make_message(inbox_a["conversation"], mid="a1")
    make_message(inbox_b["conversation"], mid="b1")
    api_client.force_authenticate(manager_single_clinic)  # clinic_a
    response = api_client.get(MESSAGES, {"conversation": inbox_b["conversation"].id})
    assert response.status_code == 200
    assert response.data["results"] == []
