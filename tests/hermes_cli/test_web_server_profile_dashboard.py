"""Dashboard profile management endpoints for multi-agent profiles."""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def isolated_dashboard_profiles(tmp_path, monkeypatch):
    from gateway import agent_audit
    from gateway.source_agent_binding import SourceAgentBindingStore
    from hermes_cli import profiles, web_server

    default_home = tmp_path / ".hermes"
    worker_home = default_home / "profiles" / "worker_alpha"
    for home in (default_home, worker_home):
        (home / "workspace").mkdir(parents=True, exist_ok=True)
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            "model:\n  provider: openrouter\n  default: nous/default\n",
            encoding="utf-8",
        )

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(default_home))
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: default_home / "profiles")
    monkeypatch.setattr(
        agent_audit,
        "DEFAULT_AGENT_AUDIT_LOG",
        default_home / "gateway_agent_audit.jsonl",
    )

    bindings_db = tmp_path / "bindings.sqlite"
    monkeypatch.setattr(
        web_server,
        "_source_binding_store",
        lambda: SourceAgentBindingStore(bindings_db),
    )

    return {"default": default_home, "worker_alpha": worker_home}


@pytest.mark.asyncio
async def test_profile_details_includes_bindings_kanban_cron_and_health(isolated_dashboard_profiles):
    from hermes_cli import kanban_db as kb
    from hermes_cli import web_server

    await web_server.set_source_binding(
        web_server.SourceBindingUpdate(
            source_binding_key="source:dingtalk:group:chat1:user1",
            profile_name="worker_alpha",
        )
    )
    web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker cron",
        schedule="every 1h",
        name="worker-cron",
    )
    memory_dir = isolated_dashboard_profiles["worker_alpha"] / "memories"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "worker memory\nOPENAI_API_KEY=sk-secret-value",
        encoding="utf-8",
    )
    (memory_dir / "USER.md").write_text("user memory", encoding="utf-8")
    conn = kb.connect()
    try:
        kb.create_task(
            conn,
            title="Worker task",
            body="do work",
            assignee="worker_alpha",
            created_by="test",
            initial_status="running",
        )
    finally:
        conn.close()

    details = await web_server.get_profile_details("worker_alpha")

    assert details["profile"]["name"] == "worker_alpha"
    assert details["model"]["model"] == "nous/default"
    assert details["bindings"][0]["source_binding_key"] == "source:dingtalk:group:chat1:user1"
    assert details["bindings"][0]["target_summary"]["platform"] == "dingtalk"
    assert details["bindings"][0]["target_summary"]["chat_type"] == "group"
    assert details["bindings"][0]["target_summary"]["chat_id"] == "chat1"
    assert details["bindings"][0]["webhook_status"]["state"] == "missing"
    assert details["kanban"]["total"] == 1
    assert details["cron"]["owner_job_count"] == 1
    assert details["health"]["checks"]["workspace_exists"] is True
    assert details["paths"]["workspace"].endswith("worker_alpha/workspace")
    assert details["skills"] == {"count": 0, "names": [], "truncated": False}
    assert details["memory"]["memory_dir"].endswith("worker_alpha/memories")
    assert details["memory"]["memory_dir_exists"] is True
    assert details["memory"]["memory_file_count"] == 2
    assert details["memory"]["state_db"].endswith("worker_alpha/state.db")
    previews = {item["name"]: item for item in details["memory"]["previews"]}
    assert previews["MEMORY.md"]["exists"] is True
    assert "worker memory" in previews["MEMORY.md"]["content"]
    assert "sk-secret-value" not in previews["MEMORY.md"]["content"]
    assert previews["USER.md"]["content"] == "user memory"
    assert details["audit"]["events"] == []
    assert details["workspace"] == {
        "provider": "local",
        "kind": "profile",
        "ref": details["paths"]["workspace"],
        "display_path": details["paths"]["workspace"],
        "sandbox_id": None,
        "capabilities": {
            "local_path": True,
            "open_path": True,
            "sandbox": False,
        },
    }


