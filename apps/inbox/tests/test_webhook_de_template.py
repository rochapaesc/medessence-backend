"""
O veredito da Meta chegando por webhook (RF-INB-3.2.10).

⚠️ O que estes testes protegem: sem isto o status só se atualizava no beat de
6 em 6 horas, e um template aprovado em dois minutos ficava `EM REVISÃO` para
a clínica por horas. E, mais importante, o payload que traz esses eventos é o
MESMO das mensagens de paciente — um campo novo que a Meta invente não pode
derrubar o lote e fazer conversa se perder.

Os três campos e o comportamento vêm do `template-webhook.ts` do wacrm (MIT).
"""

import pytest

from apps.inbox.models import WhatsAppTemplate
from apps.inbox.template_webhook import aplicar, e_de_template


@pytest.fixture
def template(clinic_a):
    return WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="retorno_paciente",
        language="pt_BR",
        category="MARKETING",
        status="PENDING",
        meta_template_id="tpl-1",
    )


def _payload(**extras) -> dict:
    return {"message_template_id": "tpl-1", **extras}


class TestQuaisCamposSaoNOSSOS:
    def test_os_tres_do_ciclo_de_vida(self):
        for field in (
            "message_template_status_update",
            "message_template_quality_update",
            "message_template_components_update",
        ):
            assert e_de_template(field)

    def test_mensagem_e_status_de_entrega_NAO_sao(self):
        """Eles seguem pelo caminho de sempre, e não por aqui."""
        assert not e_de_template("messages")
        assert not e_de_template("message_status")


@pytest.mark.django_db
class TestOVeredito:
    def test_aprovado_muda_o_status(self, clinic_a, template):
        aplicar(
            clinic_a,
            "message_template_status_update",
            _payload(event="APPROVED"),
        )
        template.refresh_from_db()
        assert template.status == "APPROVED"

    def test_recusado_guarda_o_motivo(self, clinic_a, template):
        aplicar(
            clinic_a,
            "message_template_status_update",
            _payload(event="REJECTED", reason="Conteúdo promocional demais."),
        )
        template.refresh_from_db()
        assert template.status == "REJECTED"
        assert template.rejection_reason == "Conteúdo promocional demais."

    def test_aprovar_depois_de_recusar_LIMPA_o_motivo(self, clinic_a, template):
        """
        ⚠️ O motivo só vem no REJECTED. Sem limpar, a tela mostraria o aviso
        vermelho da recusa anterior depois de a Meta ter aprovado o texto
        corrigido — e a clínica acharia que ainda está reprovado.
        """
        template.status = "REJECTED"
        template.rejection_reason = "Conteúdo promocional demais."
        template.save(update_fields=["status", "rejection_reason"])

        aplicar(
            clinic_a,
            "message_template_status_update",
            _payload(event="APPROVED"),
        )
        template.refresh_from_db()
        assert template.status == "APPROVED"
        assert template.rejection_reason == ""

    def test_pending_review_e_apelido_de_pending(self, clinic_a, template):
        template.status = "APPROVED"
        template.save(update_fields=["status"])
        aplicar(
            clinic_a,
            "message_template_status_update",
            _payload(event="PENDING_REVIEW"),
        )
        template.refresh_from_db()
        assert template.status == "PENDING"

    def test_sem_event_nao_mexe(self, clinic_a, template):
        assert aplicar(clinic_a, "message_template_status_update", _payload())
        template.refresh_from_db()
        assert template.status == "PENDING"


@pytest.mark.django_db
class TestAQualidade:
    def test_vermelho_fica_guardado(self, clinic_a, template):
        """
        ⚠️ Vermelho é o passo antes de a Meta PAUSAR o template sozinha — e
        template pausado para de enviar no meio de um fluxo, sem ninguém ter
        mexido em nada.
        """
        aplicar(
            clinic_a,
            "message_template_quality_update",
            _payload(new_quality_score="RED"),
        )
        template.refresh_from_db()
        assert template.quality_score == "RED"

    def test_nota_desconhecida_vira_vazio(self, clinic_a, template):
        aplicar(
            clinic_a,
            "message_template_quality_update",
            _payload(new_quality_score="ROXO"),
        )
        template.refresh_from_db()
        assert template.quality_score == ""


