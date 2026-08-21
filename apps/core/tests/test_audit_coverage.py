"""
O que a auditoria REGISTRA (§15) - a varredura de 20/08/2026.

O `AuditMixin` cobre `perform_create/update/destroy`, e por isso a auditoria
só enxergava cadastro. Metade do sistema não é cadastro: encerrar atendimento,
transferir conversa, publicar fluxo, inscrever numa trilha e sair do sistema
são `@action` e views próprias, e nenhuma delas deixava linha nenhuma.

Metade destes testes são provas NEGATIVAS, e elas são o ponto: uma auditoria
que registra tudo, inclusive o clique de abrir uma conversa, é tão inútil
quanto uma que não registra nada. O gestor para de ler.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import FlowStatus, SequenceEnrollmentStatus
from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
from apps.automation.tests.conftest import (
    make_channel,
    make_contact,
    make_conversation,
    make_flow,
)
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.inbox.tests.conftest import make_message
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db


def _url(conversation, acao):
    return f"/api/v1/conversations/{conversation.pk}/{acao}/"


def operacoes(clinic=None):
    """As operações registradas, na ordem em que aconteceram."""
    consulta = AuditLog.objects.all()
    if clinic is not None:
        consulta = consulta.filter(clinic=clinic)
    return [linha.payload.get("operation") for linha in consulta.order_by("id")]


@pytest.fixture
def conversation(clinic_a):
    """Canal, contato e conversa: o mínimo para exercitar as ações."""
    return make_conversation(clinic_a, make_contact(clinic_a), make_channel(clinic_a))


@pytest.fixture
def flow_a(clinic_a):
    return make_flow(clinic_a)


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


@pytest.fixture
def colega(db, clinic_a):
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    user = make_user("colega.auditoria@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.ATTENDANT)
    return user


# --------------------------------------------------------------------- #
# Atendimento
# --------------------------------------------------------------------- #


class TestAcoesDeConversa:
    def test_encerrar_deixa_linha_com_a_operacao(self, logado, conversation, clinic_a):
        logado.post(_url(conversation, "resolve"), {}, format="json")

        linha = AuditLog.objects.get()
        assert linha.action == AuditAction.UPDATE
        assert linha.resource == "Conversation"
        assert linha.resource_id == str(conversation.pk)
        assert linha.payload["operation"] == "conversation.resolve"
        # A clínica é o que permite ao gestor ver isto na tela dele.
        assert linha.clinic_id == clinic_a.pk

    def test_a_nota_interna_do_encerramento_NAO_vai_para_o_log(self, logado, conversation):
        """
        ⚠️ A nota é conteúdo do atendimento, e conteúdo não se copia para a
        auditoria: seria um segundo lugar guardando o que o paciente contou.
        """
        logado.post(
            _url(conversation, "resolve"),
            {"note": "Paciente relatou dor no peito e vai ao pronto-socorro."},
            format="json",
        )

        despejo = str(AuditLog.objects.get().payload)
        assert "dor no peito" not in despejo
        assert "note" not in despejo

    def test_transferir_guarda_para_quem_foi(self, logado, conversation, colega):
        logado.post(_url(conversation, "assign"), {}, format="json")
        logado.post(_url(conversation, "transfer"), {"to": colega.pk}, format="json")

        transferencia = AuditLog.objects.order_by("id").last()
        assert transferencia.payload["operation"] == "conversation.transfer"
        assert transferencia.payload["to"] == colega.pk

    def test_assumir_para_si_e_atribuir_a_outro_sao_operacoes_diferentes(
        self, logado, conversation, colega
    ):
        """
        Quem assumiu o próprio atendimento e quem PÔS outra pessoa nele
        respondem a perguntas diferentes na auditoria.
        """
        logado.post(_url(conversation, "assign"), {}, format="json")
        logado.post(_url(conversation, "assign"), {"assigned_to": colega.pk}, format="json")

        assert operacoes() == ["conversation.assign", "conversation.assign_other"]

    @pytest.mark.parametrize(
        ("acao", "corpo", "esperado"),
        [
            ("mark-waiting", {}, "conversation.wait"),
            ("reopen", {}, "conversation.reopen"),
            ("priority", {"priority": "high"}, "conversation.priority"),
        ],
    )
    def test_cada_acao_tem_a_sua_operacao(self, logado, conversation, acao, corpo, esperado):
        resposta = logado.post(_url(conversation, acao), corpo, format="json")

        assert resposta.status_code == 200
        assert operacoes() == [esperado]

    def test_adiar_guarda_ate_quando(self, logado, conversation):
        ate = timezone.now() + timedelta(days=1)
        logado.post(_url(conversation, "snooze"), {"until": ate.isoformat()}, format="json")

        assert AuditLog.objects.get().payload["operation"] == "conversation.snooze"
        assert AuditLog.objects.get().payload["until"].startswith(ate.date().isoformat())

    def test_marcar_e_desmarcar_assunto(self, logado, conversation, clinic_a):
        from apps.inbox.models import ConversationLabel

        etiqueta = ConversationLabel.objects.create(
            clinic=clinic_a, name="Retorno", color="#112233"
        )
        logado.post(_url(conversation, "add-label"), {"label": etiqueta.pk}, format="json")
        logado.post(_url(conversation, "remove-label"), {"label": etiqueta.pk}, format="json")

        assert operacoes() == ["conversation.label_add", "conversation.label_remove"]
        assert AuditLog.objects.order_by("id").first().payload["label"] == etiqueta.pk

    def test_ABRIR_conversa_nao_vira_evento(self, logado, conversation):
        """
        PROVA NEGATIVA, e a mais importante do arquivo.

        A recepção abre dezenas de conversas por hora, e `read` é disparado a
        cada uma. Registrar isso afogaria os eventos que importam num mar de
        cliques de navegação - a auditoria continuaria "completa" e ninguém
        mais conseguiria ler nada nela.
        """
        make_message(conversation)
        conversation.unread_count = 3
        conversation.save(update_fields=["unread_count"])

        resposta = logado.post(_url(conversation, "read"), {}, format="json")

        assert resposta.status_code == 200
        assert not AuditLog.objects.exists()

    def test_LISTAR_e_ABRIR_o_detalhe_tambem_nao(self, logado, conversation):
        """A leitura da fila é navegação, não acesso a documento (§15)."""
        logado.get("/api/v1/conversations/")
        logado.get(f"/api/v1/conversations/{conversation.pk}/")

        assert not AuditLog.objects.exists()


class TestVinculoComPaciente:
    def test_iniciar_conversa_registra_uma_vez_so(self, logado, clinic_a, conversation):
        """
        A conversa nasce uma vez. Chamar `start` de novo devolve a MESMA
        conversa, e registrar esse segundo clique seria registrar navegação.
        """
        paciente = Patient.objects.create(clinic=clinic_a, name="Raimunda", phone="5585911112222")

        primeira = logado.post(
            "/api/v1/conversations/start/", {"patient": paciente.pk}, format="json"
        )
        segunda = logado.post(
            "/api/v1/conversations/start/", {"patient": paciente.pk}, format="json"
        )

        assert (primeira.status_code, segunda.status_code) == (201, 200)
        assert operacoes() == ["conversation.start"]
        assert AuditLog.objects.get().action == AuditAction.CREATE

    def test_vincular_e_desvincular_guardam_o_paciente(self, logado, conversation, clinic_a):
        paciente = Patient.objects.create(clinic=clinic_a, name="Genivaldo")

        logado.post(_url(conversation, "link-patient"), {"patient": paciente.pk}, format="json")
        logado.post(_url(conversation, "unlink-patient"), {}, format="json")

        linhas = list(AuditLog.objects.order_by("id"))
        assert operacoes() == ["conversation.link_patient", "conversation.unlink_patient"]
        # ⚠️ O de SAÍDA é o que se perderia: depois do save a conversa não
        # aponta mais para ninguém, e sem isto a linha diria só "desvinculou".
        assert linhas[1].payload["patient"] == paciente.pk

    def test_mexer_no_numero_e_uma_operacao_de_contato(self, logado, conversation, clinic_a):
        """RF-PAC-7.1: o número é da casa, e mexer nele afeta outras fichas."""
        um = Patient.objects.create(clinic=clinic_a, name="Filho")
        dois = Patient.objects.create(clinic=clinic_a, name="Filha")

        for paciente in (um, dois):
            logado.post(
                _url(conversation, "add-contact-patient"),
                {"patient": paciente.pk},
                format="json",
            )
        logado.post(
            _url(conversation, "set-primary-patient"), {"patient": dois.pk}, format="json"
        )
        logado.post(
            _url(conversation, "remove-contact-patient"), {"patient": dois.pk}, format="json"
        )

        assert operacoes() == [
            "contact.patient_add",
            "contact.patient_add",
            "contact.patient_primary",
            "contact.patient_remove",
        ]
        saida = AuditLog.objects.order_by("id").last()
        # Quantas conversas ficaram sem paciente por tabela.
        assert "released" in saida.payload


# --------------------------------------------------------------------- #
# Automação
# --------------------------------------------------------------------- #


class TestFluxos:
    @pytest.fixture
    def fluxo_publicavel(self, clinic_a):
        from apps.automation.choices import FlowNodeType

        def no(node_id, tipo, **config):
            return {"id": node_id, "type": tipo, "label": node_id, "config": config}

        return make_flow(
            clinic_a,
            graph={
                "entry_node": "n1",
                "nodes": [
                    no("n1", FlowNodeType.START),
                    no("msg", FlowNodeType.SEND_MESSAGE, text="Olá!"),
                    no("fim", FlowNodeType.END),
                ],
                "edges": [
                    {"from": "n1", "to": "msg", "condition": "default"},
                    {"from": "msg", "to": "fim", "condition": "default"},
                ],
            },
        )

    def test_publicar_e_tirar_do_ar(self, logado, fluxo_publicavel):
        url = f"/api/v1/flows/{fluxo_publicavel.pk}/"

        publicar = logado.post(f"{url}activate/", {}, format="json")
        logado.post(f"{url}deactivate/", {}, format="json")

        assert publicar.status_code == 200, publicar.data
        assert operacoes() == ["flow.activate", "flow.deactivate"]

    def test_fluxo_recusado_na_publicacao_nao_deixa_linha(self, logado, clinic_a):
        """
        PROVA NEGATIVA: publicar é o EFEITO, não o clique. Um fluxo que a
        validação recusou continua rascunho, e registrá-lo faria a auditoria
        dizer que foi ao ar algo que nunca falou com paciente nenhum.
        """
        quebrado = make_flow(clinic_a, graph={"nodes": [], "edges": [], "entry_node": ""})

        resposta = logado.post(f"/api/v1/flows/{quebrado.pk}/activate/", {}, format="json")

        assert resposta.status_code == 400
        assert not AuditLog.objects.exists()

    def test_exportar_e_LEITURA(self, logado, flow_a):
        """
        Levar o desenho do fluxo para fora da clínica é leitura de dado da
        clínica, e a linha precisa dizer isso: um `UPDATE` mentiria dizendo
        que alguém mexeu no fluxo.
        """
        resposta = logado.get(f"/api/v1/flows/{flow_a.pk}/export/")

        assert resposta.status_code == 200
        linha = AuditLog.objects.get()
        assert linha.action == AuditAction.READ
        assert linha.payload["operation"] == "flow.export"

    def test_importar_registra_criacao(self, logado, flow_a):
        arquivo = logado.get(f"/api/v1/flows/{flow_a.pk}/export/").data
        AuditLog.objects.all().delete()

        resposta = logado.post(
            "/api/v1/flows/import/", {"arquivo": arquivo, "nome": "Vindo de fora"}, format="json"
        )

        assert resposta.status_code == 201, resposta.data
        linha = AuditLog.objects.get()
        assert linha.action == AuditAction.CREATE
        assert linha.payload["operation"] == "flow.import"


class TestSequencias:
    @pytest.fixture
    def trilha(self, clinic_a):
        sequence = Sequence.objects.create(clinic=clinic_a, name="Pós-consulta", is_active=True)
        SequenceStep.objects.create(
            sequence=sequence,
            order=1,
            offset_days=1,
            send_time=time(8, 0),
            flow=make_flow(clinic_a, name="Aviso", status=FlowStatus.ACTIVE),
        )
        return sequence

    @pytest.fixture
    def paciente(self, clinic_a):
        patient = Patient.objects.create(clinic=clinic_a, name="Ivanita")
        PatientContact.objects.create(patient=patient, contact=make_contact(clinic_a))
        return patient

    def test_inscrever_pela_ficha(self, logado, trilha, paciente):
        resposta = logado.post(
            f"/api/v1/sequences/{trilha.pk}/enroll/", {"patient": paciente.pk}, format="json"
        )

        assert resposta.status_code == 201
        linha = AuditLog.objects.get()
        assert linha.action == AuditAction.CREATE
        assert linha.resource == "Sequence"
        assert linha.payload["operation"] == "sequence.enroll"
        assert linha.payload["patient"] == paciente.pk

    def test_tirar_da_trilha(self, logado, trilha, paciente):
        logado.post(
            f"/api/v1/sequences/{trilha.pk}/enroll/", {"patient": paciente.pk}, format="json"
        )
        logado.post(
            f"/api/v1/sequences/{trilha.pk}/unenroll/", {"patient": paciente.pk}, format="json"
        )

        assert operacoes() == ["sequence.enroll", "sequence.unenroll"]
        assert SequenceEnrollment.objects.get().status == SequenceEnrollmentStatus.CANCELED

    def test_sair_de_onde_nao_se_esta_nao_e_evento(self, logado, trilha, paciente):
        """
        PROVA NEGATIVA: o `unenroll` é idempotente e responde 200 mesmo sem
        inscrição. Sem esta guarda, a tela de auditoria mostraria remoções que
        nunca aconteceram.
        """
        resposta = logado.post(
            f"/api/v1/sequences/{trilha.pk}/unenroll/", {"patient": paciente.pk}, format="json"
        )

        assert resposta.status_code == 200
        assert not AuditLog.objects.exists()

    def test_lote_guarda_CONTAGENS_e_nao_a_lista_de_pacientes(
        self, logado, trilha, clinic_a, paciente
    ):
        """
        ⚠️ Um lote da fila de resgate leva mais de mil pacientes. Guardar os
        ids transformaria o log de auditoria num segundo cadastro de quem tem
        qual condição - e ele é lido por gente que não abriria essas fichas.
        """
        sem_numero = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")

        resposta = logado.post(
            f"/api/v1/sequences/{trilha.pk}/enroll-batch/",
            {"patients": [paciente.pk, sem_numero.pk]},
            format="json",
        )

        assert resposta.status_code == 201
        payload = AuditLog.objects.get().payload
        assert payload["operation"] == "sequence.enroll_batch"
        assert (payload["inscritos"], payload["sem_numero"]) == (1, 1)
        assert payload["origem"] == "selecao"
        assert str(sem_numero.pk) not in str(payload.get("patients", ""))
        assert "patients" not in payload


# --------------------------------------------------------------------- #
# Configuração e integrações
# --------------------------------------------------------------------- #


class TestConfiguracao:
    def test_salvar_o_horario_deixa_linha(self, logado, clinic_a):
        resposta = logado.put(
            "/api/v1/clinic/business-hours/",
            {
                "hours": [
                    {"weekday": 0, "opens_at": "08:00", "closes_at": "12:00"},
                    {"weekday": 1, "opens_at": "08:00", "closes_at": "12:00"},
                ]
            },
            format="json",
        )

        assert resposta.status_code == 200, resposta.data
        linha = AuditLog.objects.get()
        assert linha.resource == "ClinicBusinessHours"
        assert linha.payload["operation"] == "clinic.business_hours"
        assert linha.payload["faixas"] == 2
        assert linha.payload["dias"] == [0, 1]
        assert linha.clinic_id == clinic_a.pk

    def test_sincronizar_com_o_prontuario_deixa_linha(self, logado, clinic_a, monkeypatch):
        from apps.integrations import tasks

        monkeypatch.setattr(tasks.sync_clinic, "delay", lambda *a, **k: None)
        clinic_a.ehr_provider = "vsaude"
        clinic_a.save(update_fields=["ehr_provider"])

        resposta = logado.post("/api/v1/sync/ehr/", {}, format="json")

        assert resposta.status_code == 202
        linha = AuditLog.objects.get()
        assert linha.payload["operation"] == "ehr.sync"
        assert linha.resource == "SyncRun"

    def test_clinica_sem_prontuario_nao_deixa_linha(self, logado, clinic_a):
        """PROVA NEGATIVA: a recusa não é uma sincronização."""
        resposta = logado.post("/api/v1/sync/ehr/", {}, format="json")

        assert resposta.status_code == 400
        assert not AuditLog.objects.exists()


# --------------------------------------------------------------------- #
# Sair do sistema
# --------------------------------------------------------------------- #


class TestLogout:
    URL = "/api/v1/auth/token/blacklist/"

    def test_sair_deixa_linha_mesmo_sem_cabecalho_de_autenticacao(
        self, api_client, manager_single_clinic
    ):
        """
        ⚠️ O `TokenViewBase` do SimpleJWT zera `authentication_classes`: esta
        request é SEMPRE anônima, mesmo com o cabeçalho. Quem identifica o
        evento é o refresh, e é por isso que o LOGOUT nunca aparecia.
        """
        from apps.accounts.passwords import issue_tokens

        tokens = issue_tokens(manager_single_clinic)

        resposta = api_client.post(self.URL, {"refresh": tokens["refresh"]}, format="json")

        assert resposta.status_code == 200
        linha = AuditLog.objects.get()
        assert linha.action == AuditAction.LOGOUT
        assert linha.user_id == manager_single_clinic.pk

    def test_refresh_invalido_nao_inventa_saida(self, api_client):
        """
        PROVA NEGATIVA: sem validar o token, qualquer um poderia forjar um
        "fulano saiu" na auditoria mandando um refresh de mentira.
        """
        resposta = api_client.post(self.URL, {"refresh": "nao.e.um.token"}, format="json")

        assert resposta.status_code == 401
        assert not AuditLog.objects.exists()

    def test_o_mesmo_refresh_duas_vezes_nao_registra_duas_saidas(
        self, api_client, manager_single_clinic
    ):
        from apps.accounts.passwords import issue_tokens

        tokens = issue_tokens(manager_single_clinic)
        api_client.post(self.URL, {"refresh": tokens["refresh"]}, format="json")

        repetido = api_client.post(self.URL, {"refresh": tokens["refresh"]}, format="json")

        assert repetido.status_code == 401
        assert AuditLog.objects.filter(action=AuditAction.LOGOUT).count() == 1


# --------------------------------------------------------------------- #
# Fora da API: terminal e admin
# --------------------------------------------------------------------- #


class TestForaDaAPI:
    def test_criar_clinica_pelo_terminal_deixa_rastro(self, db):
        """
        A docstring do comando promete rastro de quem criou a clínica desde
        que ele nasceu, e até 20/08/2026 esse rastro não existia.
        """
        from django.core.management import call_command

        from apps.tenants.models import Clinic

        call_command("clinica", nome="Clínica Nova", slug="clinica-nova")

        clinica = Clinic.objects.get(slug="clinica-nova")
        linha = AuditLog.objects.get(resource="Clinic")
        assert linha.action == AuditAction.CREATE
        assert linha.payload["operation"] == "clinic.create"
        assert linha.payload["origem"] == "manage.py clinica"
        # Sem usuário: quem roda isto é o operador no terminal do servidor.
        assert linha.user_id is None
        assert linha.clinic_id == clinica.pk

    def test_rodar_de_novo_nao_recria_nem_reescreve(self, db):
        """PROVA NEGATIVA: o comando é idempotente, e a auditoria também."""
        from django.core.management import call_command

        call_command("clinica", nome="Clínica Nova", slug="clinica-nova")
        call_command("clinica", nome="Clínica Nova", slug="clinica-nova")

        assert AuditLog.objects.filter(resource="Clinic").count() == 1

    def test_vinculo_criado_pelo_admin_deixa_linha(self, clinic_a, manager_single_clinic):
        """
        O admin do Django é a porta do operador: dar acesso por ali é o mesmo
        evento que a tela de equipe registra, e sumia inteiro.
        """
        from django.contrib.admin.sites import site
        from django.test import RequestFactory

        from apps.accounts.choices import MembershipRole
        from apps.accounts.models import Membership
        from conftest import make_user

        novo = make_user("entrou.pelo.admin@teste.dev")
        vinculo = Membership(user=novo, clinic=clinic_a, role=MembershipRole.ATTENDANT)

        request = RequestFactory().post("/admin/accounts/membership/add/")
        request.user = manager_single_clinic
        admin = site._registry[Membership]

        class _Form:
            changed_data = ["user", "clinic", "role"]

        admin.save_model(request, vinculo, _Form(), change=False)

        linha = AuditLog.objects.get()
        assert linha.action == AuditAction.CREATE
        assert linha.resource == "Membership"
        assert linha.payload["origem"] == "admin"
        assert linha.clinic_id == clinic_a.pk
