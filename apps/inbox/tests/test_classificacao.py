"""
Classificação e passagem adiante (F2.5 Bloco B1, §4.3.1 RF-ATD-6/8/9).

O que mais importa aqui: (1) a etiqueta é LOCAL e nunca encosta na `Tag` que
sincroniza com a vSaúde; (2) transferir respeita a MESMA trava de posse que
escrever — passar adiante conversa que não é sua é a disputa do Bloco A por
outro caminho; (3) a fila sai ordenada por urgência, do servidor.
"""

import pytest

from apps.inbox.attendance import ConversationBusy, add_label, remove_label, set_priority, transfer
from apps.inbox.choices import (
    ActivityType,
    AttendedBy,
    ConversationPriority,
    ConversationStatus,
    MessageKind,
)
from apps.inbox.models import Conversation, ConversationLabel, Message, Team

LABELS_URL = "/api/v1/conversation-labels/"


def _url(conversation, acao):
    return f"/api/v1/conversations/{conversation.pk}/{acao}/"


@pytest.fixture
def conversation(inbox_a):
    return inbox_a["conversation"]


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


@pytest.fixture
def colega(db, clinic_a):
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    user = make_user("colega.b1@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.ATTENDANT)
    return user


@pytest.fixture
def etiqueta(clinic_a):
    return ConversationLabel.objects.create(clinic=clinic_a, name="Reclamação", color="#C0474C")


def _eventos(conversation, tipo):
    return Message.objects.filter(
        conversation=conversation, kind=MessageKind.ACTIVITY, activity_type=tipo
    )


# ───────────────────────────── etiquetas ─────────────────────────────


def test_etiqueta_e_local_e_nao_toca_na_tag_do_prontuario(conversation, etiqueta, clinic_a):
    """
    A armadilha que este bloco existe para não cair: `patients.Tag` sincroniza
    com a vSaúde, e reusá-la faria "reclamação" tentar virar tag no prontuário.
    """
    from apps.patients.models import Tag

    add_label(conversation, None, label=etiqueta)

    assert conversation.labels.count() == 1
    assert Tag.objects.filter(clinic=clinic_a).count() == 0


def test_marcar_e_desmarcar_geram_evento_com_o_nome(conversation, etiqueta, manager_single_clinic):
    add_label(conversation, manager_single_clinic, label=etiqueta)
    remove_label(conversation, manager_single_clinic, label=etiqueta)

    marcou = _eventos(conversation, ActivityType.LABEL_ADDED).get()
    tirou = _eventos(conversation, ActivityType.LABEL_REMOVED).get()
    # O nome vai junto: a etiqueta pode ser aposentada depois, e o evento
    # continua tendo de dizer o que foi marcado.
    assert marcou.activity_data["label"] == "Reclamação"
    assert tirou.activity_data["label"] == "Reclamação"
    assert conversation.labels.count() == 0


def test_marcar_duas_vezes_nao_gera_dois_eventos(conversation, etiqueta, manager_single_clinic):
    add_label(conversation, manager_single_clinic, label=etiqueta)
    add_label(conversation, manager_single_clinic, label=etiqueta)

    assert _eventos(conversation, ActivityType.LABEL_ADDED).count() == 1


def test_excluir_etiqueta_APOSENTA_e_preserva_o_historico(logado, conversation, etiqueta):
    """Apagar reescreveria o passado: a conversa que foi uma reclamação
    continua tendo sido."""
    add_label(conversation, None, label=etiqueta)

    resposta = logado.delete(f"{LABELS_URL}{etiqueta.pk}/")

    assert resposta.status_code == 204
    etiqueta.refresh_from_db()
    assert etiqueta.is_active is False
    assert conversation.labels.filter(pk=etiqueta.pk).exists()


def test_atendente_LE_o_catalogo_mas_nao_escreve(api_client, attendant_a, etiqueta):
    """Catálogo fechado (RF-ATD-9.1): quem atende escolhe, não cadastra."""
    api_client.force_authenticate(attendant_a)

    assert api_client.get(LABELS_URL).status_code == 200
    assert api_client.post(LABELS_URL, {"name": "Inventada"}, format="json").status_code == 403


def test_catalogo_mostra_quantas_conversas_usam(logado, conversation, etiqueta):
    add_label(conversation, None, label=etiqueta)

    linha = logado.get(LABELS_URL).data["results"][0]

    # Sem a contagem, o gestor não tem como ver que criou etiqueta que ninguém
    # usa — ou duas que dizem a mesma coisa.
    assert linha["usage_count"] == 1


def test_etiqueta_de_outra_clinica_e_recusada(logado, conversation, clinic_b):
    de_outra = ConversationLabel.objects.create(clinic=clinic_b, name="Vazamento")

    resposta = logado.post(_url(conversation, "add-label"), {"label": de_outra.pk}, format="json")

    assert resposta.status_code == 400
    assert conversation.labels.count() == 0


def test_filtro_por_etiqueta_EXCLUI_quem_nao_tem(logado, conversation, etiqueta, inbox_a):
    add_label(conversation, None, label=etiqueta)

    com = logado.get("/api/v1/conversations/", {"label": etiqueta.pk})
    sem = logado.get("/api/v1/conversations/", {"label": 999999})

    # Asserção que PODE falhar: filtro que não filtra passaria num `>= 1`.
    assert [c["id"] for c in com.data["results"]] == [conversation.pk]
    assert sem.data["count"] == 0


# ───────────────────────────── prioridade ─────────────────────────────


def test_fila_ordena_por_RECENCIA_mesmo_havendo_urgente(logado, conversation, inbox_a, clinic_a):
    """
    Regra revista em 28/07/2026 depois do teste ao vivo: a mensagem que ACABOU
    de chegar tem de estar no topo. Ordenar por urgência antes da recência
    empurrava a conversa recém-chegada para o terceiro lugar, embaixo de uma
    urgente de ontem — e o usuário deixou de confiar no topo da lista.

    A prioridade continua existindo como tarja, selo e filtro. O que ela não
    faz mais é enterrar quem falou agora.
    """
    from apps.patients.models import Contact

    recente = Conversation.objects.create(
        clinic=clinic_a,
        channel=inbox_a["channel"],
        contact=Contact.objects.create(clinic=clinic_a, wa_id="5511999990000"),
        last_message_at=timezone_now_mais_recente(conversation),
    )
    set_priority(conversation, None, priority=ConversationPriority.URGENT)

    ids = [c["id"] for c in logado.get("/api/v1/conversations/").data["results"]]

    # Asserção que PODE falhar: com a ordenação antiga, ids[0] seria a urgente.
    assert ids[0] == recente.pk
    assert ids.index(conversation.pk) > ids.index(recente.pk)


def test_prioridade_repetida_nao_vira_evento(conversation, manager_single_clinic):
    set_priority(conversation, manager_single_clinic, priority=ConversationPriority.HIGH)
    set_priority(conversation, manager_single_clinic, priority=ConversationPriority.HIGH)

    assert _eventos(conversation, ActivityType.PRIORITY_CHANGED).count() == 1


def test_prioridade_invalida_e_recusada(logado, conversation):
    resposta = logado.post(_url(conversation, "priority"), {"priority": "catastrofica"}, format="json")

    assert resposta.status_code == 400
    conversation.refresh_from_db()
    assert conversation.priority == ConversationPriority.NORMAL


# ──────────────────────────── transferência ────────────────────────────


def test_transferir_troca_o_dono_e_registra_de_para(conversation, manager_single_clinic, colega):
    from apps.inbox.attendance import take_over

    take_over(conversation, manager_single_clinic)
    transfer(conversation, manager_single_clinic, to_user=colega, note="ela conhece o caso")

    conversation.refresh_from_db()
    assert conversation.assigned_to_id == colega.pk
    assert conversation.attended_by == AttendedBy.AGENT
    assert conversation.status == ConversationStatus.OPEN

    evento = _eventos(conversation, ActivityType.TRANSFERRED).get()
    assert evento.activity_data["to"]
    assert evento.activity_data["note"] == "ela conhece o caso"


def test_nota_de_passagem_vira_nota_interna_e_NAO_sai(conversation, manager_single_clinic, colega):
    from apps.inbox.attendance import take_over

    take_over(conversation, manager_single_clinic)
    transfer(conversation, manager_single_clinic, to_user=colega, note="convênio novo")

    nota = Message.objects.get(conversation=conversation, is_internal=True)
    assert nota.body == "convênio novo"
    # Sem wamid e sem status: não passou pelo caminho de envio.
    assert nota.provider_message_id == ""


def test_quem_NAO_tem_a_posse_nao_transfere(conversation, manager_single_clinic, colega):
    """Passar adiante conversa que não é sua é a disputa de posse por outro
    caminho (RF-ATD-15)."""
    from apps.inbox.attendance import take_over

    take_over(conversation, colega)

    with pytest.raises(ConversationBusy):
        transfer(conversation, manager_single_clinic, to_user=manager_single_clinic)

    conversation.refresh_from_db()
    assert conversation.assigned_to_id == colega.pk


def test_transferir_para_quem_ja_e_o_dono_nao_faz_nada(conversation, manager_single_clinic):
    from apps.inbox.attendance import take_over

    take_over(conversation, manager_single_clinic)
    transfer(conversation, manager_single_clinic, to_user=manager_single_clinic)

    assert _eventos(conversation, ActivityType.TRANSFERRED).count() == 0


def test_transferir_para_fora_da_clinica_e_recusado(logado, conversation, clinic_b):
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    de_fora = make_user("estranho.b1@teste.dev")
    Membership.objects.create(user=de_fora, clinic=clinic_b, role=MembershipRole.ATTENDANT)

    resposta = logado.post(_url(conversation, "transfer"), {"to": de_fora.pk}, format="json")

    assert resposta.status_code == 400


def test_agents_BUSCA_no_servidor_e_exclui_quem_nao_casa(logado, colega, manager_single_clinic):
    """
    Buscar é trabalho de servidor. Filtrar lista já baixada só funciona
    enquanto ela cabe inteira na resposta — e no dia em que não couber, a busca
    passa a mentir, achando só dentro do pedaço que veio.

    A asserção afirma a EXCLUSÃO: `>= 1` passaria também com o filtro inerte,
    que foi exatamente como a busca da auditoria ficou morta por uma sessão.
    """
    achou = logado.get("/api/v1/conversations/agents/", {"search": "colega"})
    nao_achou = logado.get("/api/v1/conversations/agents/", {"search": "zzz-ninguem"})

    assert [p["id"] for p in achou.data] == [colega.pk]
    assert nao_achou.data == []


def test_agents_sem_busca_traz_todo_mundo(logado, colega, manager_single_clinic):
    ids = {p["id"] for p in logado.get("/api/v1/conversations/agents/").data}

    assert {colega.pk, manager_single_clinic.pk} <= ids


def test_agents_traz_a_carga_de_cada_um(logado, conversation, manager_single_clinic, colega):
    from apps.inbox.attendance import take_over

    take_over(conversation, manager_single_clinic)

    linhas = {p["id"]: p for p in logado.get("/api/v1/conversations/agents/").data}

    # Sem a carga, todo mundo empurra para a mesma pessoa.
    assert linhas[manager_single_clinic.pk]["open_conversations"] == 1
    assert linhas[colega.pk]["open_conversations"] == 0


# ──────────────────────────────── setor ────────────────────────────────


def test_migration_semeia_recepcao_em_cada_clinica(clinic_a):
    """O B2 não precisa decidir o que fazer com clínica sem setor nenhum."""
    assert Team.objects.filter(clinic=clinic_a, name="Recepção").exists()


def timezone_now_mais_recente(conversation):
    """Uma mensagem mais nova que a da conversa do fixture — para provar que a
    ordenação por prioridade vence a recência."""
    from datetime import timedelta

    from django.utils import timezone

    base = conversation.last_message_at or timezone.now()
    return base + timedelta(minutes=5)