@pytest.mark.asyncio
async def test_profile_details_redacts_binding_webhook_secrets(isolated_dashboard_profiles):
    from hermes_cli import web_server

    store = web_server._source_binding_store()
    try:
        store.set_binding(
            "source:dingtalk:group:chat1:user1",
            "worker_alpha",
            agent_id="worker_alpha",
            fallback_target={
                "platform": "dingtalk",
                "chat_type": "group",
                "chat_id": "chat1",
                "chat_name": "Worker Group",
                "user_name": "Alice",
            },
            fallback_extra={
                "session_webhook": "https://oapi.dingtalk.com/robot/sendBySession?session=secret",
                "session_webhook_expired_time": 1779446445670,
                "api_token": "secret-token",
                "note": "visible",
            },
        )
    finally:
        store.close()

    details = await web_server.get_profile_details("worker_alpha")
    extra = details["bindings"][0]["fallback_extra"]

    assert "session_webhook" not in extra
    assert extra["session_webhook_configured"] is True
    assert extra["session_webhook_expired_time"] == 1779446445670
    assert extra["api_token"] == "[REDACTED]"
    assert extra["note"] == "visible"
    assert details["bindings"][0]["target_summary"]["label"] == "Worker Group"
    assert details["bindings"][0]["target_summary"]["scope"] == "group / Alice"
    assert details["bindings"][0]["webhook_status"] == {
        "configured": True,
        "state": "expired",
        "kind": "temporary",
        "expires_at": 1779446445670,
        "expired": True,
        "label": "expired",
    }
    assert "secret" not in json.dumps(details["bindings"])


@pytest.mark.asyncio
async def test_profile_list_includes_agent_id_and_binding_count(isolated_dashboard_profiles):
    from hermes_cli import web_server

    await web_server.set_source_binding(
        web_server.SourceBindingUpdate(
            source_binding_key="source:dingtalk:group:chat1:user1",
            profile_name="worker_alpha",
        )
    )

    result = await web_server.list_profiles_endpoint()
    by_name = {profile["name"]: profile for profile in result["profiles"]}

    assert by_name["default"]["agent_id"] == "default"
    assert by_name["default"]["binding_count"] == 0
    assert by_name["worker_alpha"]["agent_id"] == "worker_alpha"
    assert by_name["worker_alpha"]["binding_count"] == 1
    assert by_name["worker_alpha"]["binding_summary"] == {
        "total": 1,
        "webhook_configured": 0,
        "webhook_expired": 0,
        "webhook_permanent": 0,
        "webhook_temporary": 0,
    }


