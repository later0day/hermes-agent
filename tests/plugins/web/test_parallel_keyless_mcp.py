"""Keyless Parallel search/extract through the shared MCP transport and failover ring.

The implementation lives in :mod:`plugins.web.keyless_mcp`; the Parallel provider
only chooses keyless versus keyed dispatch and owns the current Beta SDK path.
All HTTP transports are mocked.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from plugins.web import keyless_mcp as km
from plugins.web.parallel import provider as pp


def _response(body: str, *, status: int = 200, content_type: str = "application/json") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response._content = body.encode()
    return response


def _envelope(payload: dict) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    })


class TestMcpTransport:
    def test_posts_single_anonymous_tools_call(self):
        body = _envelope({"results": []})
        with patch("requests.post", return_value=_response(body)) as post:
            text = km.mcp_call(km.PARALLEL_MCP_URL, "web_search", {"objective": "q"}, timeout=9)

        assert json.loads(text) == {"results": []}
        post.assert_called_once()
        args, kwargs = post.call_args
        assert args == (km.PARALLEL_MCP_URL,)
        assert kwargs["timeout"] == 9
        assert kwargs["json"] == {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "web_search", "arguments": {"objective": "q"}},
        }
        assert kwargs["headers"]["Accept"] == "application/json, text/event-stream"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        assert kwargs["headers"]["User-Agent"] == "hermes-agent"
        assert "Authorization" not in kwargs["headers"]
        assert "Mcp-Session-Id" not in kwargs["headers"]

    def test_plain_json_text_content(self):
        body = json.dumps({"result": {"content": [{"type": "text", "text": "hello"}]}})
        assert km._parse_mcp_body(body) == "hello"

    def test_sse_skips_notifications_and_returns_result_text(self):
        body = (
            'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
            'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":'
            '{"content":[{"type":"text","text":"from SSE"}]}}\n\n'
        )
        assert km._parse_mcp_body(body) == "from SSE"

    def test_jsonrpc_error_raises(self):
        body = json.dumps({"error": {"code": -32000, "message": "rate limit"}})
        with pytest.raises(km.KeylessMCPError, match="rate limit"):
            km._parse_mcp_body(body)

    def test_tool_error_raises_with_vendor_text(self):
        body = json.dumps({
            "result": {"isError": True, "content": [{"type": "text", "text": "quota exceeded"}]},
        })
        with pytest.raises(km.KeylessMCPError, match="quota exceeded"):
            km._parse_mcp_body(body)

    def test_tool_error_without_text_has_explicit_fallback(self):
        with pytest.raises(km.KeylessMCPError, match="MCP tool call failed"):
            km._parse_mcp_body('{"result":{"isError":true,"content":[]}}')

    def test_envelope_without_text_is_an_error(self):
        with pytest.raises(km.KeylessMCPError, match="no text content"):
            km._parse_mcp_body('{"result":{"content":[]}}')

    def test_unrecognized_body_is_an_error(self):
        with pytest.raises(km.KeylessMCPError, match="Unrecognized MCP response shape"):
            km._parse_mcp_body("<html>not MCP</html>")

    def test_http_error_preserves_status_and_body(self):
        with patch("requests.post", return_value=_response("quota exceeded", status=429)):
            with pytest.raises(km.KeylessMCPError, match="HTTP 429: quota exceeded"):
                km.mcp_call(km.PARALLEL_MCP_URL, "web_search", {})

    def test_request_failure_is_wrapped(self):
        with patch("requests.post", side_effect=requests.ConnectionError("offline")):
            with pytest.raises(km.KeylessMCPError, match="request failed: offline"):
                km.mcp_call(km.PARALLEL_MCP_URL, "web_search", {})


class TestParallelKeylessSearch:
    @staticmethod
    def _payload(count: int) -> str:
        return json.dumps({"results": [
            {"url": f"https://example.test/{i}", "title": f"Title {i}", "excerpts": [f"a{i}", f"b{i}"]}
            for i in range(count)
        ]})

    def test_maps_standard_results_and_request_arguments(self):
        with patch.object(km, "mcp_call", return_value=self._payload(2)) as call:
            out = km.parallel_search_keyless("kittens", limit=5)

        assert out == {
            "success": True,
            "data": {"web": [
                {"url": "https://example.test/0", "title": "Title 0", "description": "a0 b0", "position": 1},
                {"url": "https://example.test/1", "title": "Title 1", "description": "a1 b1", "position": 2},
            ]},
        }
        call.assert_called_once_with(km.PARALLEL_MCP_URL, "web_search", {
            "objective": "kittens",
            "search_queries": ["kittens"],
            "session_id": km._SESSION_ID,
        })
        arguments = call.call_args.args[2]
        assert "model_name" not in arguments  # optional analytics are deliberately omitted
        assert "api_key" not in arguments

    def test_limit_is_applied_client_side(self):
        with patch.object(km, "mcp_call", return_value=self._payload(10)):
            out = km.parallel_search_keyless("q", limit=2)
        assert len(out["data"]["web"]) == 2
        assert [row["position"] for row in out["data"]["web"]] == [1, 2]

    def test_zero_limit_returns_the_full_vendor_batch(self):
        with patch.object(km, "mcp_call", return_value=self._payload(3)):
            out = km.parallel_search_keyless("q", limit=0)
        assert len(out["data"]["web"]) == 3

    def test_transport_failure_is_soft_error_with_key_setup_hint(self):
        with patch.object(km, "mcp_call", side_effect=km.KeylessMCPError("rate limit")):
            out = km.parallel_search_keyless("q")
        assert out["success"] is False
        assert "Keyless Parallel search failed: rate limit" in out["error"]
        assert "PARALLEL_API_KEY" in out["error"]
        assert "hermes tools" in out["error"]

    def test_malformed_vendor_payload_is_soft_error(self):
        with patch.object(km, "mcp_call", return_value="not-json"):
            out = km.parallel_search_keyless("q")
        assert out["success"] is False
        assert "unexpected payload" in out["error"]


class TestParallelKeylessExtract:
    def test_maps_documents_and_fetch_arguments(self):
        payload = json.dumps({"results": [
            {"url": "https://a.test", "title": "A", "excerpts": ["chunk-a", "chunk-b"]},
            {"url": "https://b.test", "title": "B", "full_content": "entire page", "excerpts": ["ignored"]},
        ]})
        urls = ["https://a.test", "https://b.test"]
        with patch.object(km, "mcp_call", return_value=payload) as call:
            out = km.parallel_extract_keyless(urls)

        assert out[0] == {
            "url": "https://a.test",
            "title": "A",
            "content": "chunk-a\n\nchunk-b",
            "raw_content": "chunk-a\n\nchunk-b",
            "metadata": {"sourceURL": "https://a.test", "title": "A"},
        }
        assert out[1]["content"] == "entire page"
        call.assert_called_once_with(km.PARALLEL_MCP_URL, "web_fetch", {
            "urls": urls,
            "objective": "Full page content",
            "session_id": km._SESSION_ID,
        })

    def test_missing_url_becomes_error_entry(self):
        payload = json.dumps({"results": [{"url": "https://a.test", "title": "A", "excerpts": ["body"]}]})
        with patch.object(km, "mcp_call", return_value=payload):
            out = km.parallel_extract_keyless(["https://a.test", "https://missing.test"])
        missing = next(row for row in out if row["url"] == "https://missing.test")
        assert missing["error"] == "no content returned"
        assert missing["content"] == ""

    def test_vendor_origin_error_preserves_status_and_body(self):
        payload = json.dumps({"errors": [{
            "url": "https://blocked.test",
            "http_status_code": 403,
            "error_type": "http_error",
            "content": "access denied",
        }]})
        with patch.object(km, "mcp_call", return_value=payload):
            out = km.parallel_extract_keyless(["https://blocked.test"])
        assert out[0]["error"] == "HTTP 403 from origin"
        assert out[0]["content"] == "access denied"
        assert out[0]["metadata"] == {"sourceURL": "https://blocked.test"}

    def test_transport_failure_returns_one_error_per_requested_url(self):
        urls = ["https://a.test", "https://b.test"]
        with patch.object(km, "mcp_call", side_effect=km.KeylessMCPError("HTTP 503: unavailable")):
            out = km.parallel_extract_keyless(urls)
        assert [row["url"] for row in out] == urls
        assert all("HTTP 503: unavailable" in row["error"] for row in out)
        assert all("PARALLEL_API_KEY" in row["error"] for row in out)


class TestProviderRouting:
    def test_search_without_key_uses_shared_failover(self, monkeypatch):
        monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda _name: "")
        monkeypatch.setattr(km, "provider_tier", lambda _name: "auto")
        monkeypatch.setattr(km, "keyless_enabled", lambda: True)
        captured = {}

        def _keyless(display, name, query, limit, logger):
            captured.update(display=display, name=name, query=query, limit=limit, logger=logger)
            return {"success": True, "data": {"web": []}}

        monkeypatch.setattr(pp, "keyless_search", _keyless)
        out = pp.ParallelWebSearchProvider().search("kittens", limit=4)
        assert out["success"] is True
        assert captured == {
            "display": "Parallel", "name": "parallel", "query": "kittens", "limit": 4, "logger": pp.logger,
        }

    def test_keyed_search_uses_beta_sdk_and_skips_keyless(self, monkeypatch):
        monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda _name: "pk-live")
        monkeypatch.setattr(km, "provider_tier", lambda _name: "auto")
        monkeypatch.delenv("PARALLEL_SEARCH_MODE", raising=False)
        captured = {}
        client = SimpleNamespace()
        client.beta = SimpleNamespace(search=lambda **kwargs: captured.update(kwargs) or SimpleNamespace(results=[
            SimpleNamespace(url="https://r.test", title="R", excerpts=["x", "y"]),
        ]))
        monkeypatch.setattr(pp, "_get_sync_client", lambda: client)
        with patch.object(pp, "keyless_search") as keyless:
            out = pp.ParallelWebSearchProvider().search("q", limit=7)

        keyless.assert_not_called()
        assert captured == {
            "search_queries": ["q"], "objective": "q", "mode": "agentic", "max_results": 7,
        }
        assert out == {
            "success": True,
            "data": {"web": [{"url": "https://r.test", "title": "R", "description": "x y", "position": 1}]},
        }

    def test_sdk_result_limit_is_capped(self, monkeypatch):
        monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda _name: "pk-live")
        captured = {}
        client = SimpleNamespace(beta=SimpleNamespace(
            search=lambda **kwargs: captured.update(kwargs) or SimpleNamespace(results=[]),
        ))
        monkeypatch.setattr(pp, "_get_sync_client", lambda: client)
        pp.ParallelWebSearchProvider().search("q", limit=100)
        assert captured["max_results"] == pp.SEARCH_LIMIT_CAP

    def test_is_available_reflects_only_the_key(self, monkeypatch):
        values = {"PARALLEL_API_KEY": ""}
        monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda name: values[name])
        provider = pp.ParallelWebSearchProvider()
        assert provider.is_available() is False
        values["PARALLEL_API_KEY"] = "pk-live"
        assert provider.is_available() is True

    @pytest.mark.asyncio
    async def test_extract_without_key_uses_shared_failover_off_event_loop(self, monkeypatch):
        monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda _name: "")
        monkeypatch.setattr(km, "provider_tier", lambda _name: "auto")
        monkeypatch.setattr(km, "keyless_enabled", lambda: True)
        captured = {}

        def _keyless(display, name, urls, logger):
            captured.update(display=display, name=name, urls=urls, logger=logger)
            return [{"url": urls[0], "title": "", "content": "x"}]

        monkeypatch.setattr(pp, "keyless_extract", _keyless)
        out = await pp.ParallelWebSearchProvider().extract(["https://x.test"])
        assert out[0]["content"] == "x"
        assert captured == {
            "display": "Parallel", "name": "parallel", "urls": ["https://x.test"], "logger": pp.logger,
        }

    @pytest.mark.asyncio
    async def test_keyed_extract_uses_async_beta_sdk(self, monkeypatch):
        monkeypatch.setattr("agent.web_search_provider.get_provider_env", lambda _name: "pk-live")
        captured = {}

        class _Beta:
            async def extract(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(results=[
                    SimpleNamespace(url="https://a.test", title="A", full_content="body", excerpts=[]),
                ], errors=[])

        monkeypatch.setattr(pp, "_get_async_client", lambda: SimpleNamespace(beta=_Beta()))
        with patch.object(pp, "keyless_extract") as keyless:
            out = await pp.ParallelWebSearchProvider().extract(["https://a.test"])
        keyless.assert_not_called()
        assert captured == {"urls": ["https://a.test"], "full_content": True}
        assert out[0]["content"] == "body"


class TestResolveSearchMode:
    @pytest.mark.parametrize("env,expected", [
        (None, "agentic"),
        ("agentic", "agentic"),
        ("fast", "fast"),
        ("one-shot", "one-shot"),
        ("garbage", "agentic"),
        ("FAST", "fast"),
        (" one-shot ", "one-shot"),
    ])
    def test_mode_mapping(self, monkeypatch, env, expected):
        if env is None:
            monkeypatch.delenv("PARALLEL_SEARCH_MODE", raising=False)
        else:
            monkeypatch.setenv("PARALLEL_SEARCH_MODE", env)
        assert pp._resolve_search_mode() == expected


class TestParallelFailover:
    def test_rate_limit_advances_to_next_ring_vendor(self, monkeypatch):
        monkeypatch.setattr(km, "_vendor_pinned", lambda name: name == "parallel")
        calls = []

        def _parallel(query, limit):
            calls.append(("parallel", query, limit))
            return {"success": False, "error": "Keyless Parallel search failed: HTTP 429: rate limit"}

        def _firecrawl(query, limit):
            calls.append(("firecrawl", query, limit))
            return {"success": True, "data": {"web": []}}

        def _unexpected(name):
            return lambda *_args: (_ for _ in ()).throw(AssertionError(f"unexpected call to {name}"))

        searchers = {name: _unexpected(name) for name in km._KEYLESS_RING}
        searchers.update(parallel=_parallel, firecrawl=_firecrawl)
        with patch.dict(km._KEYLESS_SEARCHERS, searchers, clear=True):
            out = km.search_with_failover("parallel", "q", 3)
        assert calls == [("parallel", "q", 3), ("firecrawl", "q", 3)]
        assert out["success"] is True
        assert out["data"]["served_by"] == "firecrawl"

    def test_terminal_query_error_does_not_fail_over(self, monkeypatch):
        monkeypatch.setattr(km, "_vendor_pinned", lambda name: name == "parallel")

        def _unexpected(*_args):
            raise AssertionError("terminal errors must stop the ring")

        searchers = {name: _unexpected for name in km._KEYLESS_RING}
        searchers["parallel"] = lambda *_args: {"success": False, "error": "HTTP 400: invalid query"}
        with patch.dict(km._KEYLESS_SEARCHERS, searchers, clear=True):
            out = km.search_with_failover("parallel", "q", 3)
        assert out == {"success": False, "error": "HTTP 400: invalid query"}
