"""
Configuração do gunicorn (produção) - ASGI
Uso: gunicorn -c config/gunicorn.conf.py config.asgi:application

O gunicorn aqui NÃO serve requisição: ele supervisiona processos (forka
workers, reinicia quem morre, faz reload gracioso). Quem fala HTTP e
WebSocket dentro de cada worker É O UVICORN - `worker_class` abaixo. Não
existe "trocar para uvicorn em produção por causa do WebSocket": já é
uvicorn. No dev roda-se uvicorn direto só para ter `--reload`.

Verificado ao vivo em 28/07/2026 (esta config, com --workers 2):
  - handshake WS aceito, e evento publicado de OUTRO PROCESSO chegou ao
    cliente conectado -> o channels-redis faz a ponte entre workers, então
    múltiplos workers são seguros (o cliente não precisa cair sempre no
    mesmo processo);
  - conexão sobreviveu 25s com `--timeout 10`.
"""  # noqa: N999

import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('BACKEND_PORT', '9000')}"
worker_class = "uvicorn_worker.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count()))

# NÃO reduzir achando que isto derruba WebSocket: para worker assíncrono o
# `timeout` do gunicorn é o HEARTBEAT do worker, não o tempo de vida da
# conexão. Testado: conexão viva por 25s com timeout de 10s.
timeout = 120

# Em deploy/restart as conexões WS caem após este prazo. É esperado - o
# cliente do Inbox reconecta com backoff e faz catch-up pelo REST (§12).
graceful_timeout = 30

keepalive = 5

# ⚠️ PRODUÇÃO - o elo que falta não está aqui: o proxy da frente
# (nginx/ALB/Cloudflare) PRECISA repassar os cabeçalhos `Upgrade` e
# `Connection` e ter read timeout maior que o intervalo ocioso do WebSocket.
# Sem isso a conexão morre NO PROXY, e o sintoma parece problema do gunicorn.
# Não há proxy versionado neste repo ainda.

accesslog = "-"
errorlog = "-"
loglevel = "info"
