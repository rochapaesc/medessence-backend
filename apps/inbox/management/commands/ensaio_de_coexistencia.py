"""
Exercita os webhooks da coexistência sem depender da Meta (§4.3.3, fatia B).

Os três eventos entram pelo ENDPOINT de verdade, assinados como a Meta assina,
para o ensaio provar o caminho inteiro: assinatura, roteamento do canal,
parser, ingestão e o efeito na fila. Chamar o serviço direto pularia justamente
as partes que já quebraram calado aqui.

    manage.py ensaio_de_coexistencia --clinic 1
    manage.py ensaio_de_coexistencia --clinic 1 --limpar

⚠️ Só roda em clínica de canal FAKE. Na clínica real o eco inventaria uma
mensagem que ninguém enviou dentro da conversa de um paciente de verdade, e o
aviso de conta derrubaria o canal que está atendendo.
"""

import hashlib
import hmac
import json
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import Client

from apps.inbox.choices import ConversationStatus, WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation, Message, WebhookEvent
from apps.patients.models import Contact

WAMID = "wamid.ENSAIO-COEXISTENCIA-1"
WAMID_SEM_TELEFONE = "wamid.ENSAIO-COEXISTENCIA-2"
NUMERO_DA_AGENDA = "5589900000077"
BSUID = "BR.10000000000000000001"
URL = "/webhooks/whatsapp/meta/"
WABA_DE_ENSAIO = "waba-ensaio"
ESPERA_MAXIMA = 20.0
INTERVALO = 0.4


