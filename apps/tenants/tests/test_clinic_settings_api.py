"""
Configurações da clínica pela API (§4.13, RF-CFG-2).

O que se muda aqui alcança a clínica inteira: o nome sai nas mensagens ao
paciente, o fuso decide quando a sequência dispara e a janela redefine quem é
paciente ativo. Daí o cuidado com o que NÃO é editável.
"""

import pytest

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.models import AuditLog
from conftest import make_user

pytestmark = pytest.mark.django_db

URL = "/api/v1/clinic/"


@pytest.fixture
def gestor(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


class TestLer:
    def test_devolve_os_campos_da_clinica(self, gestor, clinic_a):
        resposta = gestor.get(URL)

        assert resposta.status_code == 200
        assert resposta.data["name"] == clinic_a.name
        assert resposta.data["timezone"] == clinic_a.timezone
        assert resposta.data["active_window_days"] == clinic_a.active_window_days
        # Contexto de LEITURA do cartão (RF-CFG-2.6): identificador e idade.
        assert resposta.data["slug"] == clinic_a.slug
        assert resposta.data["created_at"] is not None

    def test_created_at_nao_se_edita(self, gestor, clinic_a):
        """RF-CFG-2.6: a idade da clínica é contexto, não opção."""
        antes = clinic_a.created_at
        resposta = gestor.patch(
            URL, {"created_at": "2020-01-01T00:00:00Z"}, format="json"
        )

        assert resposta.status_code == 200
        clinic_a.refresh_from_db()
        assert clinic_a.created_at == antes

    def test_nao_expoe_credencial_nem_trava_do_ehr(self, gestor):
        """
        ⚠️ O que está fora da tela precisa estar fora da RESPOSTA: credencial
        do prontuário é da plataforma, e `ehr_push_enabled` é trava de
        segurança que se liga por comando depois de validar a leitura.
        """
        resposta = gestor.get(URL)

        for campo in (
            "ehr_credentials",
            "ehr_provider",
            "ehr_push_enabled",
            "ehr_external_tenant_id",
            "appointments_backfilled_until",
        ):
            assert campo not in resposta.data


class TestGravar:
    def test_muda_nome_fuso_e_janela(self, gestor, clinic_a):
        resposta = gestor.patch(
            URL,
            {
                "name": "Clínica Alfa Renomeada",
                "timezone": "America/Sao_Paulo",
                "active_window_days": 180,
            },
            format="json",
        )

        assert resposta.status_code == 200
        clinic_a.refresh_from_db()
        assert clinic_a.name == "Clínica Alfa Renomeada"
        assert clinic_a.timezone == "America/Sao_Paulo"
        assert clinic_a.active_window_days == 180

    def test_o_identificador_nao_muda(self, gestor, clinic_a):
        """O slug é chave, não nome: muda comando e endereço."""
        original = clinic_a.slug

        gestor.patch(URL, {"slug": "outro-slug"}, format="json")

        clinic_a.refresh_from_db()
        assert clinic_a.slug == original

    def test_trava_do_ehr_nao_se_liga_pela_tela(self, gestor, clinic_a):
        """
        ⚠️ `ehr_push_enabled` é a trava do write-through. Aceitá-la aqui
        transformaria uma decisão de segurança numa opção comum de formulário.
        """
        assert clinic_a.ehr_push_enabled is False

        gestor.patch(URL, {"ehr_push_enabled": True}, format="json")

        clinic_a.refresh_from_db()
        assert clinic_a.ehr_push_enabled is False

    def test_nome_vazio_e_recusado(self, gestor, clinic_a):
        resposta = gestor.patch(URL, {"name": "   "}, format="json")

        assert resposta.status_code == 400
        clinic_a.refresh_from_db()
        assert clinic_a.name

    def test_fuso_inventado_e_recusado(self, gestor, clinic_a):
        """
        ⚠️ O fuso é lido pelo disparo da sequência, pela contagem da agenda e
        pelo horário de funcionamento: um valor que o `ZoneInfo` não conhece
        quebraria os três de uma vez, longe daqui.
        """
        original = clinic_a.timezone

        resposta = gestor.patch(URL, {"timezone": "Marte/Olimpo"}, format="json")

        assert resposta.status_code == 400
        clinic_a.refresh_from_db()
        assert clinic_a.timezone == original

    @pytest.mark.parametrize("dias", [1, 5000])
    def test_janela_fora_da_faixa_e_recusada(self, gestor, clinic_a, dias):
        """Um dia deixaria a clínica inteira inativa amanhã."""
        resposta = gestor.patch(URL, {"active_window_days": dias}, format="json")

        assert resposta.status_code == 400


class TestRastro:
    def test_a_mudanca_deixa_rastro_com_o_antes_e_o_depois(self, gestor, clinic_a):
        gestor.patch(URL, {"active_window_days": 180}, format="json")

        log = AuditLog.objects.filter(action="UPDATE", resource="Clinic").first()
        assert log is not None
        assert log.payload["changed_fields"] == ["active_window_days"]
        # Sem o antes e o depois, o registro não responde de quanto para quanto.
        assert log.payload["active_window_days_antes"] == 90
        assert log.payload["active_window_days_depois"] == 180
        assert log.clinic_id == clinic_a.pk

    def test_salvar_sem_mudar_nada_nao_polui_a_auditoria(self, gestor, clinic_a):
        gestor.patch(URL, {"name": clinic_a.name}, format="json")

        assert not AuditLog.objects.filter(action="UPDATE", resource="Clinic").exists()


class TestCerca:
    @pytest.mark.parametrize("papel", [MembershipRole.ATTENDANT, MembershipRole.DOCTOR])
    def test_quem_nao_e_gestor_nao_le_nem_grava(self, api_client, clinic_a, papel):
        """
        Ler também é do gestor: a tela inteira é de configuração, e quem não
        pode mudar não precisa da lista do que existe.
        """
        user = make_user(f"{papel}.config@teste.dev")
        Membership.objects.create(user=user, clinic=clinic_a, role=papel)
        api_client.force_authenticate(user)

        assert api_client.get(URL).status_code == 403
        assert api_client.patch(URL, {"name": "x"}, format="json").status_code == 403
