from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
from box_sdk_gen.internal.utils import ResponseByteStream
from tenacity import retry, stop_after_attempt, wait_exponential

from suralink_to_box.settings import Settings


@dataclass
class DownloadedFile:
    filename: str
    content_type: str
    data: bytes


@dataclass
class StreamedDownloadedFile:
    filename: str
    content_type: str
    content_length: int | None
    stream: ResponseByteStream


class SuralinkClient:
    """
    Confirmed Suralink endpoints:
    - list engagements: GET /v1/engagements
    - list requests for engagement: GET /v1/engagements/{engagementId}/request-item
    - list files for engagement: GET /v1/files/engagement/{engagementId}
    - download engagement file: GET /v1/files/engagement?auditId=...&requestId=...&fileId=...
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.base_url = settings.suralink_base_url.rstrip("/")
        self.timeout = float(settings.http_timeout_seconds)

        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "x-acs-token": settings.suralink_token,
                "Accept": "*/*",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _normalize_path(self, path: str) -> str:
        url = path
        if url.startswith(self.base_url):
            url = url[len(self.base_url):]
        if not url.startswith("/"):
            url = "/" + url
        return url

    def _unwrap_list_response(self, payload):
        """
        Handles common API shapes:
        - {"data": [...]} 
        - {"items": [...]} 
        - [...]
        """
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
            items = payload.get("items")
            if isinstance(items, list):
                return items
        return []

    def get_json(self, path: str):
        url = self._normalize_path(path)
        resp = self._client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()

    def _browser_access_token(self) -> str:
        token = (getattr(self.settings, "suralink_browser_access_token", None) or "").strip()
        if token.startswith("Bearer "):
            token = token.removeprefix("Bearer ").strip()
        return token

    def _browser_jwt_claim(self, *names: str) -> str:
        token = self._browser_access_token()
        if not token:
            return ""
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except Exception:
            return ""
        for name in names:
            value = claims.get(name)
            if value not in (None, ""):
                return str(value)
        return ""

    def _v2_query_context(self) -> dict[str, str]:
        organization_id = (
            (getattr(self.settings, "suralink_organization_id", None) or "").strip()
            or self._browser_jwt_claim("organizationId", "orgId", "organization_id")
        )
        request_list_id = (getattr(self.settings, "suralink_request_list_id", None) or "").strip()

        params: dict[str, str] = {}
        if organization_id:
            params["organizationId"] = organization_id
        if request_list_id:
            params["requestListId"] = request_list_id
        return params

    def get_v2_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        include_context: bool = True,
    ):
        token = self._browser_access_token()
        if not token:
            raise ValueError(
                "Missing SURALINK_BROWSER_ACCESS_TOKEN. "
                "Suralink Activity comments use v2 endpoints that require the browser access_token cookie."
            )

        merged_params = self._v2_query_context() if include_context else {}
        if params:
            merged_params.update(params)

        headers = {
            "Authorization": f"Bearer {token}",
            "Cookie": f"access_token={token}",
            "Accept": "application/json",
        }
        url = self._normalize_path(path)
        resp = self._client.get(
            url,
            params=merged_params,
            headers=headers,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return resp.json()

    def list_request_history(
        self,
        request_item_id: str,
        *,
        request_list_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ):
        params = {"limit": str(limit), "offset": str(offset)}
        if request_list_id:
            params["requestListId"] = str(request_list_id)

        payload = self.get_v2_json(
            f"/v2/request/{request_item_id}/history",
            params=params,
        )
        if isinstance(payload, dict):
            events = payload.get("events")
            if isinstance(events, list):
                return events
        return self._unwrap_list_response(payload)

    def probe(self, path: str) -> tuple[int, str, str]:
        """
        Helpful for debugging unknown endpoints.
        Returns: (status_code, content_type, body_snippet)
        """
        url = self._normalize_path(path)
        resp = self._client.get(url, follow_redirects=True)
        ct = resp.headers.get("content-type", "")
        snippet = resp.text[:300] if ("text" in ct or "json" in ct or ct == "") else "<non-text response>"
        return resp.status_code, ct, snippet

    def list_engagements(self):
        payload = self.get_json("/v1/engagements")
        return self._unwrap_list_response(payload)

    def list_engagements_page(self, *, limit: int = 100, offset: int = 0):
        payload = self.get_json(f"/v1/engagements?limit={limit}&offset={offset}")
        return payload

    def list_all_engagements(self, *, page_size: int = 100) -> list[dict]:
        engagements: list[dict] = []
        offset = 0

        while True:
            payload = self.list_engagements_page(limit=page_size, offset=offset)
            batch = self._unwrap_list_response(payload)
            if not batch:
                break

            engagements.extend(batch)

            next_page = payload.get("nextPage") if isinstance(payload, dict) else None
            if not next_page:
                break

            parsed = urlparse(str(next_page))
            query = parse_qs(parsed.query)
            next_offset = query.get("offset")
            if not next_offset:
                break

            try:
                offset = int(next_offset[0])
            except (TypeError, ValueError):
                break

        return engagements

    def list_engagements_by_custom_id(self, custom_id: str):
        payload = self.get_json(f"/v1/engagements/customId/{custom_id}")
        rows = self._unwrap_list_response(payload)
        if rows:
            return rows
        if isinstance(payload, dict) and payload.get("id") is not None:
            return [payload]
        return []

    def list_clients(self, *, limit: int = 100, offset: int = 0):
        payload = self.get_json(f"/v1/clients?limit={limit}&offset={offset}")
        return payload

    def list_all_clients(self, *, page_size: int = 100) -> list[dict]:
        clients: list[dict] = []
        offset = 0

        while True:
            payload = self.list_clients(limit=page_size, offset=offset)
            batch = self._unwrap_list_response(payload)
            if not batch:
                break

            clients.extend(batch)

            next_page = payload.get("nextPage") if isinstance(payload, dict) else None
            if not next_page:
                break

            parsed = urlparse(str(next_page))
            query = parse_qs(parsed.query)
            next_offset = query.get("offset")
            if not next_offset:
                break

            try:
                offset = int(next_offset[0])
            except (TypeError, ValueError):
                break

        return clients

    def list_client_engagements(self, client_id: str):
        payload = self.get_json(f"/v1/clients/{client_id}/engagements")
        return self._unwrap_list_response(payload)

    def list_requests(self, engagement_id: str, *, limit: int = 50, offset: int = 0):
        payload = self.get_json(
            f"/v1/engagements/{engagement_id}/request-item?limit={limit}&offset={offset}"
        )
        return self._unwrap_list_response(payload)

    def get_request_item_detail(self, engagement_id: str, request_id: str):
        return self.get_json(f"/v1/engagements/{engagement_id}/request-item/{request_id}")

    def list_engagement_files(self, engagement_id: str, *, limit: int = 50, offset: int = 0):
        payload = self.get_json(
            f"/v1/files/engagement/{engagement_id}?limit={limit}&offset={offset}"
        )
        return self._unwrap_list_response(payload)

    def _guess_filename(self, response: httpx.Response, fallback: str) -> str:
        cd = response.headers.get("content-disposition", "")
        if "filename=" in cd:
            part = cd.split("filename=", 1)[1].strip()
            return part.strip('"').strip("'")
        return fallback

    @contextmanager
    def stream_engagement_file(
        self,
        *,
        audit_id: str,
        request_id: str,
        file_id: str,
        fallback_filename: str = "document.bin",
    ):
        url = self._normalize_path(
            f"/v1/files/engagement?auditId={audit_id}&requestId={request_id}&fileId={file_id}"
        )
        with self._client.stream("GET", url, follow_redirects=True) as resp:
            if resp.status_code in (403, 404):
                ct = resp.headers.get("content-type", "")
                snippet = (
                    resp.read()[:500].decode("utf-8", errors="replace")
                    if ("text" in ct or "json" in ct or ct == "")
                    else "<non-text response>"
                )
                raise httpx.HTTPStatusError(
                    message=(
                        f"HTTP {resp.status_code} for {resp.request.url}. "
                        f"Content-Type={ct}. Body starts with: {snippet}"
                    ),
                    request=resp.request,
                    response=resp,
                )

            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "application/octet-stream")
            if "application/json" in content_type.lower():
                snippet = resp.read()[:500].decode("utf-8", errors="replace")
                raise ValueError(
                    "Download endpoint returned JSON, not a file. "
                    f"Body starts with: {snippet}"
                )

            content_length_header = resp.headers.get("content-length")
            try:
                content_length = int(content_length_header) if content_length_header else None
            except ValueError:
                content_length = None

            yield StreamedDownloadedFile(
                filename=self._guess_filename(resp, fallback_filename),
                content_type=content_type,
                content_length=content_length,
                stream=ResponseByteStream(resp.iter_bytes()),
            )

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    def download_engagement_file(
        self,
        *,
        audit_id: str,
        request_id: str,
        file_id: str,
        fallback_filename: str = "document.bin",
    ) -> DownloadedFile:
        url = self._normalize_path(
            f"/v1/files/engagement?auditId={audit_id}&requestId={request_id}&fileId={file_id}"
        )
        resp = self._client.get(url, follow_redirects=True)

        if resp.status_code in (403, 404):
            ct = resp.headers.get("content-type", "")
            snippet = resp.text[:500] if ("text" in ct or "json" in ct or ct == "") else "<non-text response>"
            raise httpx.HTTPStatusError(
                message=(
                    f"HTTP {resp.status_code} for {resp.request.url}. "
                    f"Content-Type={ct}. Body starts with: {snippet}"
                ),
                request=resp.request,
                response=resp,
            )

        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "application/octet-stream")
        if "application/json" in content_type.lower():
            snippet = resp.text[:500]
            raise ValueError(
                "Download endpoint returned JSON, not a file. "
                f"Body starts with: {snippet}"
            )

        filename = self._guess_filename(resp, fallback_filename)
        return DownloadedFile(
            filename=filename,
            content_type=content_type,
            data=resp.content,
        )

    @retry(wait=wait_exponential(min=1, max=10), stop=stop_after_attempt(3), reraise=True)
    def download_file_by_id(self, file_id: str, *, fallback_filename: str = "document.bin") -> DownloadedFile:
        """
        Kept for compatibility/testing in case your tenant also supports GET /v1/files/{id}.
        """
        url = self._normalize_path(f"/v1/files/{file_id}")
        resp = self._client.get(url, follow_redirects=True)

        if resp.status_code in (403, 404):
            ct = resp.headers.get("content-type", "")
            snippet = resp.text[:500] if ("text" in ct or "json" in ct or ct == "") else "<non-text response>"
            raise httpx.HTTPStatusError(
                message=(
                    f"HTTP {resp.status_code} for {resp.request.url}. "
                    f"Content-Type={ct}. Body starts with: {snippet}"
                ),
                request=resp.request,
                response=resp,
            )

        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "application/octet-stream")
        if "application/json" in content_type.lower():
            snippet = resp.text[:500]
            raise ValueError(
                "Download endpoint returned JSON, not a file. "
                f"Body starts with: {snippet}"
            )

        filename = self._guess_filename(resp, fallback_filename)
        return DownloadedFile(
            filename=filename,
            content_type=content_type,
            data=resp.content,
        )
