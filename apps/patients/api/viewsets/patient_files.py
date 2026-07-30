"""
Arquivos do paciente (RF-PRO-7) — proxy AO VIVO do prontuário.

**Sem espelho local**, ao contrário do resto do write-through (§10.1): a
listagem sai do EHR na hora, como a busca de horários livres. Espelhar os
arquivos de 5 mil pacientes copiaria o storage do cliente para o nosso banco
sem ganho — arquivo se consulta um paciente por vez, com a ficha aberta.

⚠️ **A URL do arquivo não sai na listagem.** O `ListFolder` da vSaúde devolve
o endereço do blob em cada item; ele só é entregue pela action `open`, que é a
que grava a auditoria. Listagem com URL seria auditoria com porta dos fundos.
"""

from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.api.serializers import viewer_is_attendant
from apps.core.audit import log_action
from apps.core.models.audit_log import AuditAction
from apps.integrations.ehr.exceptions import EHRError
from apps.integrations.ehr.registry import get_ehr_provider

#: Só o que a vSaúde aceita — recusar aqui evita a viagem e o erro genérico.
TIPOS_ACEITOS = ("application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic")

#: Teto de tamanho. Calibrado ao vivo em 30/07/2026: a vSaúde não recusa por
#: tamanho, ela ENGASGA — a subida corre a ~0,5 MB/s e 30 MB morrem no meio da
#: escrita. 10 MB sobe em ~26s, que já é o limite do que se espera de uma tela,
#: e cobre com folga o exame digitalizado (os reais da clínica têm 0,1 a 1,5 MB).
TAMANHO_MAXIMO = 10 * 1024 * 1024


def _arquivo_payload(arquivo, *, com_url: bool = False) -> dict:
    """
    Um item para a tela. `url` fica de FORA por padrão: ela é conteúdo
    clínico e só sai pela action `open`, que audita.
    """
    dados = {
        "id": arquivo.external_id,
        "name": arquivo.name,
        "is_directory": arquivo.is_directory,
        "size": arquivo.size,
        "mime_type": arquivo.mime_type,
        "created_at": arquivo.created_at,
        "read_only": arquivo.read_only,
        "can_delete": arquivo.can_delete,
        # A tela usa para o selo "do prontuário" e para desabilitar o envio.
        "from_ehr": arquivo.system,
    }
    if com_url:
        dados["url"] = arquivo.url
    return dados


