"""Jina provider contracts, exercised through Hermes' real plugin discovery."""

import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

PLUGIN = "hermes-jina"
KEY = "jina_test_key_0123456789"


def install(tmp_path, monkeypatch, web=None):
    """Install this checkout as a user plugin in a throwaway profile and discover it."""
    home = tmp_path / "profile"
    plugin_dir = home / "plugins" / PLUGIN
    plugin_dir.mkdir(parents=True)
    root = Path(__file__).resolve().parents[1]
    for filename in ("plugin.yaml", "__init__.py", "provider.py"):
        shutil.copy2(root / filename, plugin_dir / filename)
    config = {"plugins": {"enabled": [PLUGIN]}}
    if web:
        config["web"] = web
    # JSON is valid YAML; Hermes does not ship PyYAML.
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty-bundled"))
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    from hermes_cli.plugins import _ensure_plugins_discovered
    from agent.web_search_registry import get_provider

    _ensure_plugins_discovered(force=True)
    return get_provider("jina")


@pytest.fixture
def provider(tmp_path, monkeypatch):
    found = install(tmp_path, monkeypatch)
    assert found is not None, "the installed plugin must register the 'jina' provider"
    return found


@pytest.fixture
def requests(monkeypatch):
    """Route every httpx client through ``handler``; record what was sent."""
    seen = []
    state = {"handler": None}

    def dispatch(request):
        seen.append(request)
        return state["handler"](request)

    mock = httpx.MockTransport(dispatch)
    sync_client, async_client = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(httpx, "Client", lambda **kw: sync_client(transport=mock, **kw))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_client(transport=mock, **kw))

    def use(handler):
        state["handler"] = handler
        return seen

    return use


def reader_ok(request):
    url = json.loads(request.content)["url"]
    return httpx.Response(200, json={"code": 200, "status": 20000, "data": {
        "title": f"Title of {url}", "url": url, "description": "desc", "content": f"# Page\n\nBody of {url}",
    }})


def extract(provider, urls):
    return asyncio.run(provider.extract(urls))


def test_without_a_key_jina_is_never_auto_selected(provider):
    from agent.web_search_registry import get_active_extract_provider, get_active_search_provider

    assert not provider.is_available()
    assert not provider.is_keyless_available()
    assert get_active_extract_provider() is not provider
    assert get_active_search_provider() is not provider


def test_a_key_makes_jina_available(provider, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", KEY)
    assert provider.is_available()


def test_explicit_extract_backend_selects_jina_without_a_key(tmp_path, monkeypatch):
    from agent.web_search_registry import get_active_extract_provider

    found = install(tmp_path, monkeypatch, web={"extract_backend": "jina"})
    assert get_active_extract_provider() is found


def test_keyless_extract_posts_the_url_and_returns_markdown(provider, requests):
    seen = requests(reader_ok)

    [doc] = extract(provider, ["https://example.com/a"])

    assert doc["url"] == "https://example.com/a"
    assert doc["title"] == "Title of https://example.com/a"
    assert doc["content"] == doc["raw_content"] == "# Page\n\nBody of https://example.com/a"
    assert "error" not in doc
    [request] = seen
    assert request.method == "POST"
    assert str(request.url) == "https://r.jina.ai/"
    assert json.loads(request.content) == {"url": "https://example.com/a"}
    assert "authorization" not in request.headers
    assert request.headers["accept"] == "application/json"


def test_keyed_extract_sends_the_bearer_key(provider, requests, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", KEY)
    seen = requests(reader_ok)

    extract(provider, ["https://example.com/a"])

    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


def test_per_url_failures_keep_order_and_never_raise(provider, requests):
    def handler(request):
        if json.loads(request.content)["url"].endswith("/limited"):
            return httpx.Response(429, json={"code": 429, "readableMessage": "RateLimitTriggeredError: slow down"})
        return reader_ok(request)

    seen = requests(handler)
    urls = ["https://example.com/ok", "https://example.com/limited", "file:///etc/passwd", "https://example.com/ok2"]

    docs = extract(provider, urls)

    assert [d["url"] for d in docs] == urls
    assert "error" not in docs[0] and "error" not in docs[3]
    assert "HTTP 429" in docs[1]["error"] and "JINA_API_KEY" in docs[1]["error"]
    assert "slow down" in docs[1]["error"]
    assert "http" in docs[2]["error"]
    assert len(seen) == 3, "a non-web URL must not be sent to Jina"


def test_empty_page_is_reported_as_a_per_url_error(provider, requests):
    requests(lambda r: httpx.Response(200, json={"code": 200, "data": {"title": "", "url": "https://x.test/", "content": "  "}}))

    [doc] = extract(provider, ["https://x.test/"])

    assert doc["error"]
    assert doc["content"] == ""


def test_transport_failures_become_readable_errors(provider, requests):
    def handler(request):
        raise httpx.ConnectTimeout("boom", request=request)

    requests(handler)

    [doc] = extract(provider, ["https://example.com/a"])

    assert doc["error"] == "Jina request timed out. Try again later."


def test_search_without_a_key_fails_without_a_request(provider, requests):
    seen = requests(lambda r: pytest.fail("no request expected"))

    result = provider.search("hermes agent")

    assert result["success"] is False
    assert "JINA_API_KEY" in result["error"]
    assert seen == []


def test_search_maps_results_and_honours_limit(provider, requests, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", KEY)
    rows = [{"title": f"T{i}", "url": f"https://r{i}.test/", "description": f"D{i}"} for i in range(5)]
    rows.insert(1, {"title": "no url"})
    seen = requests(lambda r: httpx.Response(200, json={"code": 200, "data": rows}))

    result = provider.search("hermes agent", limit=3)

    assert result == {"success": True, "data": {"web": [
        {"title": "T0", "url": "https://r0.test/", "description": "D0", "position": 1},
        {"title": "T1", "url": "https://r1.test/", "description": "D1", "position": 2},
        {"title": "T2", "url": "https://r2.test/", "description": "D2", "position": 3},
    ]}}
    [request] = seen
    assert request.method == "GET"
    assert request.url.host == "s.jina.ai"
    assert request.url.params["q"] == "hermes agent"
    assert request.url.params["num"] == "3"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["x-respond-with"] == "no-content"


def test_search_auth_failure_names_the_key_but_never_leaks_it(provider, requests, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", KEY)
    requests(lambda r: httpx.Response(401, json={"code": 401, "message": "Invalid API key"}))

    result = provider.search("q")

    assert result["success"] is False
    assert "HTTP 401" in result["error"] and "JINA_API_KEY" in result["error"]
    assert KEY not in result["error"]


def test_search_rejects_a_malformed_envelope(provider, requests, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", KEY)
    requests(lambda r: httpx.Response(200, json={"code": 200, "data": {"not": "a list"}}))

    result = provider.search("q")

    assert result["success"] is False
