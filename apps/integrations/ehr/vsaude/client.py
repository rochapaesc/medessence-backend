"""
Client HTTP da vSaúde - contrato OFICIAL (docs/vsaude-swagger.json,
baixado da própria API em 09/07/2026).

Convenções reais observadas + swagger:
    - Rotas em /api/services/app/<Service>/<Method>;
    - Listagens (GetAll) são POST com corpo JSON ({skipCount, maxResultCount});
    - Envelope ABP: {"result", "success", "error", "__abp", ...};
    - Get pontual é GET com query param (Id/id).
"""

import time

import requests
from django.conf import settings

from apps.integrations.ehr.exceptions import (
    EHRAuthError,
    EHRError,
    EHRNotConfiguredError,
    EHRRateLimitedError,
    EHRUnavailableError,
)

DEFAULT_TIMEOUT = 30  # segundos
PAGE_SIZE = 100
MAX_429_RETRIES = 4  # absorve rajadas (ex.: backfill de vários meses)
RETRY_AFTER_CAP = 15  # s - teto do respeito ao header Retry-After


class VSaudeClient:
    def __init__(self, clinic):
        credentials = clinic.ehr_credentials or {}
        self.api_key = credentials.get("api_key", "")
        self.base_url = (
            credentials.get("base_url") or getattr(settings, "VSAUDE_API_URL", "")
        ).rstrip("/")
        if not self.api_key or not self.base_url:
            raise EHRNotConfiguredError(
                f"Clínica {clinic.slug}: configure api_key (e base_url, se não houver "
                "VSAUDE_API_URL no ambiente) em Clinic.ehr_credentials."
            )
        self.session = requests.Session()
        self.session.headers.update({"VSAUDE-API-KEY": self.api_key, "Accept": "application/json"})

    def get(self, path: str, params: dict | None = None):
        """GET (métodos pontuais, ex.: PatientService/Get?Id=...)."""
        return self._request("GET", path, params=params or {})

    def post(self, path: str, body: dict | None = None, params: dict | None = None):
        """POST (listagens GetAll e comandos; Search usa query param)."""
        kwargs = {"json": body or {}}
        if params:
            kwargs["params"] = params
        return self._request("POST", path, **kwargs)

    def put(self, path: str, body: dict | None = None):
        """PUT (updates full-object, ex.: PatientService/Update)."""
        return self._request("PUT", path, json=body or {})

    def delete(self, path: str, params: dict | None = None):
        """DELETE (ex.: PatientService/Delete?Id=...)."""
        return self._request("DELETE", path, params=params or {})

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        """Respeita Retry-After (segundos); senão backoff exponencial, com teto."""
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), RETRY_AFTER_CAP)
            except ValueError:
                pass
        return min(2**attempt, RETRY_AFTER_CAP)

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}/{path.lstrip('/')}"
        # Rajada de 429 (backfill): recua e reтenta a MESMA request algumas
        # vezes antes de desistir - o beat cobre o que sobrar.
        for attempt in range(MAX_429_RETRIES + 1):
            try:
                response = self.session.request(
                    method, url, timeout=DEFAULT_TIMEOUT, **kwargs
                )
            except requests.RequestException as exc:
                raise EHRUnavailableError(f"vSaúde inacessível: {exc}") from exc
            if response.status_code == 429 and attempt < MAX_429_RETRIES:
                time.sleep(self._retry_delay(response, attempt))
                continue
            break

        if response.status_code in (401, 403):
            raise EHRAuthError("API key da vSaúde recusada (401/403).")
        if response.status_code == 429:
            raise EHRRateLimitedError(
                "Muitas requisições à vSaúde no momento — o restante entra na "
                "próxima sincronização automática."
            )
        if response.status_code >= 500:
            raise EHRUnavailableError(f"vSaúde indisponível ({response.status_code}).")
        if response.status_code >= 400:
            raise EHRError(f"vSaúde retornou {response.status_code}: {response.text[:300]}")

        try:
            envelope = response.json()
        except ValueError as exc:
            raise EHRError(f"Resposta não-JSON da vSaúde em {path}.") from exc

        # Envelope ABP - o campo "success" nem sempre vem; erro só se False.
        if isinstance(envelope, dict) and ("success" in envelope or "result" in envelope):
            if envelope.get("success", True) is False:
                error = envelope.get("error") or {}
                message = error.get("message", str(error)) if isinstance(error, dict) else error
                raise EHRError(f"vSaúde: {message}")
            return envelope.get("result")
        return envelope

    def post_paginated(self, path: str, body: dict | None = None):
        """Itera todas as páginas de um GetAll ({totalCount, items})."""
        skip = 0
        while True:
            page_body = {**(body or {}), "skipCount": skip, "maxResultCount": PAGE_SIZE}
            result = self.post(path, page_body) or {}
            items = result.get("items", []) if isinstance(result, dict) else result
            if not items:
                return
            yield from items
            skip += len(items)
            total = result.get("totalCount") if isinstance(result, dict) else None
            if total is not None and skip >= total:
                return
            if not isinstance(result, dict):  # resposta em lista = sem paginação
                return
