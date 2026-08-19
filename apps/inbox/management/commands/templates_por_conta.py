"""
Mostra (e limpa) o catálogo de templates por CONTA da Meta (RF-INB-3.3).

    python manage.py templates_por_conta --clinic 3
    python manage.py templates_por_conta --clinic 3 --sincronizar
    python manage.py templates_por_conta --clinic 3 --limpar

Template pertence à CONTA, não ao número: a clínica que troca de número ou de
app passa a usar outra conta, e o catálogo antigo ficava misturado com o novo.

Sem opção nenhuma, o comando **só olha**. É de propósito: antes de apagar
qualquer coisa dá para ver quantos templates são de cada conta, quais sumiram
da tela e quais fluxos apontam para eles.

  --sincronizar  vai à Meta e recarimba o que existe de verdade na conta atual
  --limpar       apaga o que NÃO é da conta atual (exige --confirmo)
"""

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.models import Channel, WhatsAppTemplate
from apps.inbox.template_scope import conta_da_clinica
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Inventário de templates por conta da Meta, e limpeza do que ficou para trás."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True)
        parser.add_argument("--sincronizar", action="store_true")
        parser.add_argument("--limpar", action="store_true")
        parser.add_argument("--confirmo", action="store_true")

    def handle(self, *args, **opts):
        clinic = Clinic.objects.filter(pk=opts["clinic"]).first()
        if clinic is None:
            raise CommandError(f"Clínica {opts['clinic']} não existe.")

        channel = Channel.objects.filter(clinic=clinic, is_test=False).first()
        atual = conta_da_clinica(clinic.pk)
        self.stdout.write(f"Clínica {clinic.pk} ({clinic.name})")
        self.stdout.write(
            f"  canal: {channel.display_number if channel else '(nenhum)'}\n"
            f"  conta da Meta em uso: {atual or '(nenhuma)'}\n"
        )

        if opts["sincronizar"]:
            self._sincronizar(clinic)

        self._inventario(clinic, atual)

        if opts["limpar"]:
            self._limpar(clinic, atual, confirmo=opts["confirmo"])
        else:
            fora = self._fora_da_conta(clinic, atual).count()
            if fora:
                self.stdout.write(
                    f"\n{fora} template(s) fora da conta atual. Para apagar: "
                    "--limpar --confirmo"
                )

    def _sincronizar(self, clinic):
        from apps.inbox.tasks import sincronizar_templates_da_clinica
        from apps.integrations.whatsapp.exceptions import WhatsAppAuthError

        self.stdout.write("Sincronizando com a Meta...")
        try:
            quantos = sincronizar_templates_da_clinica(clinic)
        except WhatsAppAuthError as erro:
            # Rastro de pilha aqui não ajuda ninguém: o token temporário da
            # Meta vence sozinho, e o conserto é gerar outro no painel dela.
            raise CommandError(
                f"A Meta recusou a credencial deste canal: {erro}\n"
                "O token temporário vence sozinho. Gere um novo no painel da "
                "Meta e atualize o canal antes de sincronizar."
            ) from erro
        self.stdout.write(self.style.SUCCESS(f"  {quantos} template(s) na conta atual.\n"))

    def _fora_da_conta(self, clinic, atual):
        return WhatsAppTemplate.objects.filter(clinic=clinic).exclude(waba_id=atual)

    def _inventario(self, clinic, atual):
        por_conta = {}
        for t in WhatsAppTemplate.objects.filter(clinic=clinic).order_by("name"):
            por_conta.setdefault(t.waba_id, []).append(t)

        for waba_id, itens in sorted(por_conta.items(), key=lambda kv: kv[0] != atual):
            marca = "EM USO" if waba_id == atual else "não é a conta atual"
            self.stdout.write(f"  conta {waba_id or '(desconhecida)'} [{marca}]: {len(itens)}")
            for t in itens:
                origem = "da Meta" if t.meta_template_id else "rascunho local"
                self.stdout.write(f"     {t.name} ({t.language}) {t.status} · {origem}")

        self._quem_aponta(clinic, atual)

    def _quem_aponta(self, clinic, atual):
        """
        Fluxos e passos que apontam para template que a tela não oferece mais.

        ⚠️ Eles JÁ falhariam no envio; o que muda é passar a falhar também na
        validação, de uma vez. Ver antes evita a surpresa.
        """
        from apps.automation.models import Flow

        visiveis = set(
            WhatsAppTemplate.objects.filter(clinic=clinic, waba_id=atual).values_list(
                "name", flat=True
            )
        )
        orfaos = []
        for flow in Flow.objects.filter(clinic=clinic).select_related("current_version"):
            grafo = getattr(flow.current_version, "graph", None) or {}
            for no in grafo.get("nodes", []):
                nome = ((no.get("config") or {}).get("template_name") or "").strip()
                if nome and nome not in visiveis:
                    orfaos.append((flow.name, nome))
        if orfaos:
            self.stdout.write(
                self.style.WARNING(
                    f"\n  ⚠️ {len(orfaos)} nó(s) de fluxo apontam para template fora da conta:"
                )
            )
            for fluxo, nome in orfaos:
                self.stdout.write(f"     {fluxo} → {nome}")

    def _limpar(self, clinic, atual, *, confirmo):
        fora = self._fora_da_conta(clinic, atual)
        quantos = fora.count()
        if not quantos:
            self.stdout.write("\nNada a limpar: tudo já é da conta atual.")
            return
        if not confirmo:
            raise CommandError(
                f"\n{quantos} template(s) seriam APAGADOS (os de outra conta). "
                "Repita com --confirmo."
            )
        fora.hard_delete()
        self.stdout.write(self.style.SUCCESS(f"\n{quantos} template(s) apagados."))
