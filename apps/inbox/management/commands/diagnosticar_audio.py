"""
Por que o áudio enviado não toca no celular de quem recebe.

Um teste A/B contra a Meta, com o áudio REAL que a recepção grava. Nasceu em
21/08/2026, depois de três hipóteses descartadas por medição:

  1. Content-Type ao servir      → já sai `audio/ogg` (nginx, em produção)
  2. Formato do arquivo          → OGG/Opus mono 48kHz, OpusHead byte a byte
                                   igual ao dos áudios que a Meta entrega
  3. Mime declarado no upload    → a Meta NORMALIZA: sobe-se
                                   `audio/ogg; codecs=opus` e ela guarda
                                   `audio/ogg` do mesmo jeito

O que sobrou para testar exige mandar mensagem DE VERDADE, e por isso é um
comando com destinatário explícito - nunca um teste automático.

    manage.py diagnosticar_audio --clinic 3 --para 5589999999999

⚠️ MANDA MENSAGEM DE VERDADE para o número informado. Use o SEU celular.
Envia dois áudios idênticos, um como nota de voz e outro como áudio comum: se
só um chegar tocável, a resposta está no `is_voice`.
"""

import subprocess

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Envia dois áudios de teste (voz e comum) e mostra o que a Meta responde."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True)
        parser.add_argument(
            "--para", required=True, help="Número do SEU celular, com DDI."
        )

    def handle(self, *args, **options):
        import httpx

        from apps.inbox.models import Channel

        canal = (
            Channel.objects.filter(clinic_id=options["clinic"], provider="meta")
            .order_by("-connected_at")
            .first()
        )
        if canal is None:
            raise CommandError("Esta clínica não tem canal da Meta conectado.")
        token = (canal.credentials or {}).get("access_token")
        if not token:
            raise CommandError("O canal está sem token.")

        destino = options["para"]
        cabecalho = {"Authorization": f"Bearer {token}"}
        base = f"https://graph.facebook.com/v25.0/{canal.phone_number_id}"

        # O MESMO comando que gera a gravação da recepção.
        audio = subprocess.run(
            [  # noqa: S607 - o mesmo ffmpeg do resto do módulo de áudio
                "ffmpeg", "-v", "error", "-f", "lavfi",
                "-i", "aevalsrc=0.5*sin(700*t):d=2",
                "-ac", "1", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            ],
            capture_output=True,
        ).stdout
        if not audio:
            raise CommandError("O ffmpeg não gerou o áudio de teste.")
        self.stdout.write(f"áudio de teste: {len(audio)} bytes\n")

        subida = httpx.post(
            f"{base}/media",
            headers=cabecalho,
            files={
                "file": ("gravacao.ogg", audio, "audio/ogg"),
                "messaging_product": (None, "whatsapp"),
                "type": (None, "audio/ogg"),
            },
            timeout=60,
        )
        media_id = subida.json().get("id")
        self.stdout.write(f"upload: {subida.status_code} {subida.json()}")
        if not media_id:
            raise CommandError("O upload falhou; a resposta acima diz por quê.")

        guardado = httpx.get(f"{base.rsplit('/', 1)[0]}/{media_id}", headers=cabecalho, timeout=60)
        self.stdout.write(f"a Meta guardou: {guardado.json()}\n")

        # ⚠️ O par do teste: o MESMO media id, mudando só o `voice`.
        for voz in (True, False):
            corpo = {
                "messaging_product": "whatsapp",
                "to": destino,
                "type": "audio",
                "audio": {"id": media_id, "voice": voz},
            }
            envio = httpx.post(
                f"{base}/messages", headers=cabecalho, json=corpo, timeout=60
            )
            rotulo = "nota de voz" if voz else "áudio comum"
            self.stdout.write(f"{rotulo:12} -> {envio.status_code} {envio.json()}")

        self.stdout.write(
            self.style.WARNING(
                "\nAbra o celular e diga QUAL dos dois tocou. Se só o 'áudio comum' "
                "tocar, o problema é o `is_voice`; se nenhum tocar, é a mídia; se os "
                "dois tocarem, o defeito está no caminho do CRM, não no formato."
            )
        )
