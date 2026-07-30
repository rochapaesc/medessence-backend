"""Gerenciar quem usa o número (RF-PAC-7.1): principal, remoção e promoção."""

import pytest

from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation
from apps.patients.models import Contact, Patient, PatientContact
from apps.patients.vinculos import (
    contato_do_numero,
    definir_principal,
    desvincular,
    pacientes_do_contato,
    vincular,
)


@pytest.fixture
def familia(clinic_a):
    """A mãe e dois filhos no mesmo número — o caso do RF-PAC-7."""
    contato = Contact.objects.create(clinic=clinic_a, wa_id="5585999112233")
    mae = Patient.objects.create(clinic=clinic_a, name="Maria Silva")
    joao = Patient.objects.create(clinic=clinic_a, name="João Silva")
    pedro = Patient.objects.create(clinic=clinic_a, name="Pedro Silva")
    vincular(mae, contato)  # primeiro: vira o principal
    vincular(joao, contato)
    vincular(pedro, contato)
    return {"contato": contato, "mae": mae, "joao": joao, "pedro": pedro}


# ------------------------- vincular -------------------------


def test_o_primeiro_do_numero_vira_o_principal(clinic_a):
    contato = Contact.objects.create(clinic=clinic_a, wa_id="5585999112233")
    mae = Patient.objects.create(clinic=clinic_a, name="Maria")
    filho = Patient.objects.create(clinic=clinic_a, name="João")

    primeiro, criado1 = vincular(mae, contato)
    segundo, criado2 = vincular(filho, contato)

    assert (criado1, criado2) == (True, True)
    assert primeiro.is_primary is True
    assert segundo.is_primary is False


def test_vincular_duas_vezes_nao_duplica(clinic_a, familia):
    antes = PatientContact.objects.filter(contact=familia["contato"]).count()
    vinculo, criado = vincular(familia["joao"], familia["contato"])
    assert criado is False
    assert PatientContact.objects.filter(contact=familia["contato"]).count() == antes


# ------------------------- definir principal -------------------------


def test_definir_principal_troca_e_deixa_UM_so(familia):
    definir_principal(familia["pedro"], familia["contato"])

    principais = PatientContact.objects.filter(
        contact=familia["contato"], is_primary=True
    )
    assert principais.count() == 1
    assert principais.first().patient_id == familia["pedro"].pk


def test_definir_principal_em_quem_ja_e_nao_quebra(familia):
    definir_principal(familia["mae"], familia["contato"])
    assert (
        PatientContact.objects.filter(contact=familia["contato"], is_primary=True).count()
        == 1
    )


def test_definir_principal_de_quem_nao_usa_o_numero_recusa(clinic_a, familia):
    de_fora = Patient.objects.create(clinic=clinic_a, name="Alheio")
    with pytest.raises(PatientContact.DoesNotExist):
        definir_principal(de_fora, familia["contato"])


# ------------------------- remover -------------------------


def test_remover_o_principal_PROMOVE_o_mais_antigo(familia):
    """O invariante: número com gente nunca fica sem principal — senão o
    auto-vínculo da conversa nova e a resolução de número param calados."""
    efeito = desvincular(familia["mae"], familia["contato"])

    assert efeito["promoveu"].pk == familia["joao"].pk  # o mais antigo dos que sobraram
    vivos = PatientContact.objects.filter(contact=familia["contato"])
    assert vivos.count() == 2
    assert vivos.filter(is_primary=True).count() == 1


def test_remover_quem_nao_era_principal_nao_promove_ninguem(familia):
    efeito = desvincular(familia["pedro"], familia["contato"])

    assert efeito["promoveu"] is None
    principais = PatientContact.objects.filter(
        contact=familia["contato"], is_primary=True
    )
    assert principais.first().patient_id == familia["mae"].pk


def test_remover_o_ultimo_deixa_o_numero_sem_paciente(clinic_a):
    contato = Contact.objects.create(clinic=clinic_a, wa_id="5585999112233")
    unico = Patient.objects.create(clinic=clinic_a, name="Sozinho")
    vincular(unico, contato)

    efeito = desvincular(unico, contato)

    assert efeito["promoveu"] is None
    assert PatientContact.objects.filter(contact=contato).count() == 0


def test_remover_solta_a_conversa_daquele_paciente(clinic_a, familia):
    canal = Channel.objects.create(
        clinic=clinic_a, provider=WhatsAppProviderKind.FAKE, display_number="5585999990000"
    )
    conversa = Conversation.objects.create(
        clinic=clinic_a,
        channel=canal,
        contact=familia["contato"],
        patient=familia["joao"],
    )

    efeito = desvincular(familia["joao"], familia["contato"])

    conversa.refresh_from_db()
    assert efeito["conversas_soltas"] == 1
    assert conversa.patient_id is None, "vínculo na tela sem vínculo no banco"


def test_remover_nao_apaga_o_paciente(familia):
    desvincular(familia["pedro"], familia["contato"])
    assert Patient.objects.filter(pk=familia["pedro"].pk).exists()


def test_vincular_de_novo_depois_de_remover(familia):
    """Soft delete não pode travar a recriação do vínculo."""
    desvincular(familia["pedro"], familia["contato"])
    vinculo, criado = vincular(familia["pedro"], familia["contato"])
    assert criado is True
    assert vinculo.is_primary is False  # a mãe continua principal


# ------------------------- ordem e contato por número -------------------------


def test_ordem_da_secao_principal_primeiro_depois_por_nome(familia):
    definir_principal(familia["pedro"], familia["contato"])
    nomes = [v.patient.name for v in pacientes_do_contato(familia["contato"])]
    assert nomes == ["Pedro Silva", "João Silva", "Maria Silva"]


def test_contato_do_numero_acha_pela_grafia_alternativa(clinic_a):
    existente = Contact.objects.create(clinic=clinic_a, wa_id="558599112233")

    contact, criado = contato_do_numero(clinic_a, "(85) 99911-2233")

    assert criado is False
    assert contact.pk == existente.pk


def test_contato_do_numero_cria_no_canonico(clinic_a):
    contact, criado = contato_do_numero(clinic_a, "8599112233", display_name="Marta")
    assert criado is True
    assert contact.wa_id == "5585999112233"


def test_contato_do_numero_sem_telefone(clinic_a):
    assert contato_do_numero(clinic_a, "") == (None, False)
