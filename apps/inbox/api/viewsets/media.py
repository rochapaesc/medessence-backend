from pathlib import Path

from django.core.files.base import ContentFile
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.status import HTTP_201_CREATED

from apps.core.api.viewsets import ClinicScopedMixin
from apps.core.api.viewsets.base import BaseGenericViewSet
from apps.core.audit import log_action
from apps.inbox import media_rules
from apps.inbox.api.serializers.message import media_payload
from apps.inbox.choices import MediaState, MessageKind
from apps.inbox.models import MediaAsset


class MediaUploadViewSet(ClinicScopedMixin, BaseGenericViewSet):
    """
    Sobe o anexo ANTES de enviar (`POST /inbox/media/`, multipart).

    Em dois passos, e não num create de mensagem multipart, pelo mesmo motivo
    do wacrm: a prévia com legenda precisa do arquivo já no servidor, a barra
    de progresso precisa de uma requisição só dela, e um envio que falha não
    pode custar o upload de novo — o anexo continua lá, é só reenviar.
    """

    model = MediaAsset
    parser_classes = [MultiPartParser]

    def create(self, request, *args, **kwargs):
        arquivo = request.FILES.get("file")
        if arquivo is None:
            raise ValidationError("Envie o arquivo no campo `file`.")

        mime = (arquivo.content_type or "").split(";", 1)[0].strip().lower()
        # O tipo vem do arquivo, não do cliente: quem escolhe "foto" e manda um
        # PDF tem de virar documento, não uma imagem que a Meta recusa.
        kind = media_rules.tipo_do_arquivo(mime)
        conteudo = arquivo.read()

        try:
            media_rules.validar(kind, mime, len(conteudo))
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

        nome = Path(arquivo.name or "anexo").name
        if kind == MessageKind.AUDIO and self._precisa_converter(mime, conteudo):
            conteudo, mime, nome = self._converter_audio(conteudo, nome)

        media = MediaAsset(
            clinic=self.clinic,
            mime_type=mime,
            size_bytes=len(conteudo),
            filename=nome,
            state=MediaState.READY,
        )
        # Duração e onda saem do arquivo aqui mesmo: o balão do atendente tem
        # de nascer com "0:14" e com a onda desenhada, igual ao que ele vê
        # chegar do paciente. Calcular só no download seria calcular nunca —
        # esta mídia não passa pela task de download.
        from apps.inbox.audio import ler_metadados

        media.duration_ms, media.waveform = ler_metadados(conteudo, mime)
        media.stored_file.save(nome, ContentFile(conteudo), save=False)
        media.save()

        # Registrado à mão: o create é próprio (multipart, sem serializer de
        # modelo), então o AuditMixin não tem por onde entrar. E vale
        # registrar — é arquivo saindo da clínica para o WhatsApp de alguém.
        log_action(
            user=request.user,
            action="CREATE",
            resource="MediaAsset",
            resource_id=media.pk,
            payload={
                "after": {
                    "filename": nome,
                    "mime_type": mime,
                    "size_bytes": media.size_bytes,
                }
            },
            request=request,
            clinic=self.clinic,
        )
        return Response(media_payload(media, request), status=HTTP_201_CREATED)

    @staticmethod
    def _precisa_converter(mime: str, conteudo: bytes) -> bool:
        """
        Este áudio precisa virar OGG/Opus antes de ir para a Meta?

        Os formatos que o navegador grava (webm, wav) sempre precisam. E o
        `.ogg` também pode precisar: ele é um CONTAINER, e dentro dele cabe
        vorbis - que a Cloud API não aceita. Como declaramos `codecs=opus` no
        envio, mandar vorbis seria mentir para a Meta e o áudio chegaria
        quebrado no celular do paciente.

        ⚠️ O `ffprobe` só roda para OGG: as gravações da própria recepção já
        saem opus daqui, e conferir todo áudio custaria um processo a mais em
        cada anexo.
        """
        if mime in media_rules.CONVERTER_PARA_OPUS:
            return True
        if mime != media_rules.MIME_OPUS:
            return False

        from apps.inbox.audio import codec_de_audio

        codec = codec_de_audio(conteudo)
        # Codec ilegível: converter é o caminho seguro - o custo é uma
        # passada de ffmpeg, e o preço de errar é o paciente sem o áudio.
        return codec != "opus"

    def _converter_audio(self, conteudo: bytes, nome: str):
        from apps.inbox.audio import converter_para_opus

        convertido = converter_para_opus(conteudo)
        if not convertido:
            raise ValidationError(
                "Não consegui preparar o áudio para envio. Tente gravar de novo."
            )
        return convertido, media_rules.MIME_OPUS, f"{Path(nome).stem or 'audio'}.ogg"
