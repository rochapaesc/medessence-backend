"""
Configura o canal WhatsApp de uma clínica (§7).

As credenciais NÃO vêm por argumento de linha de comando: viriam no `ps` de
qualquer processo da máquina e no histórico do shell. Vêm por variável de
ambiente, e o comando nunca as imprime de volta.

    WA_TOKEN='...' WA_PHONE_ID='...' WA_WABA_ID='...' \\
      manage.py wa_channel medessence --display-number '+55 85 99999-0000'

Conferir o que está gravado (mascarado):
    manage.py wa_channel medessence --show

Validar contra a Meta antes de confiar (uma chamada real ao Graph):
    manage.py wa_channel medessence --verify
"""

import os

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel
from apps.tenants.models import Clinic

ENV_TOKEN = "WA_TOKEN"
ENV_PHONE_ID = "WA_PHONE_ID"
ENV_WABA_ID = "WA_WABA_ID"


def _mask(value: str) -> str:
    """Só o suficiente para conferir que é o valor certo, nunca o valor."""
    if not value:
        return "—"
    if len(value) <= 6:
        return "•" * len(value)
    return f"{'•' * 8}{value[-4:]} ({len(value)} caracteres)"


class Command(BaseCommand):
    help = "Cria/atualiza o canal WhatsApp (Meta Cloud API) de uma clínica."

    def add_arguments(self, parser):
        parser.add_argument("clinic_slug", help="Slug da clínica.")
        parser.add_argument(
            "--display-number",
            default="",
            help="Número como aparece para o paciente (só rótulo de tela).",
        )
        parser.add_argument(
            "--show",
            action="store_true",
            help="Mostra o que está gravado (mascarado) e sai.",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="Chama a Meta para conferir se as credenciais funcionam.",
        )

    def handle(self, *args, **options):
        slug = options["clinic_slug"]
        try:
            clinic = Clinic.objects.get(slug=slug)
        except Clinic.DoesNotExist as exc:
            raise CommandError(f"Clínica '{slug}' não existe.") from exc

        channel = Channel.objects.filter(clinic=clinic).first()

        if options["show"]:
            return self._show(clinic, channel)
        if options["verify"]:
            return self._verify(clinic, channel)

        token = os.environ.get(ENV_TOKEN, "").strip()
        phone_id = os.environ.get(ENV_PHONE_ID, "").strip()
        waba_id = os.environ.get(ENV_WABA_ID, "").strip()

        faltando = [
            nome
            for nome, valor in (
                (ENV_TOKEN, token),
                (ENV_PHONE_ID, phone_id),
                (ENV_WABA_ID, waba_id),
            )
            if not valor
        ]
        if faltando:
            raise CommandError(
                "Faltam variáveis de ambiente: "
                + ", ".join(faltando)
                + ".\nEx.: WA_TOKEN='...' WA_PHONE_ID='...' WA_WABA_ID='...' "
                "manage.py wa_channel <slug>"
            )

        # Erro comum: colar o NÚMERO no lugar do phone_number_id.
        if phone_id.startswith("+") or " " in phone_id:
            raise CommandError(
                f"{ENV_PHONE_ID} parece um telefone, não um id. O phone_number_id é "
                "o número longo em WhatsApp → Configuração da API (ex.: 109876543210987)."
            )

        criado = channel is None
        if criado:
            channel = Channel(clinic=clinic)

        channel.provider = WhatsAppProviderKind.META
        channel.phone_number_id = phone_id
        channel.waba_id = waba_id
        if options["display_number"]:
            channel.display_number = options["display_number"]
        # `credentials` é EncryptedJSONField: o token fica cifrado no banco.
        channel.credentials = {**(channel.credentials or {}), "access_token": token}
        channel.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Canal {'criado' if criado else 'atualizado'} para {clinic.name}."
            )
        )
        self._show(clinic, channel)
        self.stdout.write(
            "\nConfira contra a Meta antes de calibrar:\n"
            f"  manage.py wa_channel {slug} --verify"
        )

    # ------------------------------------------------------------------ #

    def _show(self, clinic, channel):
        if channel is None:
            self.stdout.write(
                self.style.WARNING(f"{clinic.name} ainda não tem canal WhatsApp.")
            )
            return
        credentials = channel.credentials or {}
        self.stdout.write(f"Clínica ......... {clinic.name} ({clinic.slug})")
        self.stdout.write(f"Provedor ........ {channel.get_provider_display()}")
        self.stdout.write(f"phone_number_id . {channel.phone_number_id or '—'}")
        self.stdout.write(f"waba_id ......... {channel.waba_id or '—'}")
        self.stdout.write(f"Número exibido .. {channel.display_number or '—'}")
        self.stdout.write(f"access_token .... {_mask(credentials.get('access_token', ''))}")

    def _verify(self, clinic, channel):
        """Uma chamada real ao Graph: se o número volta, as credenciais servem."""
        if channel is None:
            raise CommandError(f"{clinic.name} não tem canal. Rode o comando sem --verify antes.")

        # Com LOG_LEVEL=DEBUG o httpx despeja cabeçalhos e corpo da chamada, e
        # o resultado - que é o ponto do comando - se perde no meio. Pior:
        # imprime o Authorization. Silencia só aqui, sem mexer no logging global.
        import logging

        for nome in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection", "pywa"):
            logging.getLogger(nome).setLevel(logging.WARNING)

        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        provider = get_whatsapp_provider(channel)
        try:
            # A MESMA sonda que a tela usa no "Já reconectei — verificar". Este
            # comando furava para o `_wa` do PyWa; agora as duas portas de
            # saída passam pelo mesmo lugar, e uma não pode divergir da outra.
            numero = provider.verify_credentials()
        except WhatsAppError as exc:
            raise CommandError(f"A Meta recusou as credenciais: {exc}") from exc
        except Exception as exc:  # rede, id inexistente, resposta inesperada
            raise CommandError(f"Não consegui falar com a Meta: {exc}") from exc

        self.stdout.write(self.style.SUCCESS("Credenciais VÁLIDAS na Meta."))
        self.stdout.write(f"  Número .......... {numero.get('display_phone_number') or '—'}")
        self.stdout.write(f"  Nome verificado . {numero.get('verified_name') or '—'}")
        self.stdout.write(f"  Qualidade ....... {numero.get('quality_rating') or '—'}")

        try:
            templates = provider.list_templates()
        except WhatsAppError as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"  Templates: não consegui listar ({exc}). "
                    "Confira o waba_id e a permissão whatsapp_business_management."
                )
            )
            return
        aprovados = [t for t in templates if (t.status or "").upper() == "APPROVED"]
        self.stdout.write(f"  Templates ....... {len(templates)} ({len(aprovados)} aprovados)")
