"""
Equipe da clínica (§4.12, RF-EQP-1..9): quem entra, quem sai, e as travas que
impedem a clínica de se trancar para fora.
"""

import pytest
from rest_framework.exceptions import PermissionDenied

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.accounts.team import assert_not_last_manager
from apps.core.models import AuditLog
from apps.inbox.choices import AttendedBy, ConversationStatus, WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation
from apps.patients.models import Contact
from apps.scheduling.models import Practitioner
from conftest import PASSWORD, make_user

TEAM_URL = "/api/v1/team/"


@pytest.fixture
def manager(db, clinic_a):
    user = make_user("gestora@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.MANAGER)
    return user


@pytest.fixture
def client_as_manager(api_client, manager, clinic_a):
    api_client.force_authenticate(manager)
    api_client.credentials(HTTP_X_CLINIC_ID=str(clinic_a.pk))
    return api_client


@pytest.fixture
def practitioner_a(db, clinic_a):
    return Practitioner.objects.create(clinic=clinic_a, name="AGAMENON BARBOSA")


@pytest.fixture
def practitioner_b(db, clinic_b):
    return Practitioner.objects.create(clinic=clinic_b, name="DE OUTRA CLINICA")


@pytest.fixture
def conversation_open_with_attendant(db, clinic_a, attendant_a):
    channel = Channel.objects.create(
        clinic=clinic_a,
        provider=WhatsAppProviderKind.FAKE,
        display_number="5585999990000",
    )
    contact = Contact.objects.create(
        clinic=clinic_a, wa_id="5585900000009", display_name="Paciente"
    )
    return Conversation.objects.create(
        clinic=clinic_a,
        channel=channel,
        contact=contact,
        status=ConversationStatus.OPEN,
        attended_by=AttendedBy.AGENT,
        assigned_to=attendant_a,
    )


# --------------------------------------------------------------------------- #
# RF-EQP-1/2: listar e admitir
# --------------------------------------------------------------------------- #


def test_lista_a_equipe_da_clinica(client_as_manager, manager, attendant_a):
    response = client_as_manager.get(TEAM_URL)

    assert response.status_code == 200
    emails = {linha["email"] for linha in response.data["results"]}
    assert emails == {manager.email, attendant_a.email}


def test_a_lista_nao_vaza_membro_de_outra_clinica(client_as_manager, clinic_b, django_user_model):
    de_fora = make_user("de.fora@teste.dev")
    Membership.objects.create(user=de_fora, clinic=clinic_b, role=MembershipRole.MANAGER)

    response = client_as_manager.get(TEAM_URL)

    assert de_fora.email not in {linha["email"] for linha in response.data["results"]}


def test_busca_exclui_quem_nao_casa(client_as_manager, attendant_a):
    """Filtro que não EXCLUI não filtra: a asserção é a ausência."""
    response = client_as_manager.get(TEAM_URL, {"search": "atendente"})
    assert {linha["email"] for linha in response.data["results"]} == {attendant_a.email}

    vazio = client_as_manager.get(TEAM_URL, {"search": "nao-existe-ninguem-assim"})
    assert vazio.data["count"] == 0


def test_admite_pessoa_nova_com_senha_temporaria(client_as_manager, clinic_a):
    response = client_as_manager.post(
        TEAM_URL,
        {"email": "nova@teste.dev", "name": "Larissa Souza", "role": "attendant"},
        format="json",
    )

    assert response.status_code == 201
    senha = response.data["temporary_password"]
    assert senha and senha.count("-") == 3

    novo = User.objects.get(email="nova@teste.dev")
    assert novo.check_password(senha)
    assert novo.must_change_password is True
    assert novo.get_full_name() == "Larissa Souza"
    assert Membership.objects.filter(user=novo, clinic=clinic_a, is_active=True).exists()


def test_quem_ja_tem_conta_nao_recebe_senha_nova(client_as_manager, clinic_b):
    """
    ⚠️ O caso do médico que atende em duas clínicas: gerar senha aqui trocaria
    a credencial que ele usa na OUTRA, e o derrubaria de lá.
    """
    das_duas = make_user("medico.das.duas@teste.dev")
    Membership.objects.create(user=das_duas, clinic=clinic_b, role=MembershipRole.DOCTOR)

    response = client_as_manager.post(
        TEAM_URL,
        {"email": das_duas.email, "name": "Nome Trocado", "role": "attendant"},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["temporary_password"] is None
    das_duas.refresh_from_db()
    assert das_duas.check_password(PASSWORD), "a senha antiga tem de continuar valendo"
    assert das_duas.must_change_password is False
    assert das_duas.get_full_name() != "Nome Trocado"


def test_nao_admite_duas_vezes_a_mesma_pessoa(client_as_manager, attendant_a):
    response = client_as_manager.post(
        TEAM_URL, {"email": attendant_a.email, "role": "attendant"}, format="json"
    )

    assert response.status_code == 403
    assert response.data["code"] == "already_member"


def test_medico_recebe_a_carteira(client_as_manager, clinic_a, practitioner_a):
    response = client_as_manager.post(
        TEAM_URL,
        {
            "email": "medico@teste.dev",
            "name": "Agamenon Barbosa",
            "role": "doctor",
            "practitioner": practitioner_a.pk,
        },
        format="json",
    )

    assert response.status_code == 201
    assert response.data["practitioner"]["id"] == practitioner_a.pk


def test_carteira_de_outra_clinica_e_recusada(client_as_manager, practitioner_b):
    response = client_as_manager.post(
        TEAM_URL,
        {"email": "medico2@teste.dev", "role": "doctor", "practitioner": practitioner_b.pk},
        format="json",
    )

    assert response.status_code in (400, 403)
    assert not User.objects.filter(email="medico2@teste.dev").exists()


# --------------------------------------------------------------------------- #
# RF-EQP-4: editar
# --------------------------------------------------------------------------- #


def test_edita_papel_e_nome(client_as_manager, attendant_a):
    membership = Membership.objects.get(user=attendant_a)

    response = client_as_manager.patch(
        f"{TEAM_URL}{membership.pk}/",
        {"role": "manager", "name": "Larissa Souza Lima"},
        format="json",
    )

    assert response.status_code == 200
    membership.refresh_from_db()
    attendant_a.refresh_from_db()
    assert membership.role == MembershipRole.MANAGER
    assert attendant_a.get_full_name() == "Larissa Souza Lima"


def test_email_nao_e_editavel(client_as_manager, attendant_a):
    """A identidade de acesso não muda pela mão de outra pessoa (RF-EQP-4)."""
    membership = Membership.objects.get(user=attendant_a)
    original = attendant_a.email

    client_as_manager.patch(
        f"{TEAM_URL}{membership.pk}/", {"email": "sequestrado@teste.dev"}, format="json"
    )

    attendant_a.refresh_from_db()
    assert attendant_a.email == original


# --------------------------------------------------------------------------- #
# RF-EQP-8: as travas
# --------------------------------------------------------------------------- #


def test_gestor_nao_muda_o_proprio_papel(client_as_manager, manager, clinic_a):
    outro = make_user("gestor2@teste.dev")
    Membership.objects.create(user=outro, clinic=clinic_a, role=MembershipRole.MANAGER)
    meu = Membership.objects.get(user=manager)

    response = client_as_manager.patch(f"{TEAM_URL}{meu.pk}/", {"role": "attendant"}, format="json")

    assert response.status_code == 403
    assert response.data["code"] == "self_target"
    meu.refresh_from_db()
    assert meu.role == MembershipRole.MANAGER


def test_gestor_nao_se_desativa(client_as_manager, manager, clinic_a):
    outro = make_user("gestor3@teste.dev")
    Membership.objects.create(user=outro, clinic=clinic_a, role=MembershipRole.MANAGER)
    meu = Membership.objects.get(user=manager)

    response = client_as_manager.post(f"{TEAM_URL}{meu.pk}/deactivate/")

    assert response.status_code == 403
    assert response.data["code"] == "self_target"


def test_clinica_nunca_fica_sem_gestor_pela_tela(client_as_manager, manager, clinic_a):
    """
    O caminho completo: o gestor desativa o colega e depois tenta sair também.

    A segunda tentativa é a que importa, e é ela que garante o invariante pela
    tela: sobrando um gestor só, ele é quem está agindo, e ninguém desativa a
    si mesmo.
    """
    colega = make_user("colega.gestor@teste.dev")
    vinculo = Membership.objects.create(user=colega, clinic=clinic_a, role=MembershipRole.MANAGER)

    assert client_as_manager.post(f"{TEAM_URL}{vinculo.pk}/deactivate/").status_code == 200

    meu = Membership.objects.get(user=manager)
    resposta = client_as_manager.post(f"{TEAM_URL}{meu.pk}/deactivate/")
    assert resposta.status_code == 403
    assert Membership.objects.filter(
        clinic=clinic_a, role=MembershipRole.MANAGER, is_active=True
    ).exists()


def test_trava_do_ultimo_gestor_recusa_desativar_e_rebaixar(db, clinic_a):
    """
    A trava em si, exercitada na unidade.

    ⚠️ Ela é INALCANÇÁVEL pela tela de hoje, e de propósito: quem age já é um
    segundo gestor ativo, então o alvo nunca é o último. Ela existe para o dia
    em que outra porta chegar ao mesmo lugar (o plano da plataforma, §4.8, um
    comando, ou uma regra de papel que mude) - é a rede que o chatwoot não
    tem, onde a proteção mora só no `Index.vue` e a API aceita a remoção.
    """
    unico = Membership.objects.create(
        user=make_user("unico.gestor@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.MANAGER,
    )

    for acao in ("deactivate", "demote"):
        with pytest.raises(PermissionDenied) as erro:
            assert_not_last_manager(unico, action=acao)
        assert erro.value.detail["code"] == "last_manager"


def test_trava_do_ultimo_gestor_libera_quando_ha_outro(db, clinic_a):
    """Prova negativa: com dois gestores, a trava não pode atrapalhar."""
    primeiro = Membership.objects.create(
        user=make_user("g.um@teste.dev"), clinic=clinic_a, role=MembershipRole.MANAGER
    )
    Membership.objects.create(
        user=make_user("g.dois@teste.dev"), clinic=clinic_a, role=MembershipRole.MANAGER
    )

    assert_not_last_manager(primeiro, action="deactivate")


def test_gestor_inativo_nao_conta_como_gestor(db, clinic_a):
    """
    ⚠️ O erro que o chatwoot comete ao contar: lá a proteção conta admins
    CONFIRMADOS, e um convidado que nunca confirmou entra na conta como se
    fosse rede de segurança. Aqui, vínculo desativado não segura ninguém.
    """
    ativo = Membership.objects.create(
        user=make_user("g.ativo@teste.dev"), clinic=clinic_a, role=MembershipRole.MANAGER
    )
    Membership.objects.create(
        user=make_user("g.inativo@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.MANAGER,
        is_active=False,
    )

    with pytest.raises(PermissionDenied):
        assert_not_last_manager(ativo, action="deactivate")


# --------------------------------------------------------------------------- #
# RF-EQP-5: desativar solta as conversas
# --------------------------------------------------------------------------- #


def test_desativar_devolve_as_conversas_para_a_fila(
    client_as_manager, attendant_a, conversation_open_with_attendant
):
    membership = Membership.objects.get(user=attendant_a)

    response = client_as_manager.post(f"{TEAM_URL}{membership.pk}/deactivate/")

    assert response.status_code == 200
    assert response.data["released_conversations"] == 1
    conversation_open_with_attendant.refresh_from_db()
    assert conversation_open_with_attendant.assigned_to_id is None
    assert conversation_open_with_attendant.attended_by == AttendedBy.NONE
    assert conversation_open_with_attendant.status == ConversationStatus.WAITING


def test_a_lista_avisa_quantas_conversas_estao_com_a_pessoa(
    client_as_manager, attendant_a, conversation_open_with_attendant
):
    response = client_as_manager.get(TEAM_URL)

    linha = next(item for item in response.data["results"] if item["email"] == attendant_a.email)
    assert linha["open_conversations"] == 1


def test_desativar_nao_apaga_o_usuario(client_as_manager, attendant_a):
    membership = Membership.objects.get(user=attendant_a)

    client_as_manager.post(f"{TEAM_URL}{membership.pk}/deactivate/")

    assert User.objects.filter(pk=attendant_a.pk).exists()
    membership.refresh_from_db()
    assert membership.is_active is False


def test_reativa(client_as_manager, attendant_a):
    membership = Membership.objects.get(user=attendant_a)
    client_as_manager.post(f"{TEAM_URL}{membership.pk}/deactivate/")

    response = client_as_manager.post(f"{TEAM_URL}{membership.pk}/reactivate/")

    assert response.status_code == 200
    membership.refresh_from_db()
    assert membership.is_active is True


# --------------------------------------------------------------------------- #
# RF-EQP-6: reset de senha pelo gestor
# --------------------------------------------------------------------------- #


def test_reseta_a_senha_de_um_membro(client_as_manager, attendant_a):
    membership = Membership.objects.get(user=attendant_a)

    response = client_as_manager.post(f"{TEAM_URL}{membership.pk}/reset-password/")

    assert response.status_code == 200
    senha = response.data["temporary_password"]
    attendant_a.refresh_from_db()
    assert attendant_a.check_password(senha)
    assert attendant_a.must_change_password is True
    assert not attendant_a.check_password(PASSWORD), "a senha antiga tem de morrer"


def test_gestor_nao_reseta_a_propria_senha_por_aqui(client_as_manager, manager):
    meu = Membership.objects.get(user=manager)

    response = client_as_manager.post(f"{TEAM_URL}{meu.pk}/reset-password/")

    assert response.status_code == 403
    assert response.data["code"] == "self_target"


def test_reset_deixa_rastro_proprio_sem_a_senha(client_as_manager, attendant_a):
    membership = Membership.objects.get(user=attendant_a)

    response = client_as_manager.post(f"{TEAM_URL}{membership.pk}/reset-password/")

    log = AuditLog.objects.filter(action="PASSWORD_RESET").first()
    assert log is not None
    assert log.resource_id == str(attendant_a.pk)
    assert response.data["temporary_password"] not in str(log.payload)


# --------------------------------------------------------------------------- #
# RF-EQP-9 e a cerca por papel
# --------------------------------------------------------------------------- #


def test_admitir_deixa_rastro(client_as_manager):
    client_as_manager.post(
        TEAM_URL, {"email": "auditada@teste.dev", "role": "attendant"}, format="json"
    )

    assert AuditLog.objects.filter(action="CREATE", resource="Membership").exists()
    assert AuditLog.objects.filter(action="CREATE", resource="User").exists()


@pytest.mark.parametrize("papel", ["attendant", "doctor"])
def test_quem_nao_e_gestor_nao_alcanca_a_equipe(api_client, clinic_a, papel):
    user = make_user(f"{papel}.sem.acesso@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=papel)
    api_client.force_authenticate(user)
    api_client.credentials(HTTP_X_CLINIC_ID=str(clinic_a.pk))

    assert api_client.get(TEAM_URL).status_code == 403
