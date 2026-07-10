"""
Client HTTP da Datafy (proxy da Meta Cloud API, §7).

⚠️ Contrato a CALIBRAR com número/WABA real (mesmo caminho da vSaúde). O
desenho segue a Meta Cloud API: `POST /v1/{phone_number_id}/messages`,
`media/{id}` para resolver a URL temporária. Credenciais do canal:
`{"access_token": "...", "base_url": "https://..."}`.
"""

import requests
from django.conf import settings

from apps.integrations.whatsapp.exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppNotConfiguredError,
    WhatsAppRateLimitedError,
    WhatsAppUnavailableError,
)

DEFAULT_TIMEOUT = 30  # segundos


class DatafyClient:
    def __init__(self, channel):
        credentials = channel.credentials or {}
        self.access_token = credentials.get("access_token", "")
        self.base_url = (
            credentials.get("base_url") or getattr(settings, "DATAFY_API_URL", "")
        ).rstrip("/")
        self.phone_number_id = channel.phone_number_id
        if not self.access_token or not self.base_url:
            raise WhatsAppNotConfiguredError(
                f"Canal {channel.pk}: configure access_token (e base_url, se não houver "
                "DATAFY_API_URL no ambiente) em Channel.credentials."
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )

    def post_messages(self, body: dict) -> dict:
        return self._request("POST", f"/v1/{self.phone_number_id}/messages", json=body)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params or {})

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self.session.request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise WhatsAppUnavailableError(f"Datafy inacessível: {exc}") from exc

        if response.status_code in (401, 403):
            raise WhatsAppAuthError("Token da Datafy recusado (401/403).")
        if response.status_code == 429:
            raise WhatsAppRateLimitedError("Rate limit da Datafy atingido (429).")
        if response.status_code >= 500:
            raise WhatsAppUnavailableError(f"Datafy indisponível ({response.status_code}).")
        if response.status_code >= 400:
            raise WhatsAppError(f"Datafy retornou {response.status_code}: {response.text[:300]}")

        try:
            return response.json()
        except ValueError:
            return {}
