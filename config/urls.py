from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.inbox.webhooks import whatsapp_webhook

urlpatterns = [
    path("secure-admin/", admin.site.urls),
    path("api/v1/", include("apps.urls_v1")),
    # Webhook do WhatsApp (§7) - endpoint ÚNICO da plataforma: GET devolve o
    # hub.challenge da Meta, POST exige X-Hub-Signature-256; o canal sai do
    # phone_number_id DO PAYLOAD, não da URL.
    path("webhooks/whatsapp/meta/", whatsapp_webhook, name="wa-webhook"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
