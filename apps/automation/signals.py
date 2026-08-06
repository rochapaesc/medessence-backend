import logging

from django.dispatch import receiver

from apps.inbox.dispatch import inbound_ingested

logger = logging.getLogger(__name__)


@receiver(inbound_ingested)
def on_inbound_ingested(sender, conversation, message, **kwargs):
    """
    O paciente falou: continua a execução em andamento ou começa uma nova.

    Falha aqui NÃO pode derrubar a ingestão. Uma mensagem que não foi gravada
    porque o motor de fluxos estourou é pior do que um fluxo que não
    respondeu: a recepção deixa de ver que o paciente escreveu, e o webhook da
    Meta ainda seria reentregue tentando de novo.
    """
    from apps.automation.triggers import handle_inbound

    try:
        handle_inbound(conversation, message)
    except Exception:
        logger.exception("Motor de fluxos falhou no inbound da conversa %s", conversation.pk)
