"""Real CLI/provider coverage for profile behaviors introduced by upstream merge 0965ced2a6.

Only the vendor boundary is fake: loopback Chat Completions servers, a real plugin tool, or the
schema-validating ACP subprocess.  In particular, the max-token cases record kwargs inside an
``ACPClient`` subclass and then delegate to the real client, so the auxiliary request still spawns
an agent and completes ACP JSON-RPC over stdio.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from tests.e2e.core.providers._native_helpers import make_home, run_chat, wait_until
from tests.e2e.core.providers._openai_helpers import bounded_turn, chat_messages, oneshot
from tests.e2e.core.terminal._pty import PtyHermes
from tests.fakes.providers import fake_acp_agent as acp
from tests.fakes.providers.chat_variants import CError, CText, CTools, FakeChatVariantServer

pytestmark = [
    pytest.mark.platforms("linux"),
    pytest.mark.live_system_guard_bypass,
]

ANSWER = "PROFILE-BOUNDARY-ANSWER"
PROMPT = "Answer with the profile boundary marker."


def _install_model_provider(hermes_home: Path, name: str, source: str) -> None:
    plugin = hermes_home / "plugins" / "model-providers" / name
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\nkind: model-provider\nversion: 0.0.1\ndescription: E2E profile boundary probe\n",
        encoding="utf-8",
    )


def _custom_model(base_url: str, **extra):
    return {
        "provider": "custom",
        "base_url": base_url,
        "default": "fake-model",
        "context_length": 128000,
        **extra,
    }


@pytest.mark.parametrize("mode", ["chat-query", "top-level-z"])
def test_oneshot_with_titles_enabled_makes_only_the_main_request(tmp_path, mode: str) -> None:
    """Neither finite CLI spelling spends a second model call on an invisible session title."""
    with FakeChatVariantServer([CText(ANSWER)]) as provider:
        nh = make_home(
            tmp_path,
            _custom_model(provider.base_url),
            env_file={"OPENAI_API_KEY": "sk-fake"},
            extra_config={"auxiliary": {"title_generation": {"enabled": True}}},
        )
        if mode == "chat-query":
            result = run_chat(nh, PROMPT)
            returncode, stdout, description = result.returncode, result.stdout, result.describe()
        else:
            result = oneshot(nh, PROMPT)
            returncode, stdout, description = result.proc.returncode, result.stdout, result.describe()

        assert returncode == 0 and ANSWER in stdout, description
        assert len(provider.main_requests()) == 1, provider.requests
        # FakeChatVariantServer classifies every tool-less completion (including titles) as aux.
        assert [r for r in provider.requests if r["kind"] == "aux"] == [], provider.requests
        assert len(provider.requests) == 1, provider.requests
        assert provider.invalid_requests() == [], provider.invalid_requests()


@pytest.mark.parametrize("status", [401, 403])
def test_provider_verdict_opt_out_never_contacts_auth_fallback(tmp_path, status: int) -> None:
    """A real SDK HTTP auth error obeys the discovered profile's should_fallback=False verdict."""
    marker = f"AUTH-OPT-OUT-{status}"
    provider_name = f"auth-opt-out-{status}"
    _source = f'''
        from providers import ProviderProfile, register_provider

        def classify_api_error(error, **kwargs):
            return {{"reason": "auth_permanent", "retryable": False, "should_fallback": False}}

        register_provider(ProviderProfile(
            name={provider_name!r}, env_vars=("OPENAI_API_KEY",), auth_type="api_key",
            static_models=("fake-model",), classify_api_error=classify_api_error,
        ))
    '''
    with FakeChatVariantServer([CError(status, marker)]) as primary, \
            FakeChatVariantServer([CText("FALLBACK-MUST-NOT-ANSWER")]) as fallback:
        nh = make_home(
            tmp_path,
            {"provider": provider_name, "base_url": primary.base_url, "default": "fake-model", "context_length": 128000},
            env_file={"OPENAI_API_KEY": "sk-fake"},
            extra_config={"fallback_providers": [{
                "provider": "custom", "model": "fallback-model", "base_url": fallback.base_url,
            }]},
        )
        _install_model_provider(nh.hermes_home, provider_name, _source)
        result = bounded_turn(nh, PROMPT, 45.0)

        assert result.proc.returncode != 0, result.describe()
        assert marker in result.proc.stdout + result.proc.stderr, result.describe()
        assert len(primary.main_requests()) == 1, primary.requests
        assert primary.main_records()[0]["response"] == "CError", primary.main_records()
        assert fallback.requests == [], fallback.requests


