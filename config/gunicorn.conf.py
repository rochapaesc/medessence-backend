"""
Configuração do gunicorn (produção) - ASGI
Uso: gunicorn -c config/gunicorn.conf.py config.asgi:application
"""  # noqa: N999

import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('BACKEND_PORT', '9000')}"
worker_class = "uvicorn_worker.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count()))

timeout = 120
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = "info"