@pytest.mark.asyncio
async def test_profile_details_tolerates_invalid_profile_config(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    (worker_home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n    docker_volumes: []\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["model"] == {"provider": "", "model": ""}
    assert details["health"]["status"] == "warning"
    assert details["health"]["checks"]["config_valid"] is False
    assert "Invalid profile config" in details["health"]["config_error"]


@pytest.mark.asyncio
async def test_profile_health_ignores_resolved_config_parse_warning(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    logs_dir = worker_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 18:28:50,094 WARNING gateway.config: "
        "Failed to process config.yaml - falling back to .env / gateway.json values. "
        "Check /tmp/.hermes/config.yaml for syntax errors. Error: while parsing a block mapping\n"
        "2026-05-22 18:28:38,631 WARNING hermes_cli.config: "
        "Failed to parse /tmp/.hermes/config.yaml: while parsing a block mapping. "
        "Falling back to default config - every user override is being IGNORED. "
        "Fix the YAML and restart.\n"
        "2026-05-22 18:14:16,451 WARNING hermes_cli.web_server: "
        "Profile worker_alpha config parse failed: mapping values are not allowed here\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["health"]["status"] == "ok"
    assert details["health"]["config_error"] is None
    assert details["health"]["recent_error"] is None


@pytest.mark.asyncio
async def test_dashboard_profile_clone_copies_config_but_no_env_or_skills(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from hermes_cli import profiles, web_server
    from hermes_cli.profiles import NO_BUNDLED_SKILLS_MARKER

    default_home = isolated_dashboard_profiles["default"]
    (default_home / ".env").write_text("DASHSCOPE_API_KEY=secret\n", encoding="utf-8")
    (default_home / "skills" / "demo").mkdir(parents=True)
    (default_home / "skills" / "demo" / "SKILL.md").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "check_alias_collision", lambda name: None)
    monkeypatch.setattr(profiles, "create_wrapper_script", lambda name: None)

    result = await web_server.create_profile_endpoint(
        web_server.ProfileCreate(name="envless_clone", clone_from_default=True)
    )

    clone_home = default_home / "profiles" / "envless_clone"
    assert result["name"] == "envless_clone"
    assert (clone_home / "config.yaml").exists()
    assert not (clone_home / "skills" / "demo" / "SKILL.md").exists()
    assert (clone_home / NO_BUNDLED_SKILLS_MARKER).exists()
    assert not (clone_home / ".env").exists()


@pytest.mark.asyncio
async def test_dashboard_profile_clone_from_template_copies_identity_but_no_env_or_skills(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from hermes_cli import profiles, web_server
    from hermes_cli.profiles import NO_BUNDLED_SKILLS_MARKER

    default_home = isolated_dashboard_profiles["default"]
    source_home = isolated_dashboard_profiles["worker_alpha"]
    (source_home / ".env").write_text("DASHSCOPE_API_KEY=secret\n", encoding="utf-8")
    (source_home / "SOUL.md").write_text("template soul\n", encoding="utf-8")
    (source_home / "skills" / "demo").mkdir(parents=True)
    (source_home / "skills" / "demo" / "SKILL.md").write_text("name: demo\n", encoding="utf-8")
    profiles.write_profile_meta(source_home, template=True)
    monkeypatch.setattr(profiles, "check_alias_collision", lambda name: None)
    monkeypatch.setattr(profiles, "create_wrapper_script", lambda name: None)

    result = await web_server.create_profile_endpoint(
        web_server.ProfileCreate(name="from_template", clone_from="worker_alpha")
    )

    clone_home = default_home / "profiles" / "from_template"
    assert result["name"] == "from_template"
    assert (clone_home / "config.yaml").exists()
    assert (clone_home / "SOUL.md").read_text(encoding="utf-8") == "template soul\n"
    assert not (clone_home / "skills" / "demo" / "SKILL.md").exists()
    assert (clone_home / NO_BUNDLED_SKILLS_MARKER).exists()
    assert not (clone_home / ".env").exists()


@pytest.mark.asyncio
async def test_profile_template_endpoint_updates_metadata(isolated_dashboard_profiles):
    from hermes_cli import web_server

    result = await web_server.set_profile_template(
        "worker_alpha",
        web_server.ProfileTemplateUpdate(template=True),
    )
    details = await web_server.get_profile_details("worker_alpha")

    assert result == {"ok": True, "name": "worker_alpha", "template": True}
    assert details["profile"]["template"] is True


@pytest.mark.asyncio
async def test_copy_profile_skills_from_default(isolated_dashboard_profiles):
    from hermes_cli import web_server

    default_home = isolated_dashboard_profiles["default"]
    (default_home / "skills" / "default_demo").mkdir(parents=True)
    (default_home / "skills" / "default_demo" / "SKILL.md").write_text(
        "name: default-demo\n",
        encoding="utf-8",
    )

    result = await web_server.copy_profile_skills(
        "worker_alpha",
        web_server.ProfileSkillsCopy(source_profile="default"),
    )
    details = await web_server.get_profile_details("worker_alpha")

    assert result["ok"] is True
    assert result["copied_skills"] == ["default_demo"]
    assert result["skills"]["names"] == ["default_demo"]
    assert details["skills"]["names"] == ["default_demo"]


@pytest.mark.asyncio
async def test_copy_selected_profile_skills_from_default(isolated_dashboard_profiles):
    from hermes_cli import web_server

    default_home = isolated_dashboard_profiles["default"]
    for skill in ("apple/notes", "devops/kanban-worker", "research/arxiv"):
        skill_dir = default_home / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"name: {skill}\n", encoding="utf-8")

    result = await web_server.copy_profile_skills(
        "worker_alpha",
        web_server.ProfileSkillsCopy(
            source_profile="default",
            skills=["research/arxiv", "apple/notes"],
        ),
    )
    details = await web_server.get_profile_details("worker_alpha")

    assert result["ok"] is True
    assert result["copied_skills"] == ["apple/notes", "research/arxiv"]
    assert result["skills"]["names"] == ["apple/notes", "research/arxiv"]
    assert details["skills"]["names"] == ["apple/notes", "research/arxiv"]
    assert not (isolated_dashboard_profiles["worker_alpha"] / "skills" / "devops" / "kanban-worker").exists()


@pytest.mark.asyncio
async def test_copy_selected_profile_skill_rejects_path_traversal(isolated_dashboard_profiles):
    from hermes_cli import web_server

    with pytest.raises(Exception) as excinfo:
        await web_server.copy_profile_skills(
            "worker_alpha",
            web_server.ProfileSkillsCopy(source_profile="default", skills=["../secret"]),
        )

    assert getattr(excinfo.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_delete_profile_skill_removes_only_target_skill(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    for skill in ("apple/notes", "apple/reminders"):
        skill_dir = worker_home / "skills" / skill
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"name: {skill}\n", encoding="utf-8")

    result = await web_server.delete_profile_skill("worker_alpha", "apple/notes")
    details = await web_server.get_profile_details("worker_alpha")

    assert result["ok"] is True
    assert result["deleted_skill"] == "apple/notes"
    assert result["skills"]["names"] == ["apple/reminders"]
    assert details["skills"]["names"] == ["apple/reminders"]
    assert not (worker_home / "skills" / "apple" / "notes").exists()
    assert (worker_home / "skills" / "apple" / "reminders" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_delete_profile_skill_rejects_default_and_path_traversal(isolated_dashboard_profiles):
    from hermes_cli import web_server

    default_home = isolated_dashboard_profiles["default"]
    skill_dir = default_home / "skills" / "apple" / "notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("name: notes\n", encoding="utf-8")

    with pytest.raises(Exception) as default_exc:
        await web_server.delete_profile_skill("default", "apple/notes")
    with pytest.raises(Exception) as traversal_exc:
        await web_server.delete_profile_skill("worker_alpha", "../secret")

    assert getattr(default_exc.value, "status_code", None) == 400
    assert getattr(traversal_exc.value, "status_code", None) == 400
    assert (skill_dir / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_profile_memory_file_api_reads_writes_and_audits(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from gateway import agent_audit
    from hermes_cli import web_server

    audit_path = isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl"
    monkeypatch.setattr(agent_audit, "DEFAULT_AGENT_AUDIT_LOG", audit_path)

    missing = await web_server.get_profile_memory_file("worker_alpha", "MEMORY.md")
    written = await web_server.update_profile_memory_file(
        "worker_alpha",
        "MEMORY.md",
        web_server.ProfileMemoryFileUpdate(content="remember this worker fact\n"),
    )
    loaded = await web_server.get_profile_memory_file("worker_alpha", "MEMORY.md")
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]

    assert missing["exists"] is False
    assert written["ok"] is True
    assert written["memory"]["memory_file_count"] == 1
    assert loaded["exists"] is True
    assert loaded["content"] == "remember this worker fact\n"
    assert events[-1]["action"] == "agent.memory_update"
    assert events[-1]["after"]["file"] == "MEMORY.md"


@pytest.mark.asyncio
async def test_profile_memory_file_api_rejects_unmanaged_files(isolated_dashboard_profiles):
    from hermes_cli import web_server

    with pytest.raises(Exception) as traversal_exc:
        await web_server.get_profile_memory_file("worker_alpha", "../config.yaml")
    with pytest.raises(Exception) as unknown_exc:
        await web_server.update_profile_memory_file(
            "worker_alpha",
            "NOTES.md",
            web_server.ProfileMemoryFileUpdate(content="nope"),
        )

    assert getattr(traversal_exc.value, "status_code", None) == 400
    assert getattr(unknown_exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_profile_skill_manifest_api_reads_writes_and_audits(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from gateway import agent_audit
    from hermes_cli import web_server

    audit_path = isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl"
    monkeypatch.setattr(agent_audit, "DEFAULT_AGENT_AUDIT_LOG", audit_path)
    skill_dir = isolated_dashboard_profiles["worker_alpha"] / "skills" / "research" / "arxiv"
    skill_dir.mkdir(parents=True)
    manifest = skill_dir / "SKILL.md"
    manifest.write_text("name: arxiv\ndescription: old\n", encoding="utf-8")

    loaded = await web_server.get_profile_skill_manifest("worker_alpha", "research/arxiv")
    updated = await web_server.update_profile_skill_manifest(
        "worker_alpha",
        "research/arxiv",
        web_server.ProfileSkillManifestUpdate(
            content="name: arxiv\ndescription: updated\n",
        ),
    )
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]

    assert loaded["content"] == "name: arxiv\ndescription: old\n"
    assert updated["ok"] is True
    assert manifest.read_text(encoding="utf-8") == "name: arxiv\ndescription: updated\n"
    assert events[-1]["action"] == "agent.skill_update"
    assert events[-1]["after"]["skill"] == "research/arxiv"


@pytest.mark.asyncio
async def test_profile_skill_manifest_api_rejects_default_and_path_traversal(
    isolated_dashboard_profiles,
):
    from hermes_cli import web_server

    default_skill = isolated_dashboard_profiles["default"] / "skills" / "demo"
    default_skill.mkdir(parents=True)
    (default_skill / "SKILL.md").write_text("name: demo\n", encoding="utf-8")

    with pytest.raises(Exception) as default_exc:
        await web_server.update_profile_skill_manifest(
            "default",
            "demo",
            web_server.ProfileSkillManifestUpdate(content="name: demo\n"),
        )
    with pytest.raises(Exception) as traversal_exc:
        await web_server.get_profile_skill_manifest("worker_alpha", "../secret")

    assert getattr(default_exc.value, "status_code", None) == 400
    assert getattr(traversal_exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_profile_describe_endpoint_reuses_profile_describer(isolated_dashboard_profiles, monkeypatch):
    from hermes_cli import profile_describer, web_server

    observed = {}

    def fake_describe_profile(name, *, overwrite=False):
        observed["name"] = name
        observed["overwrite"] = overwrite
        return profile_describer.DescribeOutcome(
            name,
            True,
            "described",
            description="Handles worker tasks.",
        )

    monkeypatch.setattr(profile_describer, "describe_profile", fake_describe_profile)

    result = await web_server.describe_profile_endpoint(
        "worker_alpha",
        web_server.ProfileDescribeRequest(overwrite=True),
    )

    assert observed == {"name": "worker_alpha", "overwrite": True}
    assert result == {
        "ok": True,
        "name": "worker_alpha",
        "reason": "described",
        "description": "Handles worker tasks.",
    }


@pytest.mark.asyncio
async def test_agent_audit_endpoint_filters_profile(isolated_dashboard_profiles, monkeypatch):
    from gateway import agent_audit
    from hermes_cli import web_server

    audit_path = isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl"
    monkeypatch.setattr(agent_audit, "DEFAULT_AGENT_AUDIT_LOG", audit_path)
    audit_path.write_text(
        "\n".join(
            [
                json.dumps({"ts": "1", "action": "agent.use", "profile_name": "default"}),
                json.dumps(
                    {
                        "ts": "2",
                        "action": "agent.webhook",
                        "profile_name": "worker_alpha",
                        "after": {"webhook_url": "https://secret.example"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = await web_server.list_agent_audit(profile="worker_alpha", limit=10)

    assert [event["action"] for event in result["events"]] == ["agent.webhook"]
    assert result["events"][0]["after"]["webhook_url"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_agent_audit_endpoint_supports_offset_and_scan_cap(isolated_dashboard_profiles, monkeypatch):
    from gateway import agent_audit
    from hermes_cli import web_server

    audit_path = isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl"
    monkeypatch.setattr(agent_audit, "DEFAULT_AGENT_AUDIT_LOG", audit_path)
    audit_path.write_text(
        "\n".join(
            json.dumps({"ts": str(i), "action": f"agent.{i}", "profile_name": "worker_alpha"})
            for i in range(5)
        )
        + "\n",
        encoding="utf-8",
    )

    page = await web_server.list_agent_audit(
        profile="worker_alpha",
        limit=2,
        offset=1,
        max_scan_lines=4,
    )

    assert page["limit"] == 2
    assert page["offset"] == 1
    assert [event["action"] for event in page["events"]] == ["agent.3", "agent.2"]


@pytest.mark.asyncio
async def test_profile_template_toggle_writes_audit(isolated_dashboard_profiles, monkeypatch):
    from gateway import agent_audit
    from hermes_cli import web_server

    audit_path = isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl"
    monkeypatch.setattr(agent_audit, "DEFAULT_AGENT_AUDIT_LOG", audit_path)

    marked = await web_server.set_profile_template(
        "worker_alpha",
        web_server.ProfileTemplateUpdate(template=True),
    )
    unmarked = await web_server.set_profile_template(
        "worker_alpha",
        web_server.ProfileTemplateUpdate(template=False),
    )

    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert marked == {"ok": True, "name": "worker_alpha", "template": True}
    assert unmarked == {"ok": True, "name": "worker_alpha", "template": False}
    assert [event["action"] for event in events] == [
        "agent.template_create",
        "agent.template_clear",
    ]
    assert events[0]["profile_name"] == "worker_alpha"
    assert events[0]["actor_user_id"] == "dashboard"


@pytest.mark.asyncio
async def test_profile_metadata_endpoint_updates_description_and_template(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from gateway import agent_audit
    from hermes_cli import profiles, web_server

    audit_path = isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl"
    monkeypatch.setattr(agent_audit, "DEFAULT_AGENT_AUDIT_LOG", audit_path)

    result = await web_server.update_profile_metadata(
        "worker_alpha",
        web_server.ProfileMetadataUpdate(
            description="Handles focused worker tasks.",
            description_auto=False,
            template=True,
        ),
    )
    meta = profiles.read_profile_meta(isolated_dashboard_profiles["worker_alpha"])
    events = [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]

    assert result["ok"] is True
    assert result["metadata"] == {
        "description": "Handles focused worker tasks.",
        "description_auto": False,
        "template": True,
    }
    assert meta == result["metadata"]
    assert events[-1]["action"] == "agent.metadata_update"
    assert events[-1]["after"]["template"] is True


@pytest.mark.asyncio
async def test_profile_health_ignores_title_generation_warning(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    logs_dir = worker_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 14:29:59,992 WARNING agent.auxiliary_client: "
        "Auxiliary: marking nous unhealthy for 60s (payment / credit error).\n"
        "2026-05-22 14:30:00,050 WARNING agent.title_generator: "
        "Title generation failed: Connection error.\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["health"]["status"] == "ok"
    assert details["health"]["recent_error"] is None


@pytest.mark.asyncio
async def test_profile_health_ignores_deleted_scratch_cwd_traceback(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    logs_dir = worker_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 17:16:33,297 ERROR tools.terminal_tool: "
        "Terminal requirements check failed: [Errno 2] No such file or directory\n"
        "Traceback (most recent call last):\n"
        "  File \"/repo/tools/terminal_tool.py\", line 2156, in check_terminal_requirements\n"
        "    config = _get_env_config()\n"
        "  File \"/repo/tools/terminal_tool.py\", line 1021, in _get_env_config\n"
        "    default_cwd = os.getcwd()\n"
        "FileNotFoundError: [Errno 2] No such file or directory\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["health"]["status"] == "ok"
    assert details["health"]["recent_error"] is None


@pytest.mark.asyncio
async def test_default_profile_health_ignores_gateway_transport_noise(
    isolated_dashboard_profiles,
):
    from hermes_cli import web_server

    default_home = isolated_dashboard_profiles["default"]
    logs_dir = default_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 17:17:26,646 WARNING gateway.run: Shutdown context: signal=SIGTERM\n"
        "2026-05-22 17:17:28,301 ERROR dingtalk_stream.client: "
        "[start] network exception, error=\n"
        "2026-05-22 17:18:34,395 WARNING gateway.run: kanban notifier: "
        "send failed for t_85fc80d7 on dingtalk (attempt 1/3): adapter returned success=False\n"
        "2026-05-22 17:18:57,480 WARNING gateway.run: kanban notifier: "
        "dropping subscription t_85fc80d7 on dingtalk after 3 consecutive send failures\n"
        "2026-05-22 17:18:50,194 WARNING agent.title_generator: "
        "Title generation failed: Connection error.\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("default")

    assert details["health"]["status"] == "ok"
    assert details["health"]["recent_error"] is None


@pytest.mark.asyncio
async def test_named_profile_health_ignores_global_gateway_transport_noise(
    isolated_dashboard_profiles,
):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    logs_dir = worker_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 18:29:16,684 WARNING gateway.run: Shutdown context: signal=SIGTERM\n"
        "2026-05-22 18:29:18,246 ERROR dingtalk_stream.client: "
        "[start] network exception, error=\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["health"]["status"] == "ok"
    assert details["health"]["recent_error"] is None


@pytest.mark.asyncio
async def test_profile_health_keeps_real_error_before_title_warning(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    logs_dir = worker_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 14:29:00,000 ERROR gateway.run: real gateway issue\n"
        "2026-05-22 14:29:59,992 WARNING agent.auxiliary_client: "
        "Auxiliary: marking nous unhealthy for 60s (payment / credit error).\n"
        "2026-05-22 14:30:00,050 WARNING agent.title_generator: "
        "Title generation failed: Connection error.\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["health"]["status"] == "warning"
    assert "real gateway issue" in details["health"]["recent_error"]


@pytest.mark.asyncio
async def test_profile_health_surfaces_warning_without_marking_unhealthy(isolated_dashboard_profiles):
    from hermes_cli import web_server

    worker_home = isolated_dashboard_profiles["worker_alpha"]
    logs_dir = worker_home / "logs"
    logs_dir.mkdir()
    (logs_dir / "errors.log").write_text(
        "2026-05-22 14:29:35,872 WARNING agent.tool_executor: "
        "Tool terminal returned error (5.28s): timed out\n",
        encoding="utf-8",
    )

    details = await web_server.get_profile_details("worker_alpha")

    assert details["health"]["status"] == "ok"
    assert "Tool terminal returned error" in details["health"]["recent_error"]


@pytest.mark.asyncio
async def test_profile_model_endpoint_updates_profile_config(isolated_dashboard_profiles):
    from hermes_cli import web_server

    result = await web_server.set_profile_model(
        "worker_alpha",
        web_server.ProfileModelUpdate(provider="anthropic", model="claude-test"),
    )
    current = await web_server.get_profile_model("worker_alpha")

    assert result == {"ok": True, "provider": "anthropic", "model": "claude-test"}
    assert current == {"provider": "anthropic", "model": "claude-test"}
    config_text = (isolated_dashboard_profiles["worker_alpha"] / "config.yaml").read_text(encoding="utf-8")
    assert "anthropic" in config_text
    assert "claude-test" in config_text


@pytest.mark.asyncio
async def test_source_binding_api_and_session_annotation(isolated_dashboard_profiles, monkeypatch):
    import hermes_state
    from hermes_cli import web_server

    monkeypatch.setattr(
        hermes_state,
        "DEFAULT_DB_PATH",
        isolated_dashboard_profiles["default"] / "state.db",
    )
    db = hermes_state.SessionDB()
    try:
        db.create_session(
            "agent:worker_alpha:dingtalk:group:chat1:user1",
            "dingtalk",
            model="test-model",
        )
    finally:
        db.close()

    set_result = await web_server.set_source_binding(
        web_server.SourceBindingUpdate(
            source_binding_key="source:dingtalk:group:chat1:user1",
            profile_name="worker_alpha",
        )
    )
    sessions = await web_server.get_sessions(limit=10, offset=0)

    assert set_result["binding"]["profile_name"] == "worker_alpha"
    assert sessions["sessions"][0]["source_binding_key"] == "source:dingtalk:group:chat1:user1"
    assert sessions["sessions"][0]["bound_profile"] == "worker_alpha"
    assert sessions["sessions"][0]["session_profile"] == "worker_alpha"

    cleared = await web_server.delete_source_binding("source:dingtalk:group:chat1:user1")
    sessions_after = await web_server.get_sessions(limit=10, offset=0)

    assert cleared == {"ok": True, "deleted": True}
    assert sessions_after["sessions"][0]["bound_profile"] == "default"


@pytest.mark.asyncio
async def test_source_binding_task_endpoint_creates_kanban_task_and_subscription(
    isolated_dashboard_profiles,
):
    from hermes_cli import kanban_db as kb
    from hermes_cli import web_server

    source_key = "source:dingtalk:group:chat1:user1"
    store = web_server._source_binding_store()
    try:
        store.set_binding(
            source_key,
            "worker_alpha",
            agent_id="worker_alpha",
            fallback_target={
                "platform": "dingtalk",
                "chat_id": "chat1",
                "chat_type": "group",
                "user_id": "user1",
                "thread_id": "thread1",
            },
        )
    finally:
        store.close()

    result = await web_server.create_source_binding_task(
        web_server.SourceBindingTaskCreate(
            source_binding_key=source_key,
            profile_name="worker_alpha",
            task="review session handoff",
        )
    )

    conn = kb.connect()
    try:
        task = kb.get_task(conn, result["task_id"])
        subs = kb.list_notify_subs(conn, result["task_id"])
    finally:
        conn.close()

    assert result["ok"] is True
    assert result["profile_name"] == "worker_alpha"
    assert result["subscribed"] is True
    assert task is not None
    assert task.assignee == "worker_alpha"
    assert task.title == "review session handoff"
    assert subs and subs[0]["platform"] == "dingtalk"
    assert subs[0]["chat_id"] == "chat1"
    assert subs[0]["thread_id"] == "thread1"
    audit = json.loads(
        (isolated_dashboard_profiles["default"] / "gateway_agent_audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()[-1]
    )
    assert audit["action"] == "delegate.create"
    assert audit["actor_user_id"] == "dashboard"
    assert audit["profile_name"] == "worker_alpha"
    assert audit["extra"]["entry"] == "dashboard.sessions"


@pytest.mark.asyncio
async def test_dashboard_delete_profile_removes_source_bindings_and_audits(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from gateway import agent_audit
    from hermes_cli import profiles as profiles_mod
    from hermes_cli import web_server

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *args, **kwargs: None)
    sent_notifications = []
    monkeypatch.setattr(
        web_server,
        "_send_dingtalk_session_webhook",
        lambda webhook, text: sent_notifications.append((webhook, text)),
    )
    store = web_server._source_binding_store()
    try:
        store.set_binding(
            "source:dingtalk:group:delete:user",
            "worker_alpha",
            agent_id="worker_alpha",
            fallback_target={"platform": "dingtalk", "chat_id": "delete"},
            fallback_extra={
                "session_webhook": "https://api.dingtalk.com/robot/sendBySession?session=secret",
                "session_webhook_expired_time": 9999999999999,
            },
        )
    finally:
        store.close()
    cron_job = await web_server.create_cron_job(
        web_server.CronJobCreate(
            prompt="worker cron",
            schedule="every 1h",
            name="delete-worker-cron",
        ),
        profile="worker_alpha",
    )

    result = await web_server.delete_profile_endpoint("worker_alpha")
    store = web_server._source_binding_store()
    try:
        remaining = store.list_bindings(profile_name="worker_alpha")
    finally:
        store.close()
    cron_jobs = web_server._call_cron_store("list_jobs", True)
    audit_events = [
        json.loads(line)
        for line in agent_audit.DEFAULT_AGENT_AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    ]

    assert result["ok"] is True
    assert not isolated_dashboard_profiles["worker_alpha"].exists()
    assert remaining == []
    assert all(job["id"] != cron_job["id"] for job in cron_jobs)
    assert len(sent_notifications) == 1
    assert sent_notifications[0][0] == "https://api.dingtalk.com/robot/sendBySession?session=secret"
    assert "worker_alpha" in sent_notifications[0][1]
    assert "/agent use <profile>" in sent_notifications[0][1]
    assert audit_events[-1]["action"] == "agent.delete"
    assert audit_events[-1]["extra"]["removed_bindings"] == 1
    assert audit_events[-1]["extra"]["removed_cron_jobs"] == 1
    assert audit_events[-1]["extra"]["notification"] == {
        "attempted": 1,
        "sent": 1,
        "failed": 0,
        "skipped": 0,
    }
    assert "secret" not in json.dumps(audit_events[-1])


@pytest.mark.asyncio
async def test_dashboard_delete_profile_notification_failure_does_not_block(
    isolated_dashboard_profiles,
    monkeypatch,
):
    from gateway import agent_audit
    from hermes_cli import profiles as profiles_mod
    from hermes_cli import web_server

    monkeypatch.setattr(profiles_mod, "_cleanup_gateway_service", lambda *args, **kwargs: None)

    def fail_send(webhook, text):
        raise RuntimeError("send failed")

    monkeypatch.setattr(web_server, "_send_dingtalk_session_webhook", fail_send)
    store = web_server._source_binding_store()
    try:
        store.set_binding(
            "source:dingtalk:group:delete:user",
            "worker_alpha",
            agent_id="worker_alpha",
            fallback_target={"platform": "dingtalk", "chat_id": "delete"},
            fallback_extra={
                "session_webhook": "https://api.dingtalk.com/robot/sendBySession?session=secret",
                "session_webhook_expired_time": 9999999999999,
            },
        )
    finally:
        store.close()

    result = await web_server.delete_profile_endpoint("worker_alpha")
    audit_events = [
        json.loads(line)
        for line in agent_audit.DEFAULT_AGENT_AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    ]

    assert result["ok"] is True
    assert not isolated_dashboard_profiles["worker_alpha"].exists()
    assert audit_events[-1]["extra"]["removed_bindings"] == 1
    assert audit_events[-1]["extra"]["notification"] == {
        "attempted": 1,
        "sent": 0,
        "failed": 1,
        "skipped": 0,
    }


@pytest.mark.asyncio
async def test_cron_owner_filter_remains_logical(isolated_dashboard_profiles):
    from hermes_cli import web_server

    default_job = web_server._call_cron_for_profile(
        "default",
        "create_job",
        prompt="default cron",
        schedule="every 1h",
        name="default-cron",
    )
    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker cron",
        schedule="every 1h",
        name="worker-cron",
    )

    default_jobs = await web_server.list_cron_jobs(profile="default")
    worker_jobs = await web_server.list_cron_jobs(profile="worker_alpha")

    assert [job["id"] for job in default_jobs] == [default_job["id"]]
    assert [job["id"] for job in worker_jobs] == [worker_job["id"]]
    assert not (isolated_dashboard_profiles["worker_alpha"] / "cron" / "jobs.json").exists()