def test_external_process_client_construction_failure_reaches_real_cli(tmp_path) -> None:
    """The process-profile exception is not swallowed and retried as OpenAI(**command=...)."""
    provider_name = "exploding-process-profile"
    sentinel = "EXTERNAL-CLIENT-CONSTRUCTION-SENTINEL-139807"
    source = f'''
        from providers import ProviderProfile, register_provider

        class ExplodingProfile(ProviderProfile):
            def create_client(self, **client_kwargs):
                raise RuntimeError({sentinel!r})

        register_provider(ExplodingProfile(
            name={provider_name!r}, auth_type="external_process", base_url="process://{provider_name}",
            process_command="/bin/true", process_args=(), static_models=("fake-model",),
            supports_model_listing=False,
        ))
    '''
    nh = make_home(tmp_path, {"provider": provider_name, "default": "fake-model"})
    _install_model_provider(nh.hermes_home, provider_name, source)
    result = run_chat(nh, PROMPT)

    output = result.stdout + result.stderr
    assert result.returncode != 0, result.describe()
    assert sentinel in output, result.describe()
    assert "unexpected keyword argument" not in output, result.describe()


def _install_recording_acp_provider(hermes_home: Path, record_path: Path, forwards: bool) -> None:
    source = f'''
        import json
        import os
        from agent.acp_client import ACPClient
        from providers import ProviderProfile, register_provider

        RECORD_PATH = {str(record_path)!r}

        class RecordingACPClient(ACPClient):
            default_command = {acp.LAUNCHER_NAME!r}
            default_args = ("--acp", "--stdio")
            command_env_vars = ({acp.COMMAND_ENV_VAR!r},)
            args_env_var = {acp.ARGS_ENV_VAR!r}
            marker_base_url = "acp://{acp.PROVIDER}"
            placeholder_model = {acp.PLACEHOLDER_MODEL!r}
            label = "Recording ACP"

            def _create_chat_completion(self, **kwargs):
                record = {{
                    "kind": "main" if kwargs.get("tools") else "aux",
                    "has_max_tokens": "max_tokens" in kwargs,
                    "max_tokens": kwargs.get("max_tokens"),
                    "has_max_completion_tokens": "max_completion_tokens" in kwargs,
                }}
                fd = os.open(RECORD_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, (json.dumps(record) + "\\n").encode("utf-8"))
                finally:
                    os.close(fd)
                return super()._create_chat_completion(**kwargs)

        class RecordingACPProfile(ProviderProfile):
            def create_client(self, **client_kwargs):
                return RecordingACPClient(**client_kwargs)

        register_provider(RecordingACPProfile(
            name={acp.PROVIDER!r}, api_mode="chat_completions", env_vars=(),
            base_url="acp://{acp.PROVIDER}", auth_type="external_process",
            process_command={acp.LAUNCHER_NAME!r}, process_args=("--acp", "--stdio"),
            process_command_env_vars=({acp.COMMAND_ENV_VAR!r},),
            process_args_env_var={acp.ARGS_ENV_VAR!r}, static_models=({acp.PLACEHOLDER_MODEL!r},),
            forwards_max_tokens={forwards!r},
        ))
    '''
    _install_model_provider(hermes_home, acp.PROVIDER, source)


