"""
RF-INB-3.3: o catálogo de templates é da CONTA da Meta, não da clínica.

O usuário trocou o número e o app da MedEssence e a tela passou a mostrar, lado
a lado, templates de duas contas, sem como saber qual era de qual. Escolher um
da conta antiga é envio recusado na frente do paciente.
"""

import pytest

from apps.inbox.models import Channel, WhatsAppTemplate
from apps.inbox.template_scope import conta_da_clinica, template_por_nome

pytestmark = pytest.mark.django_db

URL = "/api/v1/wa-templates/"

CONTA_NOVA = "111111111111111"
CONTA_ANTIGA = "999999999999999"


def _template(clinic, nome, waba_id, **extra):
    return WhatsAppTemplate.objects.create(
        clinic=clinic,
        waba_id=waba_id,
        name=nome,
        language=extra.pop("language", "pt_BR"),
        status=extra.pop("status", "APPROVED"),
        meta_template_id=extra.pop("meta_template_id", "abc123"),
        **extra,
    )


@pytest.fixture
def canal_com_conta(clinic_a):
    """O canal da clínica apontando para a conta NOVA."""
    canal = Channel.objects.filter(clinic=clinic_a, is_test=False).first()
    if canal is None:
        from apps.inbox.choices import WhatsAppProviderKind

        canal = Channel.objects.create(
            clinic=clinic_a,
            provider=WhatsAppProviderKind.FAKE,
            display_number="5585999990000",
        )
    canal.waba_id = CONTA_NOVA
    canal.save(update_fields=["waba_id"])
    return canal


def test_a_conta_da_clinica_sai_do_canal(clinic_a, canal_com_conta):
    assert conta_da_clinica(clinic_a.pk) == CONTA_NOVA


def test_template_da_conta_antiga_nao_aparece_na_tela(
    api_client, manager_single_clinic, clinic_a, canal_com_conta
):
    _template(clinic_a, "confirmacao", CONTA_NOVA)
    _template(clinic_a, "comunicado_escolar", CONTA_ANTIGA)

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(URL)

    assert resposta.status_code == 200
    nomes = {t["name"] for t in resposta.data["results"]}
    assert nomes == {"confirmacao"}


def test_mesmo_nome_em_duas_contas_COEXISTE_e_nao_se_sobrescreve(
    clinic_a, canal_com_conta
):
    """
    ⚠️ Era o defeito silencioso: a unicidade sem a conta fazia a sincronização
    SOBRESCREVER a definição antiga com a nova. Quem mandava mensagem montava os
    parâmetros com os componentes da conta errada e a Meta recusava.
    """
    novo = _template(clinic_a, "confirmacao", CONTA_NOVA, components=[{"type": "BODY"}])
    velho = _template(clinic_a, "confirmacao", CONTA_ANTIGA, components=[])

    assert novo.pk != velho.pk
    # E a leitura pelo nome traz o da conta EM USO, não o primeiro que aparecer.
    assert template_por_nome(clinic_a.pk, "confirmacao").pk == novo.pk


def test_leitura_por_nome_ignora_a_conta_antiga(clinic_a, canal_com_conta):
    _template(clinic_a, "so_na_antiga", CONTA_ANTIGA)

    assert template_por_nome(clinic_a.pk, "so_na_antiga") is None


def test_o_envio_recusa_template_que_nao_e_da_conta_atual(
    api_client, manager_single_clinic, clinic_a, inbox_a
):
    """
    O caminho que importa: a validação do envio. Antes ela achava o template da
    conta antiga, deixava passar, e a recusa vinha da Meta depois.

    Sem a fixture do canal: `inbox_a` já cria um, e a clínica só aceita um.
    """
    from apps.inbox.choices import SenderKind
    from apps.inbox.tests.conftest import make_message

    canal = inbox_a["channel"]
    canal.waba_id = CONTA_NOVA
    canal.save(update_fields=["waba_id"])

    conversation = inbox_a["conversation"]
    make_message(conversation, sender_kind=SenderKind.CONTACT)
    _template(clinic_a, "da_conta_antiga", CONTA_ANTIGA)

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        "/api/v1/messages/",
        {
            "conversation": conversation.pk,
            "kind": "template",
            "template_name": "da_conta_antiga",
        },
        format="json",
    )

    assert resposta.status_code == 400
    assert "não está aprovado nesta conta" in str(resposta.data)


# ---- a varredura do que sumiu da Meta ----


def _resposta_da_meta(*nomes):
    class _T:
        def __init__(self, nome):
            self.name = nome
            self.language = "pt_BR"
            self.category = "UTILITY"
            self.status = "APPROVED"
            self.components = []
            self.meta_id = f"id-{nome}"
            self.parameter_format = "POSITIONAL"

    return [_T(n) for n in nomes]


def test_o_que_sumiu_da_meta_some_daqui(clinic_a, canal_com_conta):
    from apps.inbox.tasks import _varrer_o_que_sumiu

    _template(clinic_a, "continua", CONTA_NOVA)
    _template(clinic_a, "apagado_la", CONTA_NOVA)

    _varrer_o_que_sumiu(canal_com_conta, _resposta_da_meta("continua"))

    nomes = set(
        WhatsAppTemplate.objects.filter(clinic=clinic_a).values_list("name", flat=True)
    )
    assert nomes == {"continua"}


def test_lista_vazia_NAO_varre_nada(clinic_a, canal_com_conta):
    """
    ⚠️ A guarda do Chatwoot. Resposta vazia por token trocado, permissão ou
    paginação interrompida apagaria o catálogo inteiro de quem não fez nada.
    """
    from apps.inbox.tasks import _varrer_o_que_sumiu

    _template(clinic_a, "continua", CONTA_NOVA)

    _varrer_o_que_sumiu(canal_com_conta, [])

    assert WhatsAppTemplate.objects.filter(clinic=clinic_a).count() == 1


def test_rascunho_local_sobrevive_a_varredura(clinic_a, canal_com_conta):
    """Regra do wacrm: template sem par na Meta não podia constar na resposta
    dela, e apagá-lo seria perder o que a clínica escreveu e não submeteu."""
    from apps.inbox.tasks import _varrer_o_que_sumiu

    _template(clinic_a, "rascunho", CONTA_NOVA, meta_template_id="", status="DRAFT")

    _varrer_o_que_sumiu(canal_com_conta, _resposta_da_meta("outro_qualquer"))

    assert WhatsAppTemplate.objects.filter(clinic=clinic_a, name="rascunho").exists()


def test_a_varredura_nao_encosta_na_conta_antiga(clinic_a, canal_com_conta):
    """Quem tira o da conta antiga é o `templates_por_conta --limpar`, não a
    sincronização: ela só responde pela conta que acabou de consultar."""
    from apps.inbox.tasks import _varrer_o_que_sumiu

    _template(clinic_a, "da_antiga", CONTA_ANTIGA)

    _varrer_o_que_sumiu(canal_com_conta, _resposta_da_meta("qualquer"))

    assert WhatsAppTemplate.objects.filter(clinic=clinic_a, name="da_antiga").exists()