@pytest.mark.django_db
class TestQuandoNaoDaParaAplicar:
    def test_template_de_outro_produto_no_mesmo_waba_e_ignorado(self, clinic_a):
        """
        O WABA é compartilhado: chega evento de template que não é nosso. Não
        é erro — a resposta precisa continuar sendo 200, senão a Meta
        reentrega o lote inteiro em laço.
        """
        assert "desconhecido" in aplicar(
            clinic_a,
            "message_template_status_update",
            {"message_template_id": "de-outro", "event": "APPROVED"},
        )

    def test_sem_id_nao_estoura(self, clinic_a):
        assert "sem message_template_id" in aplicar(
            clinic_a, "message_template_status_update", {"event": "APPROVED"}
        )

    def test_campo_que_a_meta_invente_e_ignorado(self, clinic_a):
        assert aplicar(clinic_a, "message_template_coisa_nova", {}) == "ignorado"

    def test_o_id_NAO_atravessa_a_clinica(self, clinic_a, clinic_b, template):
        """
        O id é único por WABA, mas a busca é escopada pela clínica: sem isso,
        um evento aplicaria no template de outro tenant que tivesse o mesmo id.
        """
        aplicar(
            clinic_b,
            "message_template_status_update",
            _payload(event="APPROVED"),
        )
        template.refresh_from_db()
        assert template.status == "PENDING"


@pytest.mark.django_db
def test_o_evento_NAO_grava_os_componentes_que_a_meta_mudou(clinic_a, template):
    """
    ⚠️ O evento avisa que ela mexeu, mas não traz o texto novo. Gravar por
    cima do que a clínica escreveu, sem ela ver, faria a tela mostrar um
    template que ninguém aprovou. O caminho é o botão "Atualizar".
    """
    antes = template.components
    aplicar(
        clinic_a,
        "message_template_components_update",
        _payload(message_template_name="retorno_paciente"),
    )
    template.refresh_from_db()
    assert template.components == antes


@pytest.mark.django_db
def test_o_lote_de_mensagens_NAO_cai_por_causa_do_template(
    clinic_a, inbox_a, monkeypatch
):
    """
    ⚠️ O pior negócio possível: o webhook traz mensagem de paciente no mesmo
    payload, e a Meta reentrega tudo em laço quando a resposta não é 200.
    Um campo de template que ela invente não pode fazer conversa se perder.
    """
    from apps.inbox.models import Message, WebhookEvent
    from apps.inbox.tasks import process_whatsapp_webhook

    monkeypatch.setattr(
        "apps.inbox.template_webhook.aplicar",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    channel = inbox_a["channel"]
    evento = WebhookEvent.objects.create(
        payload={
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "metadata": {
                                    "phone_number_id": channel.phone_number_id
                                },
                                "contacts": [
                                    {"wa_id": "5589999", "profile": {"name": "Ana"}}
                                ],
                                "messages": [
                                    {
                                        "id": "wamid.NOVA",
                                        "from": "5589999",
                                        "type": "text",
                                        "timestamp": "1760000000",
                                        "text": {"body": "oi"},
                                    }
                                ],
                            },
                        },
                        {
                            "field": "message_template_status_update",
                            "value": {"message_template_id": "x", "event": "APPROVED"},
                        },
                    ]
                }
            ]
        },
        source="meta",
    )

    process_whatsapp_webhook(evento.pk, channel.pk)

    assert Message.objects.filter(provider_message_id="wamid.NOVA").exists()
    evento.refresh_from_db()
    assert evento.processed_at is not None, "o lote precisa fechar como processado"
