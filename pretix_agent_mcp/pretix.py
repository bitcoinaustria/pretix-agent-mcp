"""Thin async pretix REST client.

Two rules make this the only door to pretix:

* every path segment is built from values that went through :mod:`validate`, and
* there is no method that takes a caller-supplied URL or path string.

The API token lives here and only here. It goes into the ``Authorization`` header
and never into a return value, an exception message or a log record.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .validate import path_segments

# pretix error bodies are echoed back to the agent (they explain what was wrong with
# the request) but truncated — an event settings dump would otherwise flood the context.
MAX_ERROR_CHARS = 800

# Rate-limit retries. Three attempts covers a burst against pretix Hosted's per-minute
# window without turning a sustained 429 into a hung tool call.
# ponytail: fixed attempts, no jitter or circuit breaker until a real deployment needs one.
MAX_ATTEMPTS = 3
MAX_RETRY_WAIT = 30.0


class PretixError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"pretix API error {status}: {detail}")
        self.status = status
        self.detail = detail


class Pretix:
    """pretix REST client scoped to one organizer."""

    def __init__(
        self,
        base_url: str,
        token: str,
        organizer: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = f"{base_url.rstrip('/')}/api/v1"
        self._organizer = organizer
        self._client = httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Token {token}",
                "Accept": "application/json",
                "User-Agent": "pretix-agent-mcp",
            },
            follow_redirects=False,
        )

    @property
    def organizer(self) -> str:
        return self._organizer

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, segments: tuple[str, ...]) -> str:
        # Organizer-scoped by construction: the agent cannot address another organizer.
        path = path_segments("organizers", self._organizer, *segments)
        return f"{self._base}/{path}/"

    async def request(
        self,
        method: str,
        *segments: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> Any:
        url = self._url(segments)
        for attempt in range(MAX_ATTEMPTS):
            try:
                response = await self._client.request(method, url, params=params, json=json)
            except httpx.HTTPError as exc:
                # Never interpolate the client (its headers hold the token) into the message.
                raise PretixError(0, f"could not reach pretix: {type(exc).__name__}") from None
            if response.status_code != 429 or attempt == MAX_ATTEMPTS - 1:
                break
            # pretix Hosted rate-limits to 360 requests/minute per organizer and documents
            # a 429 as safe to retry: the request was refused, not processed. Self-hosted
            # instances do not rate-limit by default, so this path stays dormant there.
            await asyncio.sleep(_retry_after(response))
        if response.status_code >= 400:
            raise PretixError(response.status_code, _detail(response))
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise PretixError(response.status_code, "pretix returned a non-JSON body") from None

    async def get(self, *segments: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", *segments, params=params)

    async def post(self, *segments: str, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        return await self.request("POST", *segments, json=json, params=params)

    async def patch(self, *segments: str, json: Any = None) -> Any:
        return await self.request("PATCH", *segments, json=json)

    async def delete(self, *segments: str) -> Any:
        return await self.request("DELETE", *segments)

    async def paginate(
        self,
        *segments: str,
        params: dict[str, Any] | None = None,
        cap: int = 500,
        page_size: int = 100,
    ) -> tuple[list[Any], int | None, bool]:
        """Collect up to ``cap`` results.

        Returns ``(results, total_count, truncated)``. ``truncated`` is True when the
        cap stopped the scan — callers report it so a number is never silently partial.
        """
        collected: list[Any] = []
        page = 1
        total: int | None = None
        query = dict(params or {})
        query["page_size"] = min(page_size, max(cap, 1))
        while len(collected) < cap:
            query["page"] = page
            payload = await self.get(*segments, params=query)
            if not isinstance(payload, dict):
                break
            if total is None and isinstance(payload.get("count"), int):
                total = payload["count"]
            results = payload.get("results") or []
            collected.extend(results)
            if not payload.get("next") or not results:
                return collected[:cap], total, False
            page += 1
        return collected[:cap], total, True


def _retry_after(response: httpx.Response) -> float:
    """Seconds to wait, from the ``Retry-After`` header pretix sends with a 429."""
    try:
        wait = float(response.headers.get("retry-after", ""))
    except ValueError:
        wait = 1.0
    return min(max(wait, 0.0), MAX_RETRY_WAIT)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    text = body if isinstance(body, str) else _stringify(body)
    text = " ".join(text.split())
    return text[:MAX_ERROR_CHARS] or response.reason_phrase


def _stringify(body: Any) -> str:
    import json as _json

    try:
        return _json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(body)
