"""Regression: top-level ``dingtalk:`` keys bridge into PlatformConfig.extra.

The DingTalk adapter reads its configuration from ``PlatformConfig.extra``
(see tests/gateway/test_dingtalk.py::TestConfig::test_reads_config_from_extra
and adapter.py __init__ / _create_and_stream_card).  The ``bridged`` allowlist
in gateway/config.py copies top-level ``platforms.<p>`` keys into ``extra``.

Two production incidents traced to keys NOT being in that allowlist while
configured only under the top-level ``dingtalk:`` block (extra left unset):

1. ``allow_all_users`` → under multiplex + installed secret-scope the plugin's
   os.environ hook (DINGTALK_ALLOW_ALL_USERS) is invisible to the scope-aware
   auth path (_auth_env reads scope, not os.environ; #72348), so every
   unrecognized DM fell through to the pairing prompt.
2. ``card_template_id`` → fell back to the built-in default template
   (382e4302, key ``msgContent``) while ``card_content_key``'s own
   load_config_readonly fallback still read the configured ``content`` key →
   template/key mismatch → streaming_update ``500 未知错误`` and a
   webhook-fallback double-send.

This drives the REAL ``load_gateway_config`` against a temp HERMES_HOME so the
actual bridge loop is exercised.
"""

import os
import tempfile
import textwrap


def _load_with_toplevel_dingtalk(tmp_home: str):
    from gateway.config import load_gateway_config, Platform
    cfg = load_gateway_config()
    dt = cfg.platforms.get(Platform.DINGTALK)
    return dict(getattr(dt, "extra", {}) or {})


def _run_case():
    from gateway.config import load_gateway_config, Platform  # noqa: F401
    with tempfile.TemporaryDirectory() as home:
        with open(os.path.join(home, "config.yaml"), "w") as fh:
            fh.write(textwrap.dedent("""\
                _config_version: 31
                model: {provider: alibaba, default: qwen-plus}
                dingtalk:
                  app_code: testapp123
                  corp_id: testcorp456
                  agent_id: '789'
                  reply_at_sender: true
                  allow_all_users: true
                  require_mention: true
                  card_template_id: 26e55230-9e7a-436e-9409-c4a5d8a2dcb8.schema
                  card_content_key: content
                  allowed_users: 'u1,u2'
                  free_response_chats: 'c1'
            """))
        prev = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        try:
            # force a fresh load under this HERMES_HOME
            import importlib
            import gateway.config as gc
            importlib.reload(gc)
            cfg = gc.load_gateway_config()
            dt = cfg.platforms.get(gc.Platform.DINGTALK)
            extra = dict(getattr(dt, "extra", {}) or {})
        finally:
            if prev is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = prev
        return extra


def test_toplevel_dingtalk_keys_bridge_into_extra():
    extra = _run_case()
    expected = {
        "app_code": "testapp123",
        "corp_id": "testcorp456",
        "agent_id": "789",
        "reply_at_sender": True,
        "allow_all_users": True,
        "require_mention": True,
        "card_template_id": "26e55230-9e7a-436e-9409-c4a5d8a2dcb8.schema",
        "card_content_key": "content",
        "allowed_users": "u1,u2",
        "free_response_chats": "c1",
    }
    missing = {k: v for k, v in expected.items() if extra.get(k) != v}
    assert not missing, f"top-level dingtalk keys not bridged into extra: {missing}"


if __name__ == "__main__":
    # Manual driver (pytest not installed in .venv).
    test_toplevel_dingtalk_keys_bridge_into_extra()
    print("PASS: test_toplevel_dingtalk_keys_bridge_into_extra")