class PatientFilesMixin:
    """As actions de arquivo do `PatientViewSet`."""

    def _paciente_com_ehr(self, pk):
        """
        O paciente e o provedor, ou o motivo pelo qual a aba não funciona.
        Devolve `(patient, provider, resposta_de_erro)`.
        """
        patient = self.get_object()
        if not self.clinic.ehr_provider:
            return patient, None, Response(
                {
                    "detail": "Arquivos ficam disponíveis com o prontuário conectado.",
                    "available": False,
                }
            )
        if not patient.external_id:
            return patient, None, Response(
                {
                    "detail": "Este paciente ainda não foi sincronizado com o prontuário.",
                    "available": False,
                }
            )
        return patient, get_ehr_provider(self.clinic), None

    def _pasta_gravavel(self, provider, patient, folder_id):
        """
        A pasta onde a escrita vai cair, recusando as do prontuário.

        A vSaúde ACEITA gravar dentro das pastas dela (verificado ao vivo em
        30/07/2026): quem faz valer o "somente leitura" do RF-PRO-7 somos nós.
        Devolve a listagem, que o chamador reaproveita para conferir o item.
        """
        try:
            alvo = provider.list_files(patient.external_id, folder_id)
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        if alvo.folder_is_system:
            raise ValidationError(
                {
                    "folder": f"A pasta {alvo.folder_name} é preenchida pelo "
                    "prontuário e não aceita alteração por aqui."
                }
            )
        return alvo

    def _so_quem_ve_conteudo(self):
        """
        Abrir, renomear e excluir pressupõem saber o que o arquivo É — é
        conteúdo clínico (P10, RF-PRO-7). Listar e enviar ficam liberados: a
        lista é metadado e quem digitaliza o que o paciente traz é a recepção.
        """
        if viewer_is_attendant(self.get_serializer_context()):
            raise PermissionDenied(
                "Só médico e gestor podem abrir, renomear ou excluir arquivos "
                "do prontuário."
            )

    @action(detail=True, methods=["get"], url_path="files")
    def files(self, request, pk=None, folder=None):
        """
        Conteúdo de uma pasta (`?folder=<id>`). Sem `folder` = raiz.

        `folder` explícito existe para o envio e a exclusão reaproveitarem
        esta resposta: eles mandam a pasta no CORPO, e ler da querystring
        devolvia a raiz enquanto a tela continuava dentro da pasta.
        """
        patient, provider, erro = self._paciente_com_ehr(pk)
        if erro is not None:
            return erro
        if folder is None:
            folder = request.query_params.get("folder", "")
        try:
            listagem = provider.list_files(patient.external_id, folder)
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        return Response(
            {
                "available": True,
                "folder": {
                    "id": listagem.folder_external_id,
                    "name": listagem.folder_name,
                },
                "folders": [_arquivo_payload(p) for p in listagem.folders],
                "files": [_arquivo_payload(a) for a in listagem.files],
            }
        )

    @action(detail=True, methods=["post"], url_path="files/open")
    def open_file(self, request, pk=None):
        """
        Devolve o endereço do arquivo e REGISTRA a leitura.
        É o único caminho pelo qual a URL sai daqui.
        """
        self._so_quem_ve_conteudo()
        patient, provider, erro = self._paciente_com_ehr(pk)
        if erro is not None:
            return erro

        file_id = request.data.get("file")
        folder_id = request.data.get("folder", "")
        if not file_id:
            raise ValidationError({"file": "Informe o arquivo."})

        try:
            listagem = provider.list_files(patient.external_id, folder_id)
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        alvo = next((a for a in listagem.files if a.external_id == file_id), None)
        if alvo is None or not alvo.url:
            raise ValidationError({"file": "Arquivo não encontrado nesta pasta."})

        membership = getattr(request, "active_membership", None)
        log_action(
            user=request.user,
            action=AuditAction.READ,
            resource="PatientFile",
            resource_id=file_id,
            # Nome e paciente, nunca o conteúdo nem a URL — o log responde
            # "quem abriu o quê", não guarda o documento.
            payload={
                "patient": patient.pk,
                "file_name": alvo.name,
                "role": getattr(membership, "role", "") or "",
            },
            request=request,
            clinic=self.clinic,
        )
        return Response(_arquivo_payload(alvo, com_url=True))

    @action(
        detail=True,
        methods=["post"],
        url_path="files/upload",
        # O parser padrão do projeto é JSON; sem isto o multipart volta 415.
        # Mesmo padrão do upload de mídia do Inbox.
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_file(self, request, pk=None):
        """Envia um arquivo para a pasta aberta (todos os papéis)."""
        patient, provider, erro = self._paciente_com_ehr(pk)
        if erro is not None:
            return erro

        arquivo = request.FILES.get("file")
        if arquivo is None:
            raise ValidationError({"file": "Envie um arquivo."})
        if arquivo.content_type not in TIPOS_ACEITOS:
            raise ValidationError(
                {"file": "O prontuário aceita apenas PDF ou imagem."}
            )
        if arquivo.size > TAMANHO_MAXIMO:
            raise ValidationError(
                {"file": f"Arquivo acima de {TAMANHO_MAXIMO // (1024 * 1024)} MB."}
            )

        destino = request.data.get("folder", "")
        if destino:
            self._pasta_gravavel(provider, patient, destino)

        try:
            provider.upload_file(
                patient.external_id,
                request.data.get("folder", ""),
                arquivo.name,
                arquivo.read(),
                arquivo.content_type,
            )
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        log_action(
            user=request.user,
            action=AuditAction.CREATE,
            resource="PatientFile",
            resource_id=arquivo.name,
            payload={"patient": patient.pk, "file_name": arquivo.name},
            request=request,
            clinic=self.clinic,
        )
        # A vSaúde responde VAZIO no upload: a lista volta relida, que é a
        # única prova de que o arquivo entrou.
        return self.files(request, pk=pk, folder=destino)

    @action(detail=True, methods=["post"], url_path="files/rename")
    def rename_file(self, request, pk=None):
        self._so_quem_ve_conteudo()
        patient, provider, erro = self._paciente_com_ehr(pk)
        if erro is not None:
            return erro

        file_id = request.data.get("file")
        nome = (request.data.get("name") or "").strip()
        if not file_id or not nome:
            raise ValidationError({"detail": "Informe o arquivo e o novo nome."})

        # `folder` é obrigatório de propósito: é ele que diz se o arquivo mora
        # numa pasta do prontuário. Sem exigir, bastaria omitir para escapar.
        alvo = self._pasta_gravavel(provider, patient, request.data.get("folder", ""))
        if not any(a.external_id == file_id for a in alvo.files):
            raise ValidationError({"file": "Arquivo não encontrado nesta pasta."})

        try:
            final = provider.rename_file(file_id, nome)
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        log_action(
            user=request.user,
            action=AuditAction.UPDATE,
            resource="PatientFile",
            resource_id=file_id,
            payload={"patient": patient.pk, "file_name": final},
            request=request,
            clinic=self.clinic,
        )
        return Response({"name": final})

    @action(detail=True, methods=["post"], url_path="files/delete")
    def delete_file(self, request, pk=None):
        self._so_quem_ve_conteudo()
        patient, provider, erro = self._paciente_com_ehr(pk)
        if erro is not None:
            return erro

        file_id = request.data.get("file")
        if not file_id:
            raise ValidationError({"file": "Informe o arquivo."})

        alvo = self._pasta_gravavel(provider, patient, request.data.get("folder", ""))
        item = next((a for a in alvo.files if a.external_id == file_id), None)
        if item is None:
            raise ValidationError({"file": "Arquivo não encontrado nesta pasta."})
        if not item.can_delete:
            raise ValidationError({"file": "O prontuário não deixa excluir este arquivo."})

        try:
            provider.delete_file(file_id)
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        log_action(
            user=request.user,
            action=AuditAction.DELETE,
            resource="PatientFile",
            resource_id=file_id,
            payload={"patient": patient.pk, "file_name": item.name},
            request=request,
            clinic=self.clinic,
        )
        return self.files(request, pk=pk, folder=request.data.get("folder", ""))
