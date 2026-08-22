"""
O plano plataforma (§4.8, RF-ADM-1/3/4/6).

Duas perguntas dominam este arquivo. A primeira é a cerca: o plano plataforma
enxerga TODOS os tenants, então quem não é admin não pode chegar nele de jeito
nenhum, e quem é não pode enxergar conteúdo de clínica por ele.

A segunda é o que a SUSPENSÃO faz de verdade (RF-ADM-1.7). Barrar só a API
deixaria a clínica suspensa disparando sequência e respondendo com o robô, o
que gasta conversa paga na Meta e diz ao paciente que a clínica está
funcionando. Os testes de robô e disparo são o coração daqui.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.models import AuditLog
from apps.tenants.choices import ClinicStatus, SuspensionCategory
from apps.tenants.models import Clinic
from conftest import make_user

pytestmark = pytest.mark.django_db

CLINICS = "/api/v1/platform/clinics/"
OVERVIEW = "/api/v1/platform/overview/"
SYNC = "/api/v1/platform/sync/"
USERS = "/api/v1/platform/users/"
HEALTH = "/api/v1/platform/health/"


@pytest.fixture
def admin_plataforma(db):
    return make_user("dono@medessence.dev", is_platform_admin=True)


@pytest.fixture
def logado(api_client, admin_plataforma):
    api_client.force_authenticate(admin_plataforma)
    return api_client


# --------------------------------------------------------------------- #
# A cerca
# --------------------------------------------------------------------- #


class TestQuemEntra:
    @pytest.mark.parametrize("url", [CLINICS, OVERVIEW, SYNC, USERS, HEALTH])
    def test_gestor_de_clinica_nao_entra_na_plataforma(
        self, api_client, manager_single_clinic, url
    ):
        """
        ⚠️ Gestor é o papel mais alto DA CLÍNICA, e isso não o aproxima da
        plataforma: aqui se vê o tamanho e o estado de todos os tenants.
        """
        api_client.force_authenticate(manager_single_clinic)

        assert api_client.get(url).status_code == 403

    @pytest.mark.parametrize("url", [CLINICS, OVERVIEW, SYNC, USERS, HEALTH])
    def test_deslogado_nao_entra(self, api_client, url):
        assert api_client.get(url).status_code == 401

    def test_admin_da_plataforma_nao_precisa_de_vinculo(self, logado, admin_plataforma):
        """
        O admin não é membro de clínica nenhuma (RF-ADM-6) - é assim no banco
        real. Se estas rotas resolvessem contexto, ele não entraria em lugar
        nenhum, que é exatamente o estado do produto até 21/08/2026.
        """
        assert not Membership.objects.filter(user=admin_plataforma).exists()

        assert logado.get(CLINICS).status_code == 200

    def test_o_header_de_clinica_nao_muda_nada_aqui(self, logado, clinic_a, clinic_b):
        """PROVA NEGATIVA: a plataforma não é escopada, e mandar o header de
        contexto não pode fazê-la devolver uma clínica só."""
        logado.credentials(HTTP_X_CLINIC_ID=str(clinic_a.pk))

        nomes = [c["name"] for c in logado.get(CLINICS).data["results"]]

        assert {clinic_a.name, clinic_b.name} <= set(nomes)


class TestNaoVeConteudo:
    def test_a_listagem_traz_CONTAGEM_de_pacientes_e_nunca_os_nomes(
        self, logado, clinic_a
    ):
        """
        ⚠️ RF-ADM-4: o admin vê que a clínica tem 2 pacientes, não quem são.
        Nome de paciente é conteúdo da clínica, e ele não é membro dela.
        """
        from apps.patients.models import Patient

        Patient.objects.create(clinic=clinic_a, name="Abelardo de Sousa")
        Patient.objects.create(clinic=clinic_a, name="Raimunda Nonata")

        resposta = logado.get(CLINICS)

        linha = next(c for c in resposta.data["results"] if c["id"] == clinic_a.pk)
        assert linha["counts"]["patients"] == 2
        assert "Abelardo" not in str(resposta.data)
        assert "Raimunda" not in str(resposta.data)

    def test_as_credenciais_do_prontuario_nao_saem_nem_entram(self, logado, clinic_a):
        """
        ⚠️ A chave do EHR é cifrada em repouso. Ela não sai na resposta, e um
        PATCH que a mande é IGNORADO em silêncio pelo serializer - o campo não
        existe lá. Se um dia alguém o acrescentar, este teste cai.
        """
        clinic_a.ehr_credentials = {"api_key": "chave-secreta-do-cliente"}
        clinic_a.save(update_fields=["ehr_credentials"])

        detalhe = logado.get(f"{CLINICS}{clinic_a.pk}/")
        patch = logado.patch(
            f"{CLINICS}{clinic_a.pk}/",
            {"ehr_credentials": {"api_key": "invadido"}},
            format="json",
        )

        assert "chave-secreta-do-cliente" not in str(detalhe.data)
        assert patch.status_code == 200
        clinic_a.refresh_from_db()
        assert clinic_a.ehr_credentials == {"api_key": "chave-secreta-do-cliente"}

    def test_apagar_clinica_nao_existe(self, logado, clinic_a):
        """RF-ADM-1.6: suspender é reversível, apagar um tenant não é."""
        assert logado.delete(f"{CLINICS}{clinic_a.pk}/").status_code == 405


# --------------------------------------------------------------------- #
# Criar e configurar
# --------------------------------------------------------------------- #


class TestCriarClinica:
    def test_a_clinica_nasce_com_gestor_e_senha_temporaria(self, logado):
        resposta = logado.post(
            CLINICS,
            {
                "name": "Clínica Aurora",
                "slug": "clinica-aurora",
                "timezone": "America/Sao_Paulo",
                "manager_name": "Marizete Alencar",
                "manager_email": "marizete@aurora.dev",
            },
            format="json",
        )

        assert resposta.status_code == 201, resposta.data
        clinica = Clinic.objects.get(slug="clinica-aurora")
        vinculo = Membership.objects.get(clinic=clinica)
        assert vinculo.role == MembershipRole.MANAGER
        assert vinculo.user.email == "marizete@aurora.dev"
        # A senha viaja uma vez e não é gravada em lugar nenhum.
        assert resposta.data["manager_temporary_password"]
        assert vinculo.user.must_change_password

    def test_clinica_nasce_ATIVA(self, logado):
        logado.post(
            CLINICS,
            {
                "name": "Clínica Aurora",
                "slug": "clinica-aurora",
                "manager_name": "Marizete",
                "manager_email": "marizete@aurora.dev",
            },
            format="json",
        )

        assert Clinic.objects.get(slug="clinica-aurora").status == ClinicStatus.ACTIVE

    def test_gestor_que_JA_tem_conta_nao_ganha_senha_nova(self, logado, attendant_a):
        """
        ⚠️ A credencial dessa pessoa é a que ela usa na OUTRA clínica, e
        trocá-la aqui a derrubaria de lá. A tela precisa dizer isso em vez de
        mostrar um campo vazio.
        """
        resposta = logado.post(
            CLINICS,
            {
                "name": "Clínica Aurora",
                "slug": "clinica-aurora",
                "manager_name": "Ignorado",
                "manager_email": attendant_a.email,
            },
            format="json",
        )

        assert resposta.status_code == 201
        assert resposta.data["manager_temporary_password"] is None

    def test_endereco_repetido_vira_frase_e_nao_500(self, logado, clinic_a):
        resposta = logado.post(
            CLINICS,
            {
                "name": "Outra com o mesmo endereço",
                "slug": clinic_a.slug,
                "manager_name": "Marizete",
                "manager_email": "marizete@aurora.dev",
            },
            format="json",
        )

        assert resposta.status_code == 400
        assert "slug" in resposta.data

    def test_fuso_desconhecido_nao_passa(self, logado):
        """O fuso governa disparo, agenda e horário: um valor inválido
        quebraria os três longe daqui."""
        resposta = logado.post(
            CLINICS,
            {
                "name": "Clínica Aurora",
                "slug": "clinica-aurora",
                "timezone": "Marte/Olympus",
                "manager_name": "Marizete",
                "manager_email": "marizete@aurora.dev",
            },
            format="json",
        )

        assert resposta.status_code == 400
        assert not Clinic.objects.filter(slug="clinica-aurora").exists()

    def test_editar_nao_deixa_trocar_o_slug(self, logado, clinic_a):
        """O slug é ENDEREÇO: muda comando, URL e o que a clínica já anotou."""
        logado.patch(
            f"{CLINICS}{clinic_a.pk}/",
            {"name": "Nome Novo", "slug": "outro-endereco"},
            format="json",
        )

        clinic_a.refresh_from_db()
        assert clinic_a.name == "Nome Novo"
        assert clinic_a.slug == "clinica-alfa"


# --------------------------------------------------------------------- #
# Suspender: o motivo e as três camadas
# --------------------------------------------------------------------- #


class TestSuspender:
    def test_suspender_exige_motivo(self, logado, clinic_a):
        """
        ⚠️ Sem motivo, a pergunta "por que esta clínica está fora" fica sem
        resposta seis meses depois, com o cliente no telefone.
        """
        sem_nada = logado.post(f"{CLINICS}{clinic_a.pk}/suspend/", {}, format="json")
        sem_texto = logado.post(
            f"{CLINICS}{clinic_a.pk}/suspend/",
            {"category": SuspensionCategory.NON_PAYMENT},
            format="json",
        )

        assert sem_nada.status_code == 400
        assert sem_texto.status_code == 400
        clinic_a.refresh_from_db()
        assert clinic_a.status == ClinicStatus.ACTIVE

    def test_categoria_inventada_nao_passa(self, logado, clinic_a):
        resposta = logado.post(
            f"{CLINICS}{clinic_a.pk}/suspend/",
            {"category": "porque_sim", "reason": "algo"},
            format="json",
        )

        assert resposta.status_code == 400

    def test_suspender_e_reativar_deixam_o_rastro(self, logado, clinic_a):
        logado.post(
            f"{CLINICS}{clinic_a.pk}/suspend/",
            {"category": SuspensionCategory.NON_PAYMENT, "reason": "Três faturas em aberto."},
            format="json",
        )
        clinic_a.refresh_from_db()
        assert clinic_a.status == ClinicStatus.SUSPENDED
        assert clinic_a.suspended_at is not None

        logado.post(f"{CLINICS}{clinic_a.pk}/reactivate/", {}, format="json")
        clinic_a.refresh_from_db()

        assert clinic_a.status == ClinicStatus.ACTIVE
        # O campo é o estado de AGORA; o histórico fica na auditoria (§15.4).
        assert clinic_a.suspension_reason == ""
        operacoes = [
            linha.payload.get("operation")
            for linha in AuditLog.objects.filter(resource="Clinic").order_by("id")
        ]
        assert operacoes == ["clinic.suspend", "clinic.reactivate"]
        suspensao = AuditLog.objects.filter(resource="Clinic").order_by("id").first()
        assert suspensao.payload["reason"] == "Três faturas em aberto."

    def test_suspender_duas_vezes_nao_reescreve_a_data(self, logado, clinic_a):
        """PROVA NEGATIVA: a segunda chamada é recusada, senão o `suspended_at`
        andaria para frente e a clínica pareceria suspensa hoje."""
        corpo = {"category": SuspensionCategory.ABUSE, "reason": "Disparo em massa."}
        logado.post(f"{CLINICS}{clinic_a.pk}/suspend/", corpo, format="json")
        clinic_a.refresh_from_db()
        primeira = clinic_a.suspended_at

        segunda = logado.post(f"{CLINICS}{clinic_a.pk}/suspend/", corpo, format="json")

        assert segunda.status_code == 400
        clinic_a.refresh_from_db()
        assert clinic_a.suspended_at == primeira


class TestOQueASuspensaoFaz:
    """RF-ADM-1.7: as três camadas, e o que continua funcionando."""

    @pytest.fixture
    def suspensa(self, clinic_a):
        clinic_a.status = ClinicStatus.SUSPENDED
        clinic_a.suspension_category = SuspensionCategory.NON_PAYMENT
        clinic_a.suspension_reason = "Fatura vencida."
        clinic_a.suspended_at = timezone.now()
        clinic_a.save()
        return clinic_a

    def test_a_equipe_e_barrada_com_codigo_proprio(
        self, api_client, manager_single_clinic, suspensa
    ):
        """
        (a) A recepção não entra. O CÓDIGO é próprio porque a tela é outra: a
        pessoa não fez nada errado, e um 403 genérico a mandaria procurar o
        gestor dela, que também não pode resolver.
        """
        api_client.force_authenticate(manager_single_clinic)

        resposta = api_client.get("/api/v1/patients/")

        assert resposta.status_code == 403
        assert resposta.data["detail"].code == "clinic_suspended"

    def test_a_mensagem_nao_conta_o_motivo_interno(
        self, api_client, manager_single_clinic, suspensa
    ):
        """
        ⚠️ PROVA NEGATIVA: "uso indevido" e "falta de pagamento" são para a
        plataforma responder, não para aparecer na tela de quem trabalha na
        recepção.
        """
        api_client.force_authenticate(manager_single_clinic)

        resposta = api_client.get("/api/v1/patients/")

        assert "Fatura vencida" not in str(resposta.data)
        assert "pagamento" not in str(resposta.data).lower()

    def test_a_pessoa_continua_entrando_e_saindo(
        self, api_client, manager_single_clinic, suspensa
    ):
        """
        ⚠️ O que fecha é a CLÍNICA, não a conta. Quem tem vínculo com outra
        clínica precisa seguir trabalhando nela, e todo mundo precisa poder
        ver o próprio perfil e sair.
        """
        api_client.force_authenticate(manager_single_clinic)

        assert api_client.get("/api/v1/me/").status_code == 200
        assert api_client.get("/api/v1/me/memberships/").status_code == 200

    def test_o_robo_NAO_responde(self, suspensa):
        """
        (c) O coração da suspensão. Um robô respondendo em nome de clínica
        suspensa diz ao paciente que ela está funcionando, e gasta conversa
        paga na Meta enquanto ninguém pode atender.
        """
        from apps.automation.choices import FlowStatus, FlowTrigger
        from apps.automation.tests.conftest import make_flow, make_inbox
        from apps.automation.triggers import handle_inbound
        from apps.inbox.tests.conftest import make_message

        make_flow(
            suspensa,
            status=FlowStatus.ACTIVE,
            trigger=FlowTrigger.FIRST_INBOUND,
            graph={
                "entry_node": "n1",
                "nodes": [
                    {"id": "n1", "type": "start", "label": "n1", "config": {}},
                    {
                        "id": "msg",
                        "type": "send_message",
                        "label": "msg",
                        "config": {"text": "Olá!"},
                    },
                ],
                "edges": [{"from": "n1", "to": "msg", "condition": "default"}],
            },
        )
        inbox = make_inbox(suspensa)
        conversa = inbox["conversation"]

        tratou = handle_inbound(conversa, make_message(conversa))

        assert tratou is False

    def test_as_sequencias_NAO_disparam(self, suspensa, clinic_b):
        """
        (b) E a inscrição fica onde está: cancelá-la destruiria a trilha de
        quem não teve culpa nenhuma na suspensão. Ela volta a disparar quando
        a clínica voltar.
        """
        from datetime import time

        from apps.automation.choices import (
            EnrollmentSource,
            FlowStatus,
            SequenceEnrollmentStatus,
        )
        from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
        from apps.automation.sequences import inscrever
        from apps.automation.tasks import sweep_sequences
        from apps.automation.tests.conftest import make_contact, make_flow

        def trilha(clinic):
            sequence = Sequence.objects.create(
                clinic=clinic, name=f"Trilha {clinic.pk}", is_active=True
            )
            SequenceStep.objects.create(
                sequence=sequence,
                order=1,
                offset_days=0,
                send_time=time(8, 0),
                flow=make_flow(clinic, name=f"F{clinic.pk}", status=FlowStatus.ACTIVE),
            )
            return sequence

        for clinic in (suspensa, clinic_b):
            inscricao = inscrever(
                trilha(clinic),
                make_contact(clinic, wa_id=f"55859000{clinic.pk:05d}"),
                source=EnrollmentSource.PATIENT_RECORD,
            )
            SequenceEnrollment.objects.filter(pk=inscricao.pk).update(
                next_dispatch_at=timezone.now() - timedelta(minutes=5)
            )

        resultado = sweep_sequences()

        # A da clínica no ar saiu; a da suspensa ficou.
        assert resultado["enfileirados"] == 1
        parada = SequenceEnrollment.objects.get(sequence__clinic=suspensa)
        assert parada.status == SequenceEnrollmentStatus.ACTIVE

    def test_a_mensagem_do_paciente_CONTINUA_chegando(self, suspensa):
        """
        (d) ⚠️ A Meta não reentrega. Recusar a ingestão faria a mensagem
        sumir para sempre, e a clínica voltaria do ar sem saber quem tinha
        procurado por ela.
        """
        from apps.automation.tests.conftest import make_channel
        from apps.inbox.models import Message
        from apps.inbox.services import ingest_events
        from apps.integrations.whatsapp.base import WhatsAppEvent, WhatsAppEventKind

        canal = make_channel(suspensa)
        evento = WhatsAppEvent(
            kind=WhatsAppEventKind.INBOUND,
            provider_message_id="wamid.SUSPENSA1",
            wa_id="5585900001111",
            body="Bom dia, consigo remarcar?",
            wa_timestamp=timezone.now(),
            contact_name="Paciente",
        )

        ingest_events(canal, [evento])

        assert Message.objects.filter(
            clinic=suspensa, provider_message_id="wamid.SUSPENSA1"
        ).exists()

    def test_a_plataforma_continua_enxergando_a_suspensa(self, logado, suspensa):
        """Sumir da lista tiraria da tela justamente a clínica que precisa de
        ação (reativar)."""
        resposta = logado.get(CLINICS)

        linha = next(c for c in resposta.data["results"] if c["id"] == suspensa.pk)
        assert linha["status"] == ClinicStatus.SUSPENDED
        assert linha["suspension"]["reason"] == "Fatura vencida."


# --------------------------------------------------------------------- #
# Números e sincronização
# --------------------------------------------------------------------- #


class TestOverview:
    def test_conta_clinicas_por_situacao(self, logado, clinic_a, clinic_b):
        clinic_b.status = ClinicStatus.SUSPENDED
        clinic_b.save(update_fields=["status"])

        dados = logado.get(OVERVIEW).data

        assert dados["clinics"] == 2
        assert (dados["clinics_active"], dados["clinics_suspended"]) == (1, 1)

    def test_movimento_e_dos_ULTIMOS_30_DIAS(self, logado, clinic_a):
        """
        ⚠️ O total desde sempre não distingue quem parou ontem de quem nunca
        começou, que é justamente a pergunta do painel.
        """
        from apps.automation.tests.conftest import make_inbox
        from apps.inbox.models import Conversation

        conversa = make_inbox(clinic_a)["conversation"]
        Conversation.objects.filter(pk=conversa.pk).update(
            last_message_at=timezone.now() - timedelta(days=90)
        )

        assert logado.get(OVERVIEW).data["conversations_30d"] == 0


class TestPainelDeSync:
    def test_quem_falhou_vem_primeiro(self, logado, clinic_a, clinic_b):
        """
        ⚠️ Painel que só lista em ordem alfabética não avisa nada: o erro fica
        na quinta linha e ninguém o vê até a clínica ligar reclamando.
        """
        from apps.integrations.choices import SyncRunKind
        from apps.integrations.models import SyncRun

        for clinic in (clinic_a, clinic_b):
            clinic.ehr_provider = "fake"
            clinic.save(update_fields=["ehr_provider"])

        agora = timezone.now()
        SyncRun.objects.create(
            clinic=clinic_a, kind=SyncRunKind.PATIENTS_FULL, started_at=agora, finished_at=agora
        )
        SyncRun.objects.create(
            clinic=clinic_b,
            kind=SyncRunKind.PATIENTS_FULL,
            started_at=agora,
            finished_at=agora,
            error="Credencial recusada pelo provedor.",
        )

        linhas = logado.get(SYNC).data["clinics"]

        # Beta vem antes de Alfa porque falhou, e não por ordem de nome.
        assert linhas[0]["clinic"]["id"] == clinic_b.pk
        assert linhas[0]["failures"] == 1

    def test_clinica_sem_prontuario_nao_e_alarme_falso(self, logado, clinic_a):
        """PROVA NEGATIVA: quem nunca sincroniza não pode aparecer como parada,
        senão o painel nasce todo vermelho e ninguém olha mais."""
        assert not clinic_a.ehr_provider

        linha = logado.get(SYNC).data["clinics"][0]

        assert linha["stalled"] is False

    def test_clinica_suspensa_nao_e_alarme_de_sync(self, logado, clinic_a):
        """Ela não sincroniza porque foi desligada de propósito."""
        clinic_a.ehr_provider = "fake"
        clinic_a.status = ClinicStatus.SUSPENDED
        clinic_a.save(update_fields=["ehr_provider", "status"])

        linha = logado.get(SYNC).data["clinics"][0]

        assert linha["stalled"] is False


# --------------------------------------------------------------------- #
# A plataforma de verdade (RF-ADM-4.1/4.2/4.3, 1.8, 7 e 8)
# --------------------------------------------------------------------- #


class TestVisaoGeralComTempo:
    def test_a_serie_cobre_os_30_dias_com_zeros(self, logado):
        """RF-ADM-4.1: dia sem mensagem entra como zero, senão o gráfico
        esconde justamente o buraco."""
        resposta = logado.get(OVERVIEW)

        serie = resposta.data["messages_by_day"]
        assert len(serie) == 31  # 30 dias atrás até hoje, inclusive
        assert all(item["count"] == 0 for item in serie)

    def test_canal_caido_vira_problema_de_primeira_classe(self, logado, clinic_a):
        """RF-ADM-4.2: a clínica real ficou dias com o WhatsApp fora sem a
        Visão geral gritar. Nunca mais."""
        _canal(
            clinic_a,
            disconnected_at=timezone.now() - timedelta(days=2),
            disconnect_reason="Session has expired.",
        )

        atencao = logado.get(OVERVIEW).data["attention"]

        caidos = [a for a in atencao if a["kind"] == "channel_down"]
        assert len(caidos) == 1
        assert caidos[0]["clinic"]["id"] == clinic_a.pk
        assert caidos[0]["detail"] == "Session has expired."
        assert caidos[0]["since"] is not None

    def test_sem_problema_nenhum_o_bloco_vem_vazio(self, logado, clinic_a):
        assert logado.get(OVERVIEW).data["attention"] == []

    def test_a_lista_diz_a_ultima_mensagem(self, logado, clinic_a):
        """RF-ADM-4.3: o "está viva?" que contagem de 30 dias não responde."""
        resposta = logado.get(CLINICS)

        linha = next(c for c in resposta.data["results"] if c["id"] == clinic_a.pk)
        assert "last_message_at" in linha


class TestDetalheInteiro:
    def test_o_detalhe_traz_os_cartoes(self, logado, clinic_a, attendant_a):
        """RF-ADM-1.8: canal, automação, equipe e histórico juntos.

        Sincronização SAIU do detalhe (22/08): a tela própria já responde por
        tipo e por clínica, e o retrieve pagava quatro consultas para repetir.
        """
        resposta = logado.get(f"{CLINICS}{clinic_a.pk}/")

        assert resposta.status_code == 200
        for campo in (
            "channel_details",
            "automation",
            "team",
            "suspension_history",
        ):
            assert campo in resposta.data, campo
        assert "sync_runs" not in resposta.data
        equipe = resposta.data["team"]
        assert len(equipe) == 1
        assert equipe[0]["role"] == MembershipRole.ATTENDANT
        assert "last_login" in equipe[0]
        # Cerca: papel e nome de EQUIPE são cadastro; paciente continua número.
        assert "patients" in resposta.data["counts"]

    def test_a_lista_continua_leve_sem_os_cartoes(self, logado, clinic_a):
        """As consultas por objeto são do RETRIEVE; a lista não as paga."""
        resposta = logado.get(CLINICS)

        assert "channel_details" not in resposta.data["results"][0]
        assert "team" not in resposta.data["results"][0]

    def test_o_historico_de_suspensao_sai_da_auditoria(self, logado, clinic_a):
        logado.post(
            f"{CLINICS}{clinic_a.pk}/suspend/",
            {"category": SuspensionCategory.NON_PAYMENT, "reason": "teste"},
            format="json",
        )
        logado.post(f"{CLINICS}{clinic_a.pk}/reactivate/", {}, format="json")

        historico = logado.get(f"{CLINICS}{clinic_a.pk}/").data[
            "suspension_history"
        ]

        assert [h["operation"] for h in historico] == [
            "clinic.reactivate",
            "clinic.suspend",
        ]
        assert historico[1]["reason"] == "teste"


class TestFiltrosServerSide:
    """⚠️ Filtro é do SERVIDOR (regra de 21/08): o front não peneira lista."""

    def test_clinicas_filtram_por_busca_e_situacao(self, logado, clinic_a, clinic_b):
        clinic_b.status = ClinicStatus.SUSPENDED
        clinic_b.save(update_fields=["status"])

        nomes = [
            c["name"]
            for c in logado.get(CLINICS, {"search": "Alfa"}).data["results"]
        ]
        assert nomes == [clinic_a.name]

        suspensas = [
            c["name"]
            for c in logado.get(CLINICS, {"status": "suspended"}).data["results"]
        ]
        assert suspensas == [clinic_b.name]

        # Busca que não acha nada devolve vazio, não erro.
        assert logado.get(CLINICS, {"search": "nao-existe"}).data["results"] == []

    def test_o_detalhe_nao_some_por_causa_do_filtro(self, logado, clinic_a):
        """`get_object` ignora a busca da lista de propósito."""
        resposta = logado.get(
            f"{CLINICS}{clinic_a.pk}/", {"search": "nada-a-ver"}
        )
        assert resposta.status_code == 200
        assert resposta.data["id"] == clinic_a.pk

    def test_pessoas_filtram_por_busca_clinica_e_tipo(
        self, logado, admin_plataforma, manager_two_clinics, attendant_a, clinic_a
    ):
        por_busca = logado.get(USERS, {"search": "atendente@"}).data["users"]
        assert [u["email"] for u in por_busca] == ["atendente@teste.dev"]

        por_clinica = {
            u["email"]
            for u in logado.get(USERS, {"clinic": str(clinic_a.pk)}).data["users"]
        }
        assert por_clinica == {"gestor@teste.dev", "atendente@teste.dev"}

        por_papel = {
            u["email"] for u in logado.get(USERS, {"role": "manager"}).data["users"]
        }
        assert por_papel == {"gestor@teste.dev"}

        admins = {
            u["email"]
            for u in logado.get(USERS, {"role": "platform_admin"}).data["users"]
        }
        assert admins == {"dono@medessence.dev"}


class TestPessoas:
    def test_lista_todo_mundo_com_vinculos_e_ultimo_acesso(
        self, logado, admin_plataforma, manager_two_clinics
    ):
        """RF-ADM-7: quem tem acesso a quê, sem abrir clínica por clínica."""
        resposta = logado.get(USERS)

        assert resposta.status_code == 200
        pessoas = {u["email"]: u for u in resposta.data["users"]}
        gestor = pessoas["gestor@teste.dev"]
        assert len(gestor["memberships"]) == 2
        assert gestor["is_platform_admin"] is False
        assert "last_login" in gestor
        assert pessoas["dono@medessence.dev"]["is_platform_admin"] is True

    def test_e_leitura_nesta_fase(self, logado):
        """As ações de conta são do eixo poder, e a rota nem aceita POST."""
        assert logado.post(USERS, {}, format="json").status_code == 405


class TestSaudeDoSistema:
    def test_o_painel_reune_banco_filas_worker_e_presas(self, logado):
        """RF-ADM-8: o inbox_doctor com porta na tela."""
        resposta = logado.get(HEALTH)

        assert resposta.status_code == 200
        assert resposta.data["database"]["alive"] is True
        assert "queues" in resposta.data
        assert "worker" in resposta.data
        assert resposta.data["stuck_messages"]["total"] == 0
        assert resposta.data["pending_migrations"] == 0

    def test_a_presa_identifica_a_conversa_e_nunca_o_conteudo(
        self, logado, clinic_a
    ):
        from apps.inbox.choices import MessageStatus, SenderKind
        from apps.inbox.models import Message

        conversa = _conversa(clinic_a)
        Message.objects.create(
            clinic=clinic_a,
            conversation=conversa,
            sender_kind=SenderKind.AGENT,
            status=MessageStatus.FAILED,
            body="conteúdo que NÃO pode atravessar",
            wa_timestamp=timezone.now(),
        )

        presas = logado.get(HEALTH).data["stuck_messages"]

        assert presas["total"] == 1
        item = presas["items"][0]
        assert item["conversation_id"] == conversa.pk
        assert "conteúdo" not in str(item)


# Andaime local: o `inbox_a` mora no conftest do app inbox e não chega aqui.
def _canal(clinic, **campos):
    from apps.inbox.choices import WhatsAppProviderKind
    from apps.inbox.models import Channel

    return Channel.objects.create(
        clinic=clinic,
        provider=WhatsAppProviderKind.FAKE,
        display_number="5585999990000",
        is_test=False,
        **campos,
    )


def _conversa(clinic):
    from apps.inbox.models import Conversation
    from apps.patients.models import Contact

    canal = _canal(clinic)
    contato = Contact.objects.create(
        clinic=clinic, wa_id="5585900000009", display_name="Fulano"
    )
    return Conversation.objects.create(clinic=clinic, channel=canal, contact=contato)
