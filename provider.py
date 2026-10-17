"""Jina Reader as a Hermes web backend.

- ``extract``: ``POST https://r.jina.ai/`` with ``{"url": ...}``, one request per URL,
  run concurrently. Works without a key at Jina's anonymous rate limit.
- ``search``: ``GET https://s.jina.ai/?q=...``. Jina requires an API key for search.

Env: ``JINA_API_KEY`` (https://jina.ai/api-dashboard/), read through Hermes' secret scope.

``is_available()`` is True only with a key, so a fresh install never auto-selects Jina.
Keyless extract is opt-in: set ``web.extract_backend: jina`` (explicit config is honoured
even when ``is_available()`` is False).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent.secret_scope import get_secret
from agent.web_search_provider import WebSearchProvider

logger = logging.getLogger(__name__)

KEY_ENV = "JINA_API_KEY"
KEY_URL = "https://jina.ai/api-dashboard/"
READER_URL = "https://r.jina.ai/"
SEARCH_URL = "https://s.jina.ai/"

TIMEOUT = 60.0
SEARCH_LIMIT_MAX = 20
# Anonymous Reader traffic is limited to roughly 20 requests a minute; stay well under it.
CONCURRENCY_KEYLESS = 2
CONCURRENCY_KEYED = 5
_MESSAGE_MAX = 300

_BASE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "hermes-jina/0.1.0",
}

MISSING_SEARCH_KEY = (
    f"Jina search requires {KEY_ENV}. Get a key at {KEY_URL} and set it with `hermes tools`. "
    "Extract works without a key."
)


class JinaError(Exception):
    """A failure whose message is safe to hand to the model."""


def _api_key() -> str:
    return (get_secret(KEY_ENV, "") or "").strip()


def _headers(api_key: str, **extra: str) -> dict[str, str]:
    headers = {**_BASE_HEADERS, **extra}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _jina_message(response: httpx.Response) -> str:
    """Jina's own error text (it never contains the request's credentials), shortened."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    message = body.get("readableMessage") or body.get("message") or ""
    return str(message).strip()[:_MESSAGE_MAX]


def _status_error(response: httpx.Response, keyed: bool) -> JinaError:
    code = response.status_code
    if code == 401:
        hint = f"Check {KEY_ENV}." if keyed else f"This Jina endpoint needs {KEY_ENV} ({KEY_URL})."
    elif code == 402:
        hint = "The Jina account is out of balance; top up at https://jina.ai/api-dashboard/."
    elif code == 429:
        hint = "Rate limit reached; try again later." if keyed else (
            f"Anonymous rate limit reached; set {KEY_ENV} for a higher limit."
        )
    elif code >= 500:
        hint = "Try again later or check https://status.jina.ai/."
    else:
        hint = ""
    detail = _jina_message(response)
    return JinaError(" ".join(part for part in (f"Jina returned HTTP {code}.", hint, detail) if part))


def _transport_error(exc: Exception) -> JinaError:
    if isinstance(exc, httpx.TimeoutException):
        return JinaError("Jina request timed out. Try again later.")
    if isinstance(exc, httpx.RequestError):
        return JinaError("Could not reach Jina. Check your network connection.")
    return JinaError("Jina returned an invalid response.")


def _payload(response: httpx.Response, keyed: bool) -> Any:
    """``data`` from a Jina JSON envelope, or raise :class:`JinaError`."""
    if response.status_code >= 400:
        raise _status_error(response, keyed)
    try:
        body = response.json()
    except ValueError as exc:
        raise JinaError("Jina returned a response that is not JSON.") from exc
    if not isinstance(body, dict):
        raise JinaError("Jina returned an invalid response.")
    if body.get("code") not in (None, 200):
        message = str(body.get("readableMessage") or body.get("message") or "")[:_MESSAGE_MAX]
        raise JinaError(f"Jina reported an error (code {body.get('code')}). {message}".strip())
    return body.get("data")


def _is_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _failed(url: str, error: str) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "raw_content": "", "error": error, "metadata": {"sourceURL": url}}


def _document(url: str, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _failed(url, "Jina returned an invalid response.")
    content = data.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        return _failed(url, "Jina returned no content for this page.")
    final_url = data.get("url") or url
    title = data.get("title") or ""
    metadata: dict[str, Any] = {"sourceURL": final_url, "title": title}
    if data.get("description"):
        metadata["description"] = data["description"]
    if data.get("warning"):
        metadata["warning"] = data["warning"]
    return {"url": final_url, "title": title, "content": content, "raw_content": content, "metadata": metadata}


class JinaWebSearchProvider(WebSearchProvider):
    """Jina Reader search + extract."""

    @property
    def name(self) -> str:
        return "jina"

    @property
    def display_name(self) -> str:
        return "Jina Reader"

    def is_available(self) -> bool:
        return bool(_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": "Jina Reader",
            "badge": "free · key optional",
            "tag": "Extract works without a key (rate-limited); search needs JINA_API_KEY.",
            "env_vars": [{
                "key": KEY_ENV,
                "prompt": "Jina API key (optional for extract, required for search)",
                "url": KEY_URL,
            }],
        }

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        api_key = _api_key()
        if not api_key:
            return {"success": False, "error": MISSING_SEARCH_KEY}
        limit = max(1, min(int(limit or 5), SEARCH_LIMIT_MAX))
        logger.info("Jina search (limit=%d)", limit)
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                response = client.get(
                    SEARCH_URL,
                    params={"q": query, "num": limit},
                    # Titles, URLs and snippets only: full page content costs tokens and time.
                    headers=_headers(api_key, **{"X-Respond-With": "no-content"}),
                )
            rows = _payload(response, keyed=True)
        except JinaError as exc:
            return {"success": False, "error": str(exc)}
        except httpx.HTTPError as exc:
            return {"success": False, "error": str(_transport_error(exc))}
        if not isinstance(rows, list):
            return {"success": False, "error": "Jina returned an invalid search response."}
        hits = [row for row in rows if isinstance(row, dict) and row.get("url")][:limit]
        return {"success": True, "data": {"web": [
            {
                "title": row.get("title") or "",
                "url": row["url"],
                "description": row.get("description") or "",
                "position": i + 1,
            }
            for i, row in enumerate(hits)
        ]}}

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        api_key = _api_key()
        keyed = bool(api_key)
        logger.info("Jina extract: %d URL(s)%s", len(urls), "" if keyed else " (keyless)")
        semaphore = asyncio.Semaphore(CONCURRENCY_KEYED if keyed else CONCURRENCY_KEYLESS)
        headers = _headers(api_key, **{"X-Retain-Images": "none"})

        async def one(client: httpx.AsyncClient, url: str) -> dict[str, Any]:
            if not _is_web_url(url):
                return _failed(url, "Only http:// and https:// URLs can be extracted.")
            async with semaphore:
                try:
                    response = await client.post(READER_URL, json={"url": url}, headers=headers)
                    return _document(url, _payload(response, keyed))
                except JinaError as exc:
                    return _failed(url, str(exc))
                except httpx.HTTPError as exc:
                    return _failed(url, str(_transport_error(exc)))

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            return list(await asyncio.gather(*(one(client, url) for url in urls)))