def _client_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("forwards", [True, False], ids=["opt-in", "default"])
def test_auxiliary_title_token_cap_reaches_real_process_client_only_when_opted_in(tmp_path, forwards: bool) -> None:
    pytest.importorskip("acp.schema", reason="the real ACP fake validates with the published schema")
    answer = f"ACP-TITLE-MAIN-ANSWER-{forwards}"
    fake = acp.AcpFake(tmp_path / "acp", [[acp.message(answer)]], aux_text="ACP Session Title")
    record_path = tmp_path / "client-calls.jsonl"
    nh = make_home(
        tmp_path,
        {"provider": acp.PROVIDER, "default": acp.PLACEHOLDER_MODEL},
        env_file=fake.env(),
        extra_config={
            "auxiliary": {"title_generation": {"enabled": True}},
            "display": {"interface": "cli"},
            "models_dev": {"url": "http://127.0.0.1:9/api.json"},
        },
    )
    _install_recording_acp_provider(nh.hermes_home, record_path, forwards)

    # A non-one-shot interactive turn is required: one-shot title generation is deliberately suppressed.
    ui = PtyHermes(tmp_path, ["chat"], None, rows=40, cols=120)
    try:
        ui.wait_ready()
        ui.submit("A concise title-generation boundary probe")
        ui.wait_for_text(answer, timeout=90)
        wait_until(lambda: fake.aux_prompts(), 30.0, "the real ACP title request")
        assert ui.exit() == 0, ui.dump()
    finally:
        ui.close()

    records = _client_records(record_path)
    aux_records = [r for r in records if r["kind"] == "aux"]
    assert len(fake.main_prompts()) == 1 and len(fake.aux_prompts()) == 1, fake.records()
    assert len(fake.pids()) == 2, "main and title calls must each execute a real ACP subprocess"
    assert fake.invalid() == [], fake.invalid()
    assert len(aux_records) == 1, records
    aux = aux_records[0]
    assert aux["has_max_tokens"] is forwards, records
    if forwards:
        assert isinstance(aux["max_tokens"], int) and aux["max_tokens"] > 0, records
    else:
        assert aux["max_tokens"] is None, records
    assert aux["has_max_completion_tokens"] is False, records


def test_non_string_part_types_survive_tool_result_request_without_fallback(tmp_path) -> None:
    """Arbitrary JSON type values reach the next request; a genuine image still follows them."""
    parts = [
        {"type": "text", "text": "multimodal plugin result"},
        {"type": {"slug": "full-time"}, "nested": {"type": ["string", "null"], "value": "preserved"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
    ]
    plugin_name = "multimodal_probe"
    source = f'''
        def register(ctx):
            ctx.register_tool(
                name={plugin_name!r}, toolset={plugin_name!r},
                schema={{"name": {plugin_name!r}, "description": "return a multimodal probe result",
                         "parameters": {{"type": "object", "properties": {{}}}}}},
                handler=lambda args, **kwargs: {{"_multimodal": True, "content": {parts!r},
                                                  "text_summary": "multimodal plugin result"}},
            )
    '''
    with FakeChatVariantServer([CTools([(plugin_name, {})]), CText(ANSWER)]) as primary, \
            FakeChatVariantServer([CText("FALLBACK-MUST-NOT-ANSWER")]) as fallback:
        nh = make_home(
            tmp_path,
            _custom_model(primary.base_url, supports_vision=True),
            env_file={"OPENAI_API_KEY": "sk-fake"},
            extra_config={
                "fallback_providers": [{"provider": "custom", "model": "fallback-model", "base_url": fallback.base_url}],
                "plugins": {"enabled": [plugin_name], "isolation": "in_process"},
                # Call the real plugin tool directly rather than adding a tool-search discovery turn.
                "tools": {"tool_search": {"enabled": "off"}},
            },
        )
        plugin = nh.hermes_home / "plugins" / plugin_name
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(f"name: {plugin_name}\nversion: 0.0.1\n", encoding="utf-8")
        (plugin / "__init__.py").write_text(textwrap.dedent(source), encoding="utf-8")

        result = run_chat(nh, "Call multimodal_probe, then answer.", args=("--toolsets", plugin_name))

        requests = primary.main_requests()
        assert len(requests) == 2, (
            result.describe(), [(r["kind"], r["response"]) for r in primary.requests],
            primary.invalid_requests(), fallback.requests,
        )
        assert result.returncode == 0 and ANSWER in result.stdout, result.describe()
        tool_messages = chat_messages(requests[1], "tool")
        assert len(tool_messages) == 1, requests[1].get("messages")
        assert tool_messages[0]["content"] == parts, tool_messages[0]
        assert fallback.requests == [], fallback.requests