class Command(BaseCommand):
    help = "Ensaia eco do celular, contato da agenda e remoção da integração."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True)
        parser.add_argument(
            "--limpar",
            action="store_true",
            help="Desfaz o que o ensaio criou e religa o canal.",
        )

    def handle(self, *args, **options):
        canal = Channel.objects.filter(
            clinic_id=options["clinic"], is_test=False
        ).first()
        if canal is None:
            raise CommandError(f"A clínica {options['clinic']} não tem canal.")
        if canal.provider != WhatsAppProviderKind.FAKE:
            raise CommandError(
                "Este ensaio só roda em clínica de canal FAKE. O eco inventaria "
                "uma mensagem dentro da conversa de um paciente de verdade."
            )

        if options["limpar"]:
            return self._limpar(canal)

        # ⚠️ O aviso de conta é achado pelo WABA, e canal fake nasce sem um.
        # Sem isto o passo 3 do ensaio não acharia canal nenhum e pareceria
        # defeito do código, quando é só o cenário faltando.
        if not canal.waba_id:
            canal.waba_id = f"{WABA_DE_ENSAIO}-{canal.clinic_id}"
            canal.save(update_fields=["waba_id", "updated_at"])
            self.stdout.write(f"Canal sem WABA: usando {canal.waba_id} para o ensaio.")

        conversa = (
            Conversation.objects.filter(clinic=canal.clinic)
            .order_by("-last_message_at")
            .first()
        )
        if conversa is None:
            raise CommandError(
                "A clínica não tem conversa nenhuma. Rode `seed_inbox_demo` antes."
            )

        self._eco(canal, conversa)
        self._agenda(canal)
        self._sem_telefone(canal)
        self._conta(canal)
        self.stdout.write(
            "\nPara desfazer:\n"
            f"  manage.py ensaio_de_coexistencia --clinic {canal.clinic_id} --limpar"
        )

    # ------------------------------------------------------------------ #

    def _eco(self, canal, conversa):
        self.stdout.write(self.style.MIGRATE_HEADING("\n1. A clínica respondeu pelo celular"))
        conversa.status = ConversationStatus.WAITING
        conversa.unread_count = 3
        conversa.waiting_since = conversa.waiting_since or conversa.created_at
        conversa.save()
        self.stdout.write(
            f"   antes  ..... {conversa.status}, {conversa.unread_count} não lidas, "
            f"posse {conversa.attended_by}"
        )

        self._postar(
            canal,
            {
                "field": "smb_message_echoes",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": canal.display_number,
                        "phone_number_id": canal.phone_number_id,
                    },
                    "message_echoes": [
                        {
                            "from": canal.display_number,
                            "to": conversa.contact.wa_id,
                            "id": WAMID,
                            "timestamp": "1755600000",
                            "type": "text",
                            "text": {"body": "Respondi por aqui, pelo celular."},
                        }
                    ],
                },
            },
        )

        conversa.refresh_from_db()
        mensagem = Message.objects.filter(provider_message_id=WAMID).first()
        self.stdout.write(
            f"   depois ..... {conversa.status}, {conversa.unread_count} não lidas, "
            f"posse {conversa.attended_by}"
        )
        if mensagem is None:
            self.stdout.write(self.style.ERROR("   a mensagem NÃO entrou na conversa"))
            return
        self.stdout.write(
            f"   balão ...... {mensagem.direction}, do celular: {mensagem.from_phone}, "
            f"na conversa de {mensagem.conversation.contact}"
        )

    def _agenda(self, canal):
        self.stdout.write(self.style.MIGRATE_HEADING("\n2. Contato salvo na agenda do celular"))
        self._postar(
            canal,
            {
                "field": "smb_app_state_sync",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": canal.display_number,
                        "phone_number_id": canal.phone_number_id,
                    },
                    "action": "add",
                    "contact_phone_number": NUMERO_DA_AGENDA,
                    "contact_name": "Contato do Ensaio",
                },
            },
        )
        contato = Contact.objects.filter(
            clinic=canal.clinic, wa_id=NUMERO_DA_AGENDA
        ).first()
        self.stdout.write(
            f"   contato .... {contato or 'NÃO criado'}"
            + (
                f" (conversas: {contato.conversations.count()})"
                if contato is not None
                else ""
            )
        )
        self.stdout.write("   ⚠️ salvar na agenda não abre conversa, e é assim mesmo")

    def _sem_telefone(self, canal):
        """
        A pessoa adotou nome de usuário: a Meta manda a mensagem SEM telefone,
        só com o identificador dela (RF-CON-6).
        """
        self.stdout.write(
            self.style.MIGRATE_HEADING("\n3. Mensagem de quem usa nome de usuário")
        )
        self._postar(
            canal,
            {
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": canal.display_number,
                        "phone_number_id": canal.phone_number_id,
                    },
                    "contacts": [
                        {"profile": {"name": "Pessoa do Ensaio"}, "user_id": BSUID}
                    ],
                    "messages": [
                        {
                            "id": WAMID_SEM_TELEFONE,
                            "from_user_id": BSUID,
                            "timestamp": "1755600100",
                            "type": "text",
                            "text": {"body": "Oi, vim pelo nome de usuário."},
                        }
                    ],
                },
            },
        )
        contato = Contact.objects.filter(clinic=canal.clinic, user_id=BSUID).first()
        if contato is None:
            self.stdout.write(self.style.ERROR("   contato NÃO criado"))
            return
        self.stdout.write(
            f"   contato .... {contato} | telefone: {contato.wa_id or '(nenhum)'} "
            f"| conversas: {contato.conversations.count()}"
        )
        self.stdout.write(f"   resposta ... sairia por {contato.destino}")

    def _conta(self, canal):
        self.stdout.write(
            self.style.MIGRATE_HEADING("\n4. A integração foi removida pelo celular")
        )
        self._postar(
            canal,
            {
                "field": "account_update",
                "value": {
                    "phone_number": canal.display_number,
                    "event": "PARTNER_REMOVED",
                    "disconnection_info": {
                        "disconnect_reason": "partner_removed",
                        "initiator": "business",
                    },
                },
            },
            # ⚠️ Este evento vem SEM metadata: o canal é achado pelo WABA do
            # `entry.id`. É o caminho que não existia antes da fatia B.
            com_numero=False,
        )
        canal.refresh_from_db()
        self.stdout.write(
            f"   canal ...... {'DESCONECTADO' if canal.disconnected else 'no ar'}"
            f" | motivo: {canal.disconnect_reason or '—'}"
        )

    def _postar(self, canal, change: dict, *, com_numero: bool = True) -> None:
        """Assina e entrega como a Meta faria, pelo endpoint público."""
        entry = {"id": canal.waba_id, "changes": [change]}
        if not com_numero:
            entry["time"] = 1755600000
        corpo = json.dumps({"entry": [entry]}).encode()

        segredo = (canal.credentials or {}).get("app_secret") or settings.WHATSAPP_APP_SECRET
        if not segredo:
            raise CommandError(
                "Sem app secret configurado, o webhook recusa tudo (fail closed). "
                "Defina WHATSAPP_APP_SECRET ou o `app_secret` do canal."
            )
        assinatura = "sha256=" + hmac.new(segredo.encode(), corpo, hashlib.sha256).hexdigest()

        # ⚠️ `SERVER_NAME` obrigatório: fora do pytest, `testserver` (o padrão
        # do Client) não está em ALLOWED_HOSTS e a chamada morre com 400 de
        # DisallowedHost, que não tem nada a ver com o webhook.
        antes = WebhookEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
        resposta = Client(SERVER_NAME="localhost").post(
            URL,
            data=corpo,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=assinatura,
        )
        if resposta.status_code != 200:
            raise CommandError(
                f"O webhook recusou ({resposta.status_code}). "
                "Confira o app secret e a assinatura."
            )
        self._esperar_o_worker(antes)

    def _esperar_o_worker(self, ultimo_id_anterior: int) -> None:
        """
        ⚠️ O webhook responde 200 e joga na fila (RNF-4), então ler o resultado
        na linha seguinte mostra o estado ANTES do processamento e o ensaio
        mentiria dizendo que nada aconteceu. Aqui se espera o evento sair de
        pendente, que é o mesmo sinal que o `inbox_doctor` usa.
        """
        for _ in range(int(ESPERA_MAXIMA / INTERVALO)):
            evento = (
                WebhookEvent.objects.filter(id__gt=ultimo_id_anterior)
                .order_by("-id")
                .first()
            )
            if evento is None or evento.processed_at or evento.error:
                if evento is not None and evento.error:
                    self.stdout.write(self.style.ERROR(f"   erro: {evento.error[:200]}"))
                return
            time.sleep(INTERVALO)
        self.stdout.write(
            self.style.WARNING(
                "   o worker não processou a tempo. Ele está de pé? "
                "`docker compose ps medessence_celery_worker`"
            )
        )

    def _limpar(self, canal):
        # ⚠️ Exclusão de VERDADE, e não o soft delete do projeto: a constraint
        # `uniq_message_wamid` **não** dispensa registro apagado, então um
        # `delete()` comum deixaria o wamid ocupado e o ensaio seguinte
        # estouraria com violação de unicidade em vez de rodar.
        Message.all_objects.filter(
            provider_message_id__in=[WAMID, WAMID_SEM_TELEFONE]
        ).hard_delete()
        # A conversa de quem entrou só pelo identificador vai junto: ela nasceu
        # do ensaio e não tem paciente por trás.
        sem_telefone = Contact.all_objects.filter(clinic=canal.clinic, user_id=BSUID)
        Conversation.all_objects.filter(contact__in=sem_telefone).hard_delete()
        sem_telefone.hard_delete()
        Contact.all_objects.filter(
            clinic=canal.clinic, wa_id=NUMERO_DA_AGENDA
        ).hard_delete()
        canal.disconnected_at = None
        canal.disconnect_reason = ""
        canal.auth_error_count = 0
        campos = ["disconnected_at", "disconnect_reason", "auth_error_count", "updated_at"]
        # Só o WABA que o ENSAIO inventou sai; um de verdade fica onde está.
        if canal.waba_id.startswith(WABA_DE_ENSAIO):
            canal.waba_id = ""
            campos.append("waba_id")
        canal.save(update_fields=campos)
        self.stdout.write(self.style.SUCCESS("Ensaio desfeito e canal religado."))
