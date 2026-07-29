"""
Saúde do PROCESSAMENTO — o worker do Celery está vivo, atual e vazando fila?

Existe por causa de uma falha real (29/07/2026): o worker ficou dez horas com o
código antigo em memória, os anexos subiam mas o envio morria, e ninguém tinha
como saber. Reiniciar não resolvia porque nada avisava. São três modos de
falha diferentes, e o `restart: always` só cobre o primeiro:

  1. worker MORTO           → o container reinicia sozinho (só o crash loop escapa)
  2. worker VIVO com código VELHO → nada avisa; foi o nosso caso
  3. worker VIVO mas TRAVADO      → a fila cresce e o batimento para

O sintoma é sempre o mesmo para a recepção: mensagem não sai, mídia não baixa,
mensagem do paciente não aparece. Aqui a gente separa a causa.
"""

import hashlib
import logging
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

# Onde o batimento fica. No cache (Redis) e não no banco: é dado de um minuto
# atrás, sem valor histórico, e um INSERT por minuto por ambiente seria lixo
# crescente numa tabela para responder uma pergunta de "agora".
CHAVE_BATIMENTO = "worker:batimento"

# O batimento é agendado A CADA MINUTO. Três minutos de silêncio já é problema
# — não é atraso de fila, é o worker (ou o beat) fora do ar.
TOLERANCIA = timedelta(minutes=3)

# Guarda no Redis por bastante tempo: um batimento VELHO é informação (diz
# quando parou); um batimento AUSENTE não distingue "nunca rodou" de "parou há
# duas horas".
TTL_BATIMENTO = 60 * 60 * 24

# O que conta como "o código da aplicação". `apps/` e `config/` são o que
# muda; dependências mudam com rebuild da imagem, que já reinicia tudo.
_RAIZES = ("apps", "config")


@lru_cache(maxsize=1)
def assinatura_do_codigo() -> str:
    """
    Impressão digital do código que ESTE processo carregou.

    Em cache no processo de propósito: o valor tem de congelar no instante em
    que o processo subiu. É justamente essa característica que denuncia o
    worker desatualizado — o Django reinicia (autoreload ou deploy) e recalcula,
    o worker parado no tempo continua devolvendo a assinatura antiga.

    Lê o CONTEÚDO, não a data de modificação. Data era o caminho barato e
    falhou nas duas pontas: o mtime do volume do Docker tem resolução de
    segundos (duas edições no mesmo segundo ficam idênticas) e um checkout que
    só reescreve a data acusaria mudança que não houve. Ler ~300 arquivos custa
    milissegundos e acontece UMA vez por processo.
    """
    base = Path(settings.BASE_DIR)
    digestor = hashlib.md5()  # noqa: S324 - impressão digital, não segurança
    achou = False
    for raiz in _RAIZES:
        diretorio = base / raiz
        if not diretorio.is_dir():
            continue
        for arquivo in sorted(diretorio.rglob("*.py")):
            if "__pycache__" in arquivo.parts or "migrations" in arquivo.parts:
                continue
            try:
                digestor.update(str(arquivo.relative_to(base)).encode())
                digestor.update(arquivo.read_bytes())
                achou = True
            except OSError:
                continue
    return digestor.hexdigest()[:12] if achou else "desconhecida"


def registrar_batimento() -> dict:
    """Chamado PELO WORKER, uma vez por minuto. O que ele grava é o que o
    Django lê para saber se ainda tem alguém do outro lado."""
    batimento = {
        "em": timezone.now().isoformat(),
        "assinatura": assinatura_do_codigo(),
    }
    cache.set(CHAVE_BATIMENTO, batimento, TTL_BATIMENTO)
    return batimento


def tamanho_das_filas() -> dict[str, int]:
    """
    Quantas tarefas esperando em cada fila.

    Lê o Redis direto porque é o broker: perguntar ao worker (`inspect`)
    dependeria justamente do worker que pode estar fora — a pergunta ficaria
    sem resposta exatamente quando ela importa.
    """
    try:
        from django_redis import get_redis_connection

        conexao = get_redis_connection("default")
        return {
            fila.name: int(conexao.llen(fila.name)) for fila in settings.CELERY_QUEUES
        }
    except Exception as exc:  # pragma: no cover - Redis fora é o próprio sintoma
        logger.warning("não deu para medir as filas: %s", exc)
        return {}


def saude_do_processamento() -> dict:
    """
    O diagnóstico, pronto para a tela e para o `inbox_doctor`.

    `alive` responde "há alguém executando tarefas?"; `stale_code` responde "é
    a versão certa?". As duas perguntas são independentes — o worker do dia
    29/07 estava VIVO e ERRADO ao mesmo tempo, e um sinal só não separaria os
    casos.
    """
    batimento = cache.get(CHAVE_BATIMENTO)
    filas = tamanho_das_filas()
    pendentes = sum(filas.values())

    if not batimento:
        return {
            "alive": False,
            "stale_code": False,
            "last_seen": None,
            "queued": pendentes,
            "queues": filas,
            # Sem batimento nenhum pode ser worker fora OU beat fora. Quem
            # separa é a fila: parada, ninguém está publicando; crescendo,
            # ninguém está consumindo.
            "reason": (
                "Nenhum batimento registrado — worker ou agendador fora do ar."
            ),
        }

    visto_em = timezone.datetime.fromisoformat(batimento["em"])
    atraso = timezone.now() - visto_em
    vivo = atraso <= TOLERANCIA
    desatualizado = batimento.get("assinatura") != assinatura_do_codigo()

    if not vivo:
        motivo = (
            f"Sem sinal do processamento há {int(atraso.total_seconds() // 60)} min."
        )
    elif desatualizado:
        motivo = "O worker está rodando uma versão antiga do código."
    else:
        motivo = ""

    return {
        "alive": vivo,
        # Só reporta versão velha se o worker ESTÁ VIVO: com ele fora, dizer
        # "código antigo" mandaria consertar a coisa errada.
        "stale_code": vivo and desatualizado,
        "last_seen": batimento["em"],
        "queued": pendentes,
        "queues": filas,
        "reason": motivo,
    }
