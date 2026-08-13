"""
O veredito da Meta sobre um template, chegando por webhook (RF-INB-3.2.10).

⚠️ Sem isto, o status só se atualiza no beat de 6 em 6 horas ou no botão
"Atualizar" da tela: um template aprovado em dois minutos podia ficar
`EM REVISÃO` para a clínica por horas. A Meta AVISA quando o veredito sai, e é
esse aviso que este módulo trata.

⚠️ **A assinatura é passo manual, e não tem API.** Os três campos abaixo NÃO
vêm assinados por padrão: alguém precisa marcá-los no painel da Meta, em
WhatsApp → Configuration → Webhooks, uma vez por app. É o que o wacrm
documenta no `template-webhook.ts`, e é por isso que ele mantém o botão de
sincronizar de propósito ("legacy fallback, intentionally preserved") — nós
mantemos pelo mesmo motivo. Até a marcação existir, este código fica inerte e
nada quebra.

Casa pelo `meta_template_id`, que é único por WABA. Casar por nome exigiria
adivinhar o idioma e acertaria a variante errada.
"""

import logging

logger = logging.getLogger(__name__)

#: O que a Meta manda sobre o ciclo de vida de um template.
CAMPOS = {
    "message_template_status_update",
    "message_template_quality_update",
    "message_template_components_update",
}

#: Nota de qualidade que ela atribui ao template pelo comportamento de quem
#: recebe. Vermelho é o passo antes de pausar.
QUALIDADES = {"GREEN", "YELLOW", "RED"}


def e_de_template(field: str) -> bool:
    return field in CAMPOS


def aplicar(clinic, field: str, value: dict) -> str:
    """
    Aplica UMA mudança de template. Devolve o que foi feito, para o log.

    Nunca estoura: webhook que falha é reentregue pela Meta em laço, e um
    campo novo que ela invente não pode derrubar o processamento das mensagens
    que vêm no mesmo lote.
    """
    if field == "message_template_status_update":
        return _status(clinic, value)
    if field == "message_template_quality_update":
        return _qualidade(clinic, value)
    if field == "message_template_components_update":
        return _componentes(value)
    return "ignorado"


def _achar(clinic, value: dict):
    from apps.inbox.models import WhatsAppTemplate

    meta_id = value.get("message_template_id")
    if meta_id in (None, ""):
        return None, "sem message_template_id"
    template = WhatsAppTemplate.objects.filter(
        clinic=clinic, meta_template_id=str(meta_id)
    ).first()
    if template is None:
        # Template criado direto no painel da Meta, ou de outro produto que
        # divide o mesmo WABA. Não é erro: a sincronização o traz depois.
        return None, f"template desconhecido ({meta_id})"
    return template, ""


def _status(clinic, value: dict) -> str:
    from apps.inbox.template_builder import status_normalizado

    template, problema = _achar(clinic, value)
    if template is None:
        return problema

    evento = value.get("event") or ""
    if not evento:
        return "sem event"

    novo = status_normalizado(evento)
    template.status = novo
    # ⚠️ O motivo só vem no REJECTED, e some em qualquer outro veredito: sem
    # limpar, a tela mostraria o aviso vermelho da recusa anterior depois de a
    # Meta ter aprovado o texto corrigido.
    template.rejection_reason = (
        (value.get("reason") or "Recusado pela Meta.") if novo == "REJECTED" else ""
    )
    template.save(update_fields=["status", "rejection_reason"])
    return f"{template.name}: {novo}"


def _qualidade(clinic, value: dict) -> str:
    """
    A nota que a Meta dá ao template pelo comportamento de quem recebe.

    Guardamos no `quality_score` porque VERMELHO é o passo antes de ela pausar
    o template sozinha - e template pausado para de enviar no meio de um fluxo,
    sem ninguém ter mexido em nada.
    """
    template, problema = _achar(clinic, value)
    if template is None:
        return problema

    nota = (value.get("new_quality_score") or "").upper()
    template.quality_score = nota if nota in QUALIDADES else ""
    template.save(update_fields=["quality_score"])
    return f"{template.name}: qualidade {template.quality_score or 'desconhecida'}"


def _componentes(value: dict) -> str:
    """
    A Meta mexeu no template sozinha, em geral reclassificando a categoria
    (Marketing → Utility depois da revisão de conteúdo).

    ⚠️ NÃO gravamos o que ela mandou: o evento não traz os componentes novos,
    só avisa que mudaram. Gravar por cima do que a clínica escreveu, sem ela
    ver, faria a tela mostrar um texto que ninguém aprovou. O caminho é o
    botão "Atualizar", que busca o estado completo.
    """
    logger.info(
        "A Meta alterou o template %s (%s). Use Atualizar para trazer o novo.",
        value.get("message_template_name"),
        value.get("message_template_id"),
    )
    return "componentes alterados pela Meta"
