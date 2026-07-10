"""
ASGI do projeto — HTTP (Django) + WebSocket (Channels, §12).

O projeto já nasceu ASGI; a Fatia C só liga o roteamento WebSocket. Em
produção rode um servidor ASGI (uvicorn/gunicorn+UvicornWorker):

    uvicorn config.asgi:application
    gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Instancia o app HTTP ANTES de importar consumers (que tocam models/apps).
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402

from apps.inbox.routing import websocket_urlpatterns  # noqa: E402
from apps.inbox.ws_auth import JWTClinicMiddleware  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": JWTClinicMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
