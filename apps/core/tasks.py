from celery import shared_task


@shared_task(queue="default")
def worker_heartbeat():
    """
    Batimento do worker, agendado a cada minuto pelo beat.

    Vai na fila `default`, que nenhum outro trabalho usa, de propósito: numa
    fila movimentada o batimento ficaria atrás de um download de mídia lento e
    a tela acusaria "processamento parado" por causa de UMA tarefa demorada.
    Aqui ele mede o que promete — se o worker está consumindo.

    Como precisa do beat para acontecer, o silêncio acusa os dois: worker fora
    ou agendador fora. Quem separa os casos é o tamanho da fila.
    """
    from apps.core.health import registrar_batimento

    return registrar_batimento()
