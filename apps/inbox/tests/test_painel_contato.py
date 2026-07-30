"""
Painel do contato (Bloco C) e citação/reação (Bloco D).

O painel é o `ContactPanel` do Chatwoot enxugado ao que a recepção usa: quem é
a pessoa, o que já se anotou sobre ela, o que já foi trocado e as conversas
anteriores. O CPF sai MASCARADO — documento inteiro é da ficha (§15), onde o
acesso é auditado.
"""

from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.inbox.choices import MessageKind, ReactionActor, SenderKind
from apps.inbox.models import MediaAsset, Message, MessageReaction
from apps.patients.models import ContactNote, Patient, PatientContact

CONVERSATIONS = "/api/v1/conversations/"
NOTES = "/api/v1/contact-notes/"
MESSAGES = "/api/v1/messages/"


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _painel(client, conversation):
    return client.get(f"{CONVERSATIONS}{conversation.id}/contact-panel/")


def _mensagem(conversation, **kwargs):
    kwargs.setdefault("sender_kind", SenderKind.CONTACT)
    kwargs.setdefault("wa_timestamp", timezone.now())
    return Message.objects.create(
        clinic=conversation.clinic, conversation=conversation, **kwargs
    )


# ─────────────────────────────── painel ───────────────────────────────


def test_painel_traz_paciente_com_cpf_mascarado(logado, inbox_a):
    """Painel lateral de atendimento não é lugar de expor documento."""
    paciente = inbox_a["patient"]
    paciente.cpf = "12345678900"
    paciente.birth_date = date(1968, 3, 14)
    paciente.insurance_name = "Unimed"
    paciente.save(update_fields=["cpf", "birth_date", "insurance_name"])

    resposta = _painel(logado, inbox_a["conversation"])

    assert resposta.status_code == 200
    assert resposta.data["patient"]["name"] == paciente.name
    assert resposta.data["patient"]["insurance_name"] == "Unimed"
    assert resposta.data["patient"]["age"] > 50
    assert "12345678900" not in str(resposta.data)


def test_painel_mostra_os_outros_pacientes_do_mesmo_numero(logado, inbox_a):
    """Um número atende a casa inteira (RF-PAC-7). Metade dos enganos de
    atendimento é responder sobre a pessoa errada da mesma família."""
    filho = Patient.objects.create(clinic=inbox_a["conversation"].clinic, name="João Ribeiro")
    PatientContact.objects.create(patient=filho, contact=inbox_a["contact"])

    resposta = _painel(logado, inbox_a["conversation"])

    nomes = [p["name"] for p in resposta.data["other_patients"]]
    assert "João Ribeiro" in nomes


def test_painel_lista_arquivos_da_conversa_do_mais_novo_para_o_mais_velho(logado, inbox_a):
    """A recepção procura 'aquele PDF que ela mandou' o tempo todo."""
    conversation = inbox_a["conversation"]
    for i, nome in enumerate(["antigo.pdf", "recente.pdf"]):
        media = MediaAsset.objects.create(
            clinic=conversation.clinic, mime_type="application/pdf", filename=nome
        )
        _mensagem(
            conversation,
            kind=MessageKind.DOCUMENT,
            media=media,
            wa_timestamp=timezone.now() - timedelta(minutes=10 - i),
        )
    _mensagem(conversation, kind=MessageKind.TEXT, body="só texto, não é arquivo")

    resposta = _painel(logado, inbox_a["conversation"])

    nomes = [a["filename"] for a in resposta.data["files"]]
    assert nomes == ["recente.pdf", "antigo.pdf"]


def test_painel_lista_atendimentos_ja_encerrados(logado, inbox_a, manager_single_clinic):
    """
    ATENDIMENTOS, não "conversas anteriores".

    O Chatwoot abre uma conversa nova por atendimento, então lá listar
    conversas lista o histórico. Aqui a conversa é única por contato
    (`uniq_conversation_channel_contact`): o que se encerra é o atendimento, e
    ele fica marcado na linha do tempo. Listar conversas devolveria lista
    vazia — cara de "nunca falou antes" para quem fala com a clínica há anos.
    """
    from apps.inbox.attendance import resolve

    conversation = inbox_a["conversation"]
    resolve(conversation, manager_single_clinic, note="Resultado entregue.")

    resposta = _painel(logado, conversation)

    anteriores = resposta.data["previous_services"]
    assert len(anteriores) == 1
    assert anteriores[0]["by"]
    assert anteriores[0]["conversation"] == conversation.pk


