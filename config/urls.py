from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.inbox.webhooks import whatsapp_webhook

urlpatterns = [
    path("secure-admin/", admin.site.urls),
    path("api/v1/", include("apps.urls_v1")),
    # Webhook do WhatsApp (§7) — público, autenticado por uuid+segredo na URL.
    path(
        "webhooks/whatsapp/<uuid:channel_uuid>/<str:secret>/", whatsapp_webhook, name="wa-webhook"
    ),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
