"""
Cria na Meta os templates dos passos de uma sequência (RF-SEQ-5.4).

    python manage.py templates_do_resgate --clinic 3 --sequencia "Resgate de inativos"

Para cada passo cujo fluxo abre com nó de template SEM template escolhido:

  1. cria o template na conta da clínica com o `suggested_body` do passo,
     pelo MESMO endpoint da tela (validações do `template_builder` incluídas)
     e manda para a revisão da Meta;
  2. quando a Meta aceita a submissão, aponta o nó do fluxo para o template
     criado, deixando o passo pronto para publicar assim que a aprovação
     chegar.

⚠️ Isto FALA com a conta real da clínica na Meta. Recusa da Meta não perde o
texto: o template fica como rascunho local com o motivo (RF-INB-3.2.5).
"""

import re

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.automation.models import Sequence
from apps.tenants.models import Clinic


def _nome_de_template(sequencia: str, passo: str) -> str:
    """`Resgate de inativos` + `Primeiro convite` → `resgate_de_inativos_primeiro_convite`."""
    bruto = f"{sequencia} {passo}".lower()
    sem_acento = (
        bruto.replace("á", "a").replace("à", "a").replace("ã", "a").replace("â", "a")
        .replace("é", "e").replace("ê", "e").replace("í", "i")
        .replace("ó", "o").replace("ô", "o").replace("õ", "o")
        .replace("ú", "u").replace("ü", "u").replace("ç", "c")
    )
    return re.sub(r"[^a-z0-9]+", "_", sem_acento).strip("_")[:120]


class Command(BaseCommand):
    help = "Cria e submete à Meta os templates dos passos de uma sequência."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument(
            "--sequencia", required=True, help="Nome exato da sequência"
        )
        parser.add_argument(
            "--publicar",
            action="store_true",
            help="Publica os fluxos dos passos que já apontam para template.",
        )

    def handle(self, *args, **options):
        from rest_framework.test import APIClient

        clinic = Clinic.objects.filter(pk=options["clinic"]).first()
        if clinic is None:
            raise CommandError(f"Clínica {options['clinic']} não encontrada.")
        sequence = Sequence.objects.filter(
            clinic=clinic, name=options["sequencia"]
        ).first()
        if sequence is None:
            raise CommandError(
                f"Sequência {options['sequencia']!r} não existe em {clinic.name}."
            )

        gestor = (
            Membership.objects.filter(clinic=clinic, role=MembershipRole.MANAGER)
            .select_related("user")
            .first()
        )
        if gestor is None:
            raise CommandError(f"A clínica {clinic.pk} não tem gestor para autenticar.")

        client = APIClient()
        client.force_authenticate(gestor.user)
        extra = {"SERVER_NAME": "localhost", "HTTP_X_CLINIC_ID": str(clinic.pk)}

        for passo in sequence.steps.select_related("flow").order_by(
            "offset_days", "send_time", "order"
        ):
            versao = passo.flow.current_version
            grafo = (versao.graph or {}) if versao else {}
            fala = next(
                (n for n in grafo.get("nodes", []) if n.get("type") == "send_template"),
                None,
            )
            if fala is None:
                self.stdout.write(f"  {passo.name}: o fluxo não abre com nó de template, pulei.")
                continue
            config = fala.get("config") or {}
            if config.get("template_name"):
                self.stdout.write(
                    f"  {passo.name}: já aponta para {config['template_name']!r}, pulei."
                )
                continue
            corpo = (config.get("suggested_body") or "").strip()
            if not corpo:
                self.stdout.write(f"  {passo.name}: sem sugestão de corpo, pulei.")
                continue

            nome = _nome_de_template(sequence.name, passo.name)

            from apps.inbox.models import WhatsAppTemplate

            existente = WhatsAppTemplate.objects.filter(
                clinic=clinic, name=nome, language="pt_BR"
            ).first()

            # Já chegou à Meta (tem id): não se cria de novo, só se aponta o
            # passo para ele. É o caminho de quando a sincronização trouxe o
            # template antes de o comando rodar.
            if existente is not None and existente.meta_template_id:
                fala["config"] = {
                    "template_name": nome,
                    "variables": {},
                    "suggested_body": corpo,
                }
                versao.save(update_fields=["graph", "updated_at"])
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  {passo.name}: {nome} já existe na Meta "
                        f"({existente.status}); o passo agora aponta para ele."
                    )
                )
                continue

            # Sobra de uma falha de credencial: rascunho recusado SEM id da
            # Meta. Some para dar lugar ao novo.
            if existente is not None and existente.status == "REJECTED":
                existente.delete()

            resposta = client.post(
                "/api/v1/wa-templates/",
                {
                    "name": nome,
                    # Resgate é DIVULGAÇÃO na política da Meta (§4.5): UTILITY
                    # aqui seria recategorizado ou recusado na revisão.
                    "category": "MARKETING",
                    "language": "pt_BR",
                    "body": corpo,
                },
                format="json",
                **extra,
            )

            if resposta.status_code != 201:
                corpo_erro = getattr(resposta, "data", None) or resposta.content[:300]
                self.stdout.write(
                    self.style.ERROR(f"  {passo.name}: HTTP {resposta.status_code} · {corpo_erro}")
                )
                continue

            status = resposta.data.get("status")
            motivo = resposta.data.get("rejection_reason") or ""
            if status == "REJECTED":
                # A Meta recusou a SUBMISSÃO (ou não deu para falar com ela):
                # o texto ficou como rascunho local com o motivo.
                self.stdout.write(
                    self.style.WARNING(f"  {passo.name}: {nome} NÃO submetido · {motivo}")
                )
                continue

            # Submetido: o nó do fluxo passa a apontar para ele, pronto para
            # publicar quando a aprovação chegar.
            fala["config"] = {
                "template_name": nome,
                "variables": {},
                "suggested_body": corpo,
            }
            versao.save(update_fields=["graph", "updated_at"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"  {passo.name}: {nome} criado e em revisão ({status}); o passo já aponta para ele."
                )
            )

        if options["publicar"]:
            self.stdout.write("")
            for passo in sequence.steps.select_related("flow"):
                flow = passo.flow
                if flow.status == "active":
                    self.stdout.write(f"  {passo.name}: fluxo já publicado.")
                    continue
                resposta = client.post(
                    f"/api/v1/flows/{flow.pk}/activate/", format="json", **extra
                )
                if resposta.status_code == 200:
                    self.stdout.write(
                        self.style.SUCCESS(f"  {passo.name}: fluxo publicado.")
                    )
                else:
                    corpo_erro = getattr(resposta, "data", None) or resposta.content[:300]
                    self.stdout.write(
                        self.style.ERROR(
                            f"  {passo.name}: publicação recusada · {corpo_erro}"
                        )
                    )

        self.stdout.write("")
        self.stdout.write(
            "O veredito da Meta chega pela sincronização (botão Atualizar agora "
            "na tela de Templates, ou o beat de 6h)."
        )
