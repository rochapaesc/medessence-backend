"""
Arquivos do paciente (RF-PRO-7): proxy ao vivo, gate P10 e auditoria.

O teste que mais importa aqui é o da URL: ela é conteúdo clínico e não pode
sair na listagem, só pela ação de abrir — que é a que audita.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.integrations.ehr.base import EHRFile, EHRFolderListing
from apps.patients.models import Patient

URL_BLOB = "https://storage.invalido/prontuario/exame.pdf"


@pytest.fixture
def paciente(clinic_a):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    return Patient.objects.create(
        clinic=clinic_a, name="Willian Costa", external_id="guid-paciente"
    )


class _ProviderFalso:
    """Adapter de mentira, no formato do port — a vSaúde real não entra aqui."""

    def __init__(self):
        self.enviados = []
        self.renomeados = []
        self.apagados = []

    def list_files(self, patient_external_id, folder_external_id=""):
        if folder_external_id == "pasta-exames":
            return EHRFolderListing(
                folder_external_id="pasta-exames",
                folder_name="Exames",
                # A pasta que o próprio prontuário administra.
                folder_is_system=True,
                files=[
                    EHRFile(
                        external_id="arq-1",
                        name="Pedido 02/09/2025.pdf",
                        size=243251,
                        mime_type="application/pdf",
                        url=URL_BLOB,
                        read_only=True,
                        can_delete=False,
                    )
                ],
            )
        if folder_external_id == "pasta-atestado":
            return EHRFolderListing(
                folder_external_id="pasta-atestado",
                folder_name="Atestado Médico",
                files=[
                    EHRFile(
                        external_id="arq-2",
                        name="Guia.pdf",
                        size=1024,
                        mime_type="application/pdf",
                        url=URL_BLOB,
                        # Como o prontuário devolve o que NÓS subimos: travado
                        # no papel, mas o Rename funciona (calibrado ao vivo).
                        read_only=True,
                        can_delete=True,
                    )
                ],
            )
        return EHRFolderListing(
            folders=[
                EHRFile(
                    external_id="pasta-exames",
                    name="Exames",
                    is_directory=True,
                    system=True,
                    read_only=True,
                    can_delete=False,
                ),
                EHRFile(
                    external_id="pasta-atestado",
                    name="Atestado Médico",
                    is_directory=True,
                ),
            ]
        )

    def upload_file(self, patient_external_id, folder_external_id, filename, content, content_type):
        self.enviados.append((folder_external_id, filename, len(content), content_type))

    def rename_file(self, file_external_id, name):
        self.renomeados.append((file_external_id, name))
        return f"{name}.pdf"

    def delete_file(self, file_external_id):
        self.apagados.append(file_external_id)


@pytest.fixture
def provider(monkeypatch):
    falso = _ProviderFalso()
    monkeypatch.setattr(
        "apps.integrations.ehr.registry.get_ehr_provider", lambda clinic: falso
    )
    monkeypatch.setattr(
        "apps.patients.api.viewsets.patient_files.get_ehr_provider",
        lambda clinic: falso,
    )
    return falso


def _url(patient, sufixo=""):
    return f"/api/v1/patients/{patient.pk}/files/{sufixo}"


# ------------------------- listagem -------------------------


def test_lista_a_raiz_com_pastas(api_client, manager_single_clinic, paciente, provider):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(_url(paciente))

    assert resposta.status_code == 200
    assert resposta.data["available"] is True
    nomes = [p["name"] for p in resposta.data["folders"]]
    assert nomes == ["Exames", "Atestado Médico"]
    # A pasta do EHR vem marcada: é o selo e o motivo de não deixar enviar.
    exames = resposta.data["folders"][0]
    assert exames["from_ehr"] is True
    assert exames["read_only"] is True
    assert exames["can_delete"] is False


def test_a_URL_do_arquivo_NAO_sai_na_listagem(
    api_client, manager_single_clinic, paciente, provider
):
    """O invariante da fatia: listagem com URL seria auditoria com porta dos
    fundos, porque quem lista não fica registrado."""
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(_url(paciente), {"folder": "pasta-exames"})

    assert resposta.status_code == 200
    assert resposta.data["files"][0]["name"] == "Pedido 02/09/2025.pdf"
    assert "url" not in resposta.data["files"][0]
    assert URL_BLOB not in str(resposta.data)


def test_clinica_sem_ehr_explica_em_vez_de_quebrar(
    api_client, manager_single_clinic, clinic_a, provider
):
    clinic_a.ehr_provider = ""
    clinic_a.save(update_fields=["ehr_provider"])
    sem_ehr = Patient.objects.create(clinic=clinic_a, name="Sem EHR")
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(_url(sem_ehr))

    assert resposta.status_code == 200
    assert resposta.data["available"] is False
    assert "prontuário conectado" in resposta.data["detail"]


def test_paciente_sem_external_id_explica(
    api_client, manager_single_clinic, clinic_a, paciente, provider
):
    # `paciente` entra só para a clínica ficar com EHR configurado.
    novo = Patient.objects.create(clinic=clinic_a, name="Recém-cadastrado")
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(_url(novo))

    assert resposta.data["available"] is False
    assert "sincronizado" in resposta.data["detail"]


# ------------------------- abrir (auditado) -------------------------


def test_abrir_devolve_a_URL_e_grava_na_auditoria(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        _url(paciente, "open/"),
        {"file": "arq-1", "folder": "pasta-exames"},
        format="json",
    )

    assert resposta.status_code == 200
    assert resposta.data["url"] == URL_BLOB

    log = AuditLog.objects.filter(resource="PatientFile", action=AuditAction.READ).first()
    assert log is not None
    assert log.payload["file_name"] == "Pedido 02/09/2025.pdf"
    assert log.payload["patient"] == paciente.pk
    # O log responde "quem abriu o quê" — não guarda o documento nem o endereço.
    assert URL_BLOB not in str(log.payload)


def test_abrir_arquivo_de_outra_pasta_recusa(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        _url(paciente, "open/"), {"file": "arq-inexistente"}, format="json"
    )
    assert resposta.status_code == 400


# ------------------------- gate P10 -------------------------


def test_atendente_LISTA_mas_nao_abre(api_client, attendant_a, paciente, provider):
    """A lista é metadado (P10); o arquivo aberto é conteúdo."""
    api_client.force_authenticate(attendant_a)

    listagem = api_client.get(_url(paciente))
    assert listagem.status_code == 200

    abrir = api_client.post(
        _url(paciente, "open/"),
        {"file": "arq-1", "folder": "pasta-exames"},
        format="json",
    )
    assert abrir.status_code == 403
    assert URL_BLOB not in str(abrir.data)


def test_atendente_ENVIA(api_client, attendant_a, paciente, provider):
    """É a recepção que digitaliza o que o paciente traz."""
    api_client.force_authenticate(attendant_a)
    arquivo = SimpleUploadedFile("guia.pdf", b"%PDF-1.4 conteudo", content_type="application/pdf")

    resposta = api_client.post(
        _url(paciente, "upload/"),
        {"file": arquivo, "folder": "pasta-atestado"},
        format="multipart",
    )

    assert resposta.status_code == 200
    assert provider.enviados == [("pasta-atestado", "guia.pdf", 17, "application/pdf")]


def test_atendente_nao_renomeia_nem_exclui(api_client, attendant_a, paciente, provider):
    api_client.force_authenticate(attendant_a)
    renomear = api_client.post(
        _url(paciente, "rename/"),
        {"file": "arq-2", "name": "Novo", "folder": "pasta-atestado"},
        format="json",
    )
    excluir = api_client.post(
        _url(paciente, "delete/"),
        {"file": "arq-2", "folder": "pasta-atestado"},
        format="json",
    )
    assert renomear.status_code == 403
    assert excluir.status_code == 403
    assert provider.renomeados == []
    assert provider.apagados == []


# ------------------------- enviar -------------------------


def test_recusa_formato_que_o_prontuario_nao_aceita(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)
    arquivo = SimpleUploadedFile("planilha.xlsx", b"PK\x03\x04", content_type="application/vnd.ms-excel")

    resposta = api_client.post(
        _url(paciente, "upload/"), {"file": arquivo}, format="multipart"
    )

    assert resposta.status_code == 400
    assert "PDF ou imagem" in str(resposta.data)
    assert provider.enviados == [], "não pode nem viajar até o EHR"


def test_upload_devolve_a_lista_relida(
    api_client, manager_single_clinic, paciente, provider
):
    """A vSaúde responde VAZIO no upload: a lista é a única prova de que
    o arquivo entrou."""
    api_client.force_authenticate(manager_single_clinic)
    arquivo = SimpleUploadedFile("foto.jpg", b"\xff\xd8\xff", content_type="image/jpeg")

    resposta = api_client.post(
        _url(paciente, "upload/"), {"file": arquivo}, format="multipart"
    )

    assert resposta.status_code == 200
    assert resposta.data["available"] is True
    assert "folders" in resposta.data


# ------------------------- renomear e excluir -------------------------


def test_renomear_devolve_o_nome_final_do_servidor(
    api_client, manager_single_clinic, paciente, provider
):
    """O EHR preserva a extensão: mandar "Teste" devolve "Teste.pdf"."""
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post(
        _url(paciente, "rename/"),
        {"file": "arq-2", "name": "Teste", "folder": "pasta-atestado"},
        format="json",
    )

    assert resposta.status_code == 200
    assert resposta.data["name"] == "Teste.pdf"
    assert provider.renomeados == [("arq-2", "Teste")]


def test_excluir_chama_o_ehr_e_devolve_a_lista(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post(
        _url(paciente, "delete/"),
        {"file": "arq-2", "folder": "pasta-atestado"},
        format="json",
    )

    assert resposta.status_code == 200
    assert provider.apagados == ["arq-2"]
    # Volta a pasta ABERTA, não a raiz: a tela continua onde o usuário estava.
    assert resposta.data["folder"]["name"] == "Atestado Médico"
    assert AuditLog.objects.filter(
        resource="PatientFile", action=AuditAction.DELETE
    ).exists()


# ------------------------- escopo -------------------------


def test_paciente_de_outra_clinica_nao_abre(
    api_client, manager_single_clinic, clinic_b, provider
):
    alheio = Patient.objects.create(
        clinic=clinic_b, name="De Outra", external_id="guid-alheio"
    )
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(_url(alheio)).status_code == 404


# ------------------- a pasta do prontuário é só leitura -------------------
#
# Calibrado ao vivo em 30/07/2026: a vSaúde ACEITA gravar dentro das pastas
# dela própria. O botão escondido na tela não era regra nenhuma - quem faz o
# "somente leitura" do RF-PRO-7 valer somos nós.


def test_enviar_para_a_pasta_do_prontuario_e_recusado(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)
    arquivo = SimpleUploadedFile("intruso.pdf", b"%PDF-1.4", content_type="application/pdf")

    resposta = api_client.post(
        _url(paciente, "upload/"),
        {"file": arquivo, "folder": "pasta-exames"},
        format="multipart",
    )

    assert resposta.status_code == 400
    assert "preenchida pelo prontuário" in str(resposta.data)
    assert provider.enviados == [], "não pode nem viajar até o EHR"


def test_renomear_e_excluir_dentro_da_pasta_do_prontuario_sao_recusados(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)

    renomear = api_client.post(
        _url(paciente, "rename/"),
        {"file": "arq-1", "name": "Outro", "folder": "pasta-exames"},
        format="json",
    )
    excluir = api_client.post(
        _url(paciente, "delete/"),
        {"file": "arq-1", "folder": "pasta-exames"},
        format="json",
    )

    assert renomear.status_code == 400
    assert excluir.status_code == 400
    assert provider.renomeados == []
    assert provider.apagados == []


def test_omitir_a_pasta_NAO_e_atalho_para_escapar_do_gate(
    api_client, manager_single_clinic, paciente, provider
):
    """Se `folder` fosse opcional, bastaria não mandá-lo para alterar um
    arquivo que mora na pasta do prontuário."""
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post(
        _url(paciente, "rename/"), {"file": "arq-1", "name": "Outro"}, format="json"
    )

    assert resposta.status_code == 400
    assert provider.renomeados == []


def test_arquivo_travado_pelo_EHR_nao_e_excluido_mesmo_em_pasta_comum(
    api_client, manager_single_clinic, paciente, provider
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(
        _url(paciente, "delete/"),
        {"file": "arq-1", "folder": "pasta-atestado"},
        format="json",
    )
    # `arq-1` não mora nesta pasta - o gate confere isso antes de chamar o EHR.
    assert resposta.status_code == 400
    assert provider.apagados == []


def test_renomear_arquivo_read_only_em_pasta_comum_FUNCIONA(
    api_client, manager_single_clinic, paciente, provider
):
    """O prontuário devolve `isReadOnly: true` em TODO arquivo que sobe por
    nós, e mesmo assim o Rename dele funciona (verificado ao vivo). Tratar a
    flag como trava mataria o Renomear na prática."""
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post(
        _url(paciente, "rename/"),
        {"file": "arq-2", "name": "Guia nova", "folder": "pasta-atestado"},
        format="json",
    )

    assert resposta.status_code == 200
    assert provider.renomeados == [("arq-2", "Guia nova")]


def test_upload_devolve_a_pasta_ABERTA_e_nao_a_raiz(
    api_client, manager_single_clinic, paciente, provider
):
    """A pasta vem no CORPO do multipart; lendo da querystring a resposta
    voltava com a raiz e a tela trocava de conteúdo sozinha."""
    api_client.force_authenticate(manager_single_clinic)
    arquivo = SimpleUploadedFile("guia.pdf", b"%PDF-1.4", content_type="application/pdf")

    resposta = api_client.post(
        _url(paciente, "upload/"),
        {"file": arquivo, "folder": "pasta-atestado"},
        format="multipart",
    )

    assert resposta.status_code == 200
    assert resposta.data["folder"]["name"] == "Atestado Médico"


def test_teto_de_tamanho_recusa_antes_de_viajar(
    api_client, manager_single_clinic, paciente, provider
):
    """10 MB é o teto: acima disso a subida engasga no meio (a vSaúde não
    recusa por tamanho, ela para de responder)."""
    api_client.force_authenticate(manager_single_clinic)
    grande = SimpleUploadedFile(
        "exame.pdf", b"x" * (11 * 1024 * 1024), content_type="application/pdf"
    )

    resposta = api_client.post(
        _url(paciente, "upload/"), {"file": grande}, format="multipart"
    )

    assert resposta.status_code == 400
    assert "10 MB" in str(resposta.data)
    assert provider.enviados == []