def test_painel_de_outra_clinica_nao_abre(logado, inbox_b):
    resposta = _painel(logado, inbox_b["conversation"])
    assert resposta.status_code == 404


# ──────────────────────────── notas do contato ────────────────────────────


def test_nota_do_contato_sobrevive_ao_encerramento(logado, inbox_a):
    """Diferente da nota da conversa: 'prefere ser chamada de Malu' precisa
    valer na próxima vez que este número escrever."""
    criada = logado.post(
        NOTES,
        {"contact": inbox_a["contact"].pk, "body": "Prefere ser chamada de Malu."},
        format="json",
    )
    assert criada.status_code == 201

    inbox_a["conversation"].delete()  # conversa encerrada/removida

    nota = ContactNote.objects.get(pk=criada.data["id"])
    assert nota.body == "Prefere ser chamada de Malu."


def test_nota_registra_quem_escreveu(logado, inbox_a, manager_single_clinic):
    criada = logado.post(
        NOTES, {"contact": inbox_a["contact"].pk, "body": "Não atende antes das 10h."},
        format="json",
    )

    assert criada.data["author_name"]
    assert ContactNote.objects.get(pk=criada.data["id"]).author == manager_single_clinic


def test_notas_aparecem_no_painel(logado, inbox_a):
    logado.post(
        NOTES, {"contact": inbox_a["contact"].pk, "body": "Filho João agenda por ela."},
        format="json",
    )

    resposta = _painel(logado, inbox_a["conversation"])

    assert [n["body"] for n in resposta.data["notes"]] == ["Filho João agenda por ela."]


def test_notas_sao_escopadas_por_clinica(logado, inbox_a, inbox_b, clinic_b):
    ContactNote.objects.create(
        clinic=clinic_b, contact=inbox_b["contact"], body="nota da outra clínica"
    )

    resposta = logado.get(NOTES)

    assert all("outra clínica" not in n["body"] for n in resposta.data["results"])


