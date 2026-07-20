"""
ASGI config for config project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

# Instancia o app HTTP ANTES de importar consumers (que tocam models/apps).
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402

from apps.inbox.routing import websocket_urlpatterns  # noqa: E402
from apps.inbox.ws_auth import JWTClinicMiddleware  # noqa: E402

# Em DEBUG replicamos o runserver (serve estáticos pelos finders); em produção
# quem serve é o WhiteNoise/nginx.
http_app = ASGIStaticFilesHandler(django_asgi_app) if settings.DEBUG else django_asgi_app

application = ProtocolTypeRouter(
    {
        "http": http_app,
        "websocket": JWTClinicMiddleware(URLRouter(websocket_urlpatterns)),
    }
)
