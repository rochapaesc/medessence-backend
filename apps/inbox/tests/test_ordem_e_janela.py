"""
Dois defeitos que só a fila real mostrou, em 11/08/2026.

Os dois têm a mesma assinatura: o dado no banco estava certo e a TELA mostrava
outra coisa. Um por causa de como o Postgres ordena NULL, outro por um campo
que faltava no evento de tempo real.
"""

from datetime import timedelta

import pytest
from django.db.models import F
from django.utils import timezone

from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation
from apps.patients.models import Contact

pytestmark = pytest.mark.django_db

URL = "/api/v1/conversations/"


def conversa(clinic, canal, wa_id, *, last_message_at, last_inbound_at=None):
    contato = Contact.objects.create(clinic=clinic, wa_id=wa_id, display_name=f"C{wa_id[-2:]}")
    return Conversation.objects.create(
        clinic=clinic,
        channel=canal,
        contact=contato,
        last_message_at=last_message_at,
        last_inbound_at=last_inbound_at,
    )


@pytest.fixture
def canal(clinic_a):
    return Channel.objects.create(
        clinic=clinic_a,
        provider=WhatsAppProviderKind.FAKE,
        display_number="5585999990000",
    )


@pytest.fixture
def atendente(api_client, attendant_a):
    api_client.force_authenticate(attendant_a)
    return api_client


class TestOrdemDaFila:
    def test_a_ordenacao_pede_nulls_last_no_sql(self, atendente, clinic_a, canal):
        """
        ⚠️ Este teste olha o SQL, e não o resultado, DE PROPÓSITO.

        No PostgreSQL (produção) `ORDER BY campo DESC` traz NULL **primeiro**,
        então toda conversa sem mensagem nenhuma subia acima da que acabou de
        chegar. O cliente ordena com NULL por último, então a lista se
        reorganizava sozinha assim que alguém resolvia qualquer conversa.

        A suíte roda em **SQLite** (`config/settings_test`), que faz o
        OPOSTO: NULL por último em DESC. Ou seja, conferir a ordem do
        resultado aqui passa mesmo com o defeito no lugar - foi assim que ele
        chegou à tela do usuário. O que dá para afirmar nos dois bancos é a
        INTENÇÃO, escrita no SQL.
        """
        agora = timezone.now()
        conversa(clinic_a, canal, "5585900000001", last_message_at=None)
        conversa(clinic_a, canal, "5585900000002", last_message_at=agora)

        from apps.inbox.api.viewsets import ConversationViewSet

        view = ConversationViewSet()
        view.request = type("R", (), {"query_params": {}, "user": None})()
        view.clinic = clinic_a
        view.membership = None
        sql = str(
            Conversation.objects.filter(clinic=clinic_a, deleted_at__isnull=True)
            .order_by(F("last_message_at").desc(nulls_last=True))
            .query
        ).upper()
        assert "NULLS LAST" in sql

        # E a view precisa usar exatamente essa ordenação.
        import inspect

        fonte = inspect.getsource(ConversationViewSet.get_queryset)
        assert "nulls_last=True" in fonte, "a fila voltou a ordenar sem NULLS LAST"

    def test_quem_falou_por_ultimo_aparece_primeiro(self, atendente, clinic_a, canal):
        agora = timezone.now()
        recente = conversa(clinic_a, canal, "5585900000008", last_message_at=agora)
        antiga = conversa(
            clinic_a, canal, "5585900000009", last_message_at=agora - timedelta(days=3)
        )

        resposta = atendente.get(URL)

        ids = [c["id"] for c in resposta.data["results"]]
        assert ids[:2] == [recente.pk, antiga.pk]

    def test_a_ordem_e_recencia_pura(self, atendente, clinic_a, canal):
        """A prioridade não reordena (RF-ATD-8): ela é tarja, selo e filtro."""
        agora = timezone.now()
        velha_urgente = conversa(
            clinic_a, canal, "5585900000004", last_message_at=agora - timedelta(hours=5)
        )
        velha_urgente.priority = "urgent"
        velha_urgente.save(update_fields=["priority"])
        nova = conversa(clinic_a, canal, "5585900000005", last_message_at=agora)

        resposta = atendente.get(URL)

        assert [c["id"] for c in resposta.data["results"]][:2] == [nova.pk, velha_urgente.pk]


class TestJanelaNoTempoReal:
    def test_o_evento_carrega_a_janela(self, clinic_a, canal):
        """
        ⚠️ A janela de 24h sai de `last_inbound_at`, então a mensagem do
        paciente a ABRE no mesmo instante em que dispara este evento. Sem o
        campo no payload, a tela ficava com o valor da última carga: mostrava
        "janela 24h" com o paciente acabando de escrever, e o composer exigia
        template numa conversa que aceitava texto livre.
        """
        from apps.inbox.realtime import notify_conversation_updated

        c = conversa(
            clinic_a,
            canal,
            "5585900000006",
            last_message_at=timezone.now(),
            last_inbound_at=timezone.now(),
        )
        enviados = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("apps.inbox.realtime._broadcast", lambda cid, p: enviados.append(p))
            notify_conversation_updated(c)

        payload = enviados[0]
        assert payload["window_open"] is True
        assert payload["last_inbound_at"] is not None

    def test_janela_fechada_tambem_viaja(self, clinic_a, canal):
        """O campo tem de vir nos DOIS estados: só quando `True` faria a tela
        nunca voltar a fechar a janela depois de aberta."""
        from apps.inbox.realtime import notify_conversation_updated

        c = conversa(
            clinic_a,
            canal,
            "5585900000007",
            last_message_at=timezone.now(),
            last_inbound_at=timezone.now() - timedelta(hours=30),
        )
        enviados = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("apps.inbox.realtime._broadcast", lambda cid, p: enviados.append(p))
            notify_conversation_updated(c)

        assert enviados[0]["window_open"] is False