@pytest.fixture
def colega_atendente(db, clinic_a):
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    user = make_user("colega.notas@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.ATTENDANT)
    return user


def test_nota_de_colega_nao_pode_ser_editada_nem_apagada(
    api_client, colega_atendente, inbox_a, manager_single_clinic
):
    """
    A MESMA regra das mensagens, no servidor: mexe quem escreveu, ou o gestor.

    O front mostra o lápis para todo mundo e traduz o 403 em aviso — sem a
    barreira aqui, qualquer colega reescreveria o registro do outro por um
    PATCH direto, e a assinatura da nota viraria mentira.
    """
    nota = ContactNote.objects.create(
        clinic=inbox_a["conversation"].clinic,
        contact=inbox_a["contact"],
        author=manager_single_clinic,
        body="Prefere ser chamada de Malu.",
    )
    api_client.force_authenticate(colega_atendente)

    editada = api_client.patch(
        f"{NOTES}{nota.pk}/", {"body": "reescrita"}, format="json"
    )
    apagada = api_client.delete(f"{NOTES}{nota.pk}/")

    assert editada.status_code == 403
    assert apagada.status_code == 403
    nota.refresh_from_db()
    assert nota.body == "Prefere ser chamada de Malu."


def test_autor_edita_e_gestor_apaga_nota_de_outro(
    api_client, colega_atendente, inbox_a, manager_single_clinic
):
    """O gestor entra porque alguém precisa poder limpar o que foi escrito
    errado quando quem escreveu não está mais."""
    nota = ContactNote.objects.create(
        clinic=inbox_a["conversation"].clinic,
        contact=inbox_a["contact"],
        author=colega_atendente,
        body="Filho João agenda por ela.",
    )

    api_client.force_authenticate(colega_atendente)
    editada = api_client.patch(
        f"{NOTES}{nota.pk}/", {"body": "Filho João agenda por ela (WhatsApp)."},
        format="json",
    )
    assert editada.status_code == 200
    nota.refresh_from_db()
    # A correção não rouba a autoria: a assinatura diz quem SOUBE, não quem
    # digitou por último.
    assert nota.author == colega_atendente

    api_client.force_authenticate(manager_single_clinic)
    apagada = api_client.delete(f"{NOTES}{nota.pk}/")
    assert apagada.status_code == 204


# ──────────────────────────── citação e reação ────────────────────────────


def test_resposta_citando_traz_a_mensagem_citada(logado, inbox_a):
    """`reply_to_provider_id` e o adapter já existiam; só a tela nunca via a
    citação — era meio caminho andado jogado fora."""
    conversation = inbox_a["conversation"]
    conversation.last_inbound_at = timezone.now()
    conversation.save(update_fields=["last_inbound_at"])
    citada = _mensagem(
        conversation, kind=MessageKind.TEXT, body="Posso levar o exame de janeiro?",
        provider_message_id="wamid.CITADA",
    )

    criada = logado.post(
        MESSAGES,
        {
            "conversation": conversation.id,
            "body": "Pode sim, traz o de janeiro mesmo.",
            "reply_to_provider_id": "wamid.CITADA",
        },
        format="json",
    )

    assert criada.status_code == 201
    assert criada.data["reply_to"]["id"] == citada.pk
    assert criada.data["reply_to"]["preview"] == "Posso levar o exame de janeiro?"


def test_citacao_a_mensagem_que_nao_temos_vem_nula(logado, inbox_a):
    """Resposta a algo anterior à integração: o balão desenha só a resposta.
    Chip vazio seria pior — anunciaria algo que não dá para mostrar."""
    conversation = inbox_a["conversation"]
    conversation.last_inbound_at = timezone.now()
    conversation.save(update_fields=["last_inbound_at"])

    criada = logado.post(
        MESSAGES,
        {"conversation": conversation.id, "body": "claro", "reply_to_provider_id": "wamid.SUMIU"},
        format="json",
    )

    assert criada.data["reply_to"] is None


def test_lista_resolve_todas_as_citacoes_em_uma_consulta(
    logado, inbox_a, django_assert_max_num_queries
):
    """A citação viaja por wamid; sem o resolvedor da página, cada balão que
    responde a outro custaria uma consulta."""
    conversation = inbox_a["conversation"]
    for i in range(6):
        alvo = _mensagem(
            conversation, kind=MessageKind.TEXT, body=f"pergunta {i}",
            provider_message_id=f"wamid.P{i}",
        )
        _mensagem(
            conversation, sender_kind=SenderKind.AGENT, kind=MessageKind.TEXT,
            body=f"resposta {i}", reply_to_provider_id=alvo.provider_message_id,
        )

    with django_assert_max_num_queries(10):
        resposta = logado.get(f"{MESSAGES}?conversation={conversation.id}")

    citadas = [m["reply_to"] for m in resposta.data["results"] if m["reply_to"]]
    assert len(citadas) == 6


def test_atendente_reage_e_o_selo_ganha_dono(logado, inbox_a, manager_single_clinic):
    """A tabela já guardava reação de atendente desde a fatia 4 — só não havia
    por onde criar uma."""
    conversation = inbox_a["conversation"]
    message = _mensagem(
        conversation, kind=MessageKind.TEXT, body="obrigada!", provider_message_id="wamid.X"
    )

    resposta = logado.post(f"{MESSAGES}{message.pk}/react/", {"emoji": "👍"}, format="json")

    assert resposta.status_code == 200
    selo = MessageReaction.objects.get(message=message)
    assert selo.emoji == "👍"
    assert selo.actor_kind == ReactionActor.AGENT
    assert selo.actor_user == manager_single_clinic
    assert resposta.data["reactions"][0]["actor_name"]


def test_reagir_com_emoji_vazio_apaga_o_selo(logado, inbox_a):
    """Apaga de VERDADE: a linha soft-deletada continuaria ocupando a chave
    única, e reagir de novo estouraria IntegrityError."""
    conversation = inbox_a["conversation"]
    message = _mensagem(
        conversation, kind=MessageKind.TEXT, body="oi", provider_message_id="wamid.Y"
    )
    logado.post(f"{MESSAGES}{message.pk}/react/", {"emoji": "👍"}, format="json")

    logado.post(f"{MESSAGES}{message.pk}/react/", {"emoji": ""}, format="json")

    assert MessageReaction.all_objects.filter(message=message).count() == 0
    # E dá para reagir de novo sem colidir com a chave única.
    de_novo = logado.post(f"{MESSAGES}{message.pk}/react/", {"emoji": "❤️"}, format="json")
    assert de_novo.status_code == 200


def test_nao_reage_a_mensagem_que_ainda_nao_saiu(logado, inbox_a):
    """Sem wamid não há o que reagir do lado da Meta — o selo ficaria só nosso."""
    conversation = inbox_a["conversation"]
    message = _mensagem(conversation, kind=MessageKind.TEXT, body="enviando…")

    resposta = logado.post(f"{MESSAGES}{message.pk}/react/", {"emoji": "👍"}, format="json")

    assert resposta.status_code == 400


# ─────────────── desvincular e notas do atendimento ─────────────── #


def test_desvincula_o_paciente_sem_destruir_o_historico_do_numero(
    logado, inbox_a
):
    """
    Vincular errado acontece — dois pacientes com o mesmo sobrenome, o número
    do filho cadastrado na mãe. Sem desfazer, o engano fica na tela para sempre.

    Mas solta APENAS a conversa: o `PatientContact` é o histórico de que este
    número já atendeu aquele paciente, e apagá-lo junto destruiria o vínculo
    do responsável familiar (RF-PAC-7) por causa de um engano numa conversa.
    """
    conversation = inbox_a["conversation"]
    PatientContact.objects.create(
        patient=conversation.patient, contact=conversation.contact
    )
    assert conversation.patient_id is not None

    resposta = logado.post(f"{CONVERSATIONS}{conversation.id}/unlink-patient/")

    assert resposta.status_code == 200
    conversation.refresh_from_db()
    assert conversation.patient_id is None
    assert PatientContact.objects.filter(contact=conversation.contact).exists()


def test_desvincular_conversa_sem_vinculo_avisa(logado, inbox_a):
    conversation = inbox_a["conversation"]
    conversation.patient = None
    conversation.save(update_fields=["patient"])

    resposta = logado.post(f"{CONVERSATIONS}{conversation.id}/unlink-patient/")

    assert resposta.status_code == 400


def test_painel_traz_as_notas_escritas_NO_CHAT(logado, inbox_a, manager_single_clinic):
    """
    O usuário procurou as notas do chat no painel e não achou.

    Elas entram, mas em bloco PRÓPRIO: a nota do atendimento morre com ele, a
    do contato acompanha a pessoa. Juntar as duas apagaria a diferença que dá
    sentido às duas.
    """
    from apps.inbox.services import create_internal_note

    create_internal_note(
        inbox_a["conversation"], manager_single_clinic, "Confirmou por telefone"
    )

    resposta = _painel(logado, inbox_a["conversation"])

    notas = resposta.data["conversation_notes"]
    assert [n["body"] for n in notas] == ["Confirmou por telefone"]
    assert notas[0]["author_name"]
    # E continua separado das notas DO CONTATO.
    assert resposta.data["notes"] == []


# ───────────── Quem usa este número (RF-PAC-7.1) ─────────────


@pytest.fixture
def familia(inbox_a):
    """Mãe principal do número, conversa vinculada ao FILHO — o caso em que o
    selo de principal mentia."""
    from apps.patients.vinculos import vincular

    clinic = inbox_a["conversation"].clinic
    contato = inbox_a["contact"]
    mae = Patient.objects.create(clinic=clinic, name="Maria Silva")
    filho = Patient.objects.create(clinic=clinic, name="João Silva")
    vincular(mae, contato)  # primeiro do número: principal
    vincular(filho, contato)
    conversa = inbox_a["conversation"]
    conversa.patient = filho
    conversa.save(update_fields=["patient"])
    return {"mae": mae, "filho": filho, "conversa": conversa, "contato": contato}


def test_selo_de_principal_vem_do_BANCO_nao_do_default(logado, familia):
    """
    O paciente da conversa saía com `is_primary` FIXO em true, então o filho
    aparecia como principal quando a principal era a mãe — e é essa flag que a
    seção e o diálogo "para quem é" usam para o selo.
    """
    resposta = _painel(logado, familia["conversa"])

    assert resposta.data["patient"]["name"] == "João Silva"
    assert resposta.data["patient"]["is_primary"] is False, "o principal é a mãe"
    outros = {p["name"]: p["is_primary"] for p in resposta.data["other_patients"]}
    assert outros["Maria Silva"] is True
    # Um principal, nunca dois: é o que a lista da seção mostra.
    todos = [resposta.data["patient"], *resposta.data["other_patients"]]
    assert sum(1 for p in todos if p["is_primary"]) == 1


def test_paciente_da_conversa_que_E_o_principal_vem_marcado(logado, inbox_a):
    from apps.patients.vinculos import vincular

    vincular(inbox_a["patient"], inbox_a["contact"])
    resposta = _painel(logado, inbox_a["conversation"])
    assert resposta.data["patient"]["is_primary"] is True


def test_adicionar_paciente_ao_numero_nao_troca_o_da_conversa(logado, familia):
    """A ação existe justamente porque o `link-patient` trocaria."""
    clinic = familia["conversa"].clinic
    pedro = Patient.objects.create(clinic=clinic, name="Pedro Silva")

    resposta = logado.post(
        f"{CONVERSATIONS}{familia['conversa'].id}/add-contact-patient/",
        {"patient": pedro.pk},
        format="json",
    )

    assert resposta.status_code == 200
    familia["conversa"].refresh_from_db()
    assert familia["conversa"].patient_id == familia["filho"].pk
    assert PatientContact.objects.filter(
        contact=familia["contato"], patient=pedro
    ).exists()


def test_definir_principal_pela_API(logado, familia):
    resposta = logado.post(
        f"{CONVERSATIONS}{familia['conversa'].id}/set-primary-patient/",
        {"patient": familia["filho"].pk},
        format="json",
    )

    assert resposta.status_code == 200
    vinculos = PatientContact.objects.filter(contact=familia["contato"], is_primary=True)
    assert vinculos.count() == 1
    assert vinculos.first().patient_id == familia["filho"].pk


def test_remover_do_numero_conta_quem_foi_promovido(logado, familia):
    """A tela precisa contar: sem isso a recepção descobre depois que o
    destino das mensagens mudou."""
    resposta = logado.post(
        f"{CONVERSATIONS}{familia['conversa'].id}/remove-contact-patient/",
        {"patient": familia["mae"].pk},
        format="json",
    )

    assert resposta.status_code == 200
    assert resposta.data["promoted_to_primary"]["name"] == "João Silva"


def test_remover_o_paciente_DA_conversa_solta_a_conversa(logado, familia):
    resposta = logado.post(
        f"{CONVERSATIONS}{familia['conversa'].id}/remove-contact-patient/",
        {"patient": familia["filho"].pk},
        format="json",
    )

    assert resposta.status_code == 200
    assert resposta.data["patient"] is None
    familia["conversa"].refresh_from_db()
    assert familia["conversa"].patient_id is None


def test_remover_quem_nao_usa_o_numero_avisa(logado, familia):
    clinic = familia["conversa"].clinic
    de_fora = Patient.objects.create(clinic=clinic, name="Alheio")
    resposta = logado.post(
        f"{CONVERSATIONS}{familia['conversa'].id}/remove-contact-patient/",
        {"patient": de_fora.pk},
        format="json",
    )
    assert resposta.status_code == 400


def test_vinculo_de_outra_clinica_nao_entra(logado, familia, clinic_b):
    """Escopo: paciente de outro tenant não vira vínculo daqui."""
    alheio = Patient.objects.create(clinic=clinic_b, name="De Outra")
    resposta = logado.post(
        f"{CONVERSATIONS}{familia['conversa'].id}/add-contact-patient/",
        {"patient": alheio.pk},
        format="json",
    )
    assert resposta.status_code == 400
    assert not PatientContact.objects.filter(patient=alheio).exists()
