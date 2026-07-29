"""
Diagnóstico do Inbox num comando só.

Nasceu de uma tarde perdida (29/07/2026) em que "os anexos dão erro" era, na
verdade, o worker com dez horas de código antigo em memória. Descobrir isso
custou seis `docker compose` diferentes e a leitura de um erro da Meta. Este
comando responde de uma vez as perguntas que a gente sempre acaba fazendo:

    python manage.py inbox_doctor

Não conserta nada de propósito: um comando que "arruma sozinho" esconde a
causa, e a causa é o que evita a repetição.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

OK = "\033[32mOK\033[0m"
ALERTA = "\033[33m!!\033[0m"
FALHA = "\033[31mXX\033[0m"


class Command(BaseCommand):
    help = "Diagnostica processamento, filas, canais e mensagens presas do Inbox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sem-cor",
            action="store_true",
            help="Sem códigos de cor (para log ou pipe).",
        )

    def handle(self, *args, **options):
        self.sem_cor = options["sem_cor"]
        self._processamento()
        self._filas()
        self._canais()
        self._mensagens()

    # ------------------------------------------------------------------ #

    def _marca(self, estado: str) -> str:
        if not self.sem_cor:
            return estado
        return {OK: "OK", ALERTA: "!!", FALHA: "XX"}[estado]

    def _linha(self, estado: str, texto: str, detalhe: str = ""):
        self.stdout.write(f"  [{self._marca(estado)}] {texto}")
        if detalhe:
            self.stdout.write(f"       {detalhe}")

    def _titulo(self, texto: str):
        self.stdout.write(f"\n{texto}")

    # ------------------------------------------------------------------ #

    def _processamento(self):
        from apps.core.health import assinatura_do_codigo, saude_do_processamento

        self._titulo("PROCESSAMENTO")
        saude = saude_do_processamento()

        if not saude["alive"]:
            self._linha(FALHA, "Worker fora do ar.", saude["reason"])
            self._linha(
                ALERTA,
                "O que fazer:",
                "docker compose restart medessence_celery_worker medessence_celery_beat",
            )
        elif saude["stale_code"]:
            # O caso que motivou tudo isto. O worker NÃO recarrega sozinho
            # fora do modo de desenvolvimento com watchmedo.
            self._linha(ALERTA, "Worker vivo, mas com código ANTIGO.", saude["reason"])
            self._linha(
                ALERTA,
                "O que fazer:",
                "docker compose restart medessence_celery_worker",
            )
        else:
            self._linha(OK, "Worker vivo e atualizado.")

        if saude["last_seen"]:
            visto = timezone.datetime.fromisoformat(saude["last_seen"])
            atraso = int((timezone.now() - visto).total_seconds())
            self._linha(OK, f"Último batimento há {atraso}s.")
        self._linha(OK, f"Versão do código nesta API: {assinatura_do_codigo()}")

    def _filas(self):
        from apps.core.health import tamanho_das_filas

        self._titulo("FILAS")
        filas = tamanho_das_filas()
        if not filas:
            self._linha(FALHA, "Não deu para ler o Redis — o broker está fora.")
            return
        for nome, tamanho in sorted(filas.items()):
            # Fila com trabalho parado é normal por segundos; dezenas paradas
            # é worker travado ou lento demais para o que está chegando.
            estado = OK if tamanho == 0 else (ALERTA if tamanho < 20 else FALHA)
            self._linha(estado, f"{nome}: {tamanho}")

    def _canais(self):
        from apps.inbox.models import Channel

        self._titulo("CANAIS DE WHATSAPP")
        canais = Channel.objects.select_related("clinic")
        if not canais:
            self._linha(ALERTA, "Nenhum canal cadastrado.")
            return
        for canal in canais:
            if canal.disconnected:
                self._linha(
                    FALHA,
                    f"{canal.clinic} · {canal.display_number}: DESCONECTADO",
                    canal.disconnect_reason or "sem motivo registrado",
                )
            else:
                falhas = (
                    f" ({canal.auth_error_count} falha(s) recente(s))"
                    if canal.auth_error_count
                    else ""
                )
                self._linha(
                    OK, f"{canal.clinic} · {canal.display_number}: conectado{falhas}"
                )

    def _mensagens(self):
        from apps.inbox.models import Channel
        from apps.inbox.services import mensagens_para_reenviar

        self._titulo("MENSAGENS")
        for clinic_id in Channel.objects.values_list("clinic_id", flat=True).distinct():
            presas = mensagens_para_reenviar(clinic_id).count()
            estado = OK if presas == 0 else ALERTA
            self._linha(
                estado,
                f"clínica {clinic_id}: {presas} mensagem(ns) não enviada(s) nas últimas 24h",
            )
        self.stdout.write("")
