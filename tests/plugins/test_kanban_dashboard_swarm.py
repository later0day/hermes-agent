"""Kanban dashboard board payload extras used by the plugin UI."""

from __future__ import annotations

from pathlib import Path


def test_kanban_board_payload_includes_history_and_swarm_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db, kanban_swarm
    from plugins.kanban.dashboard import plugin_api

    kanban_db.init_db()
    conn = kanban_db.connect()
    try:
        root_id = kanban_db.create_task(
            conn,
            title="Root swarm",
            body="root",
            created_by="test",
            initial_status="running",
        )
        worker_id = kanban_db.create_task(
            conn,
            title="Worker A",
            body="worker",
            assignee="worker_alpha",
            created_by="test",
            initial_status="running",
        )
        done_id = kanban_db.create_task(
            conn,
            title="Done task",
            body="done",
            created_by="test",
            initial_status="running",
        )
        conn.execute(
            "UPDATE tasks SET status = 'done', completed_at = created_at WHERE id = ?",
            (done_id,),
        )
        conn.commit()
        kanban_db.link_tasks(conn, root_id, worker_id)
        kanban_swarm.post_blackboard_update(
            conn,
            root_id,
            author="test",
            key="topology",
            value={
                "goal": "Ship swarm UI",
                "worker_ids": [worker_id],
            },
        )
    finally:
        conn.close()

    payload = plugin_api.get_board(
        tenant=None,
        include_archived=False,
        board=None,
        workflow_template_id=None,
        current_step_key=None,
    )

    assert payload["history"][0]["id"] == done_id
    assert payload["swarm_graphs"]
    graph = payload["swarm_graphs"][0]
    assert graph["root_id"] == root_id
    assert graph["goal"] == "Ship swarm UI"
    assert graph["worker_ids"] == [worker_id]
    assert {node["role"] for node in graph["nodes"]} == {"root", "worker"}
    assert graph["edges"] == [{"parent_id": root_id, "child_id": worker_id}]


def test_kanban_board_payload_includes_full_swarm_topology(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)

    from hermes_cli import kanban_db, kanban_swarm
    from plugins.kanban.dashboard import plugin_api

    kanban_db.init_db()
    conn = kanban_db.connect()
    try:
        created = kanban_swarm.create_swarm(
            conn,
            goal="Ship profile polish",
            workers=[
                kanban_swarm.SwarmWorkerSpec(
                    profile="coder",
                    title="Implement profile polish",
                    body="Implement",
                ),
                kanban_swarm.SwarmWorkerSpec(
                    profile="reviewer",
                    title="Review profile polish",
                    body="Review",
                ),
            ],
            verifier_assignee="qa",
            synthesizer_assignee="lead",
            created_by="lead",
        )
    finally:
        conn.close()

    payload = plugin_api.get_board(
        tenant=None,
        include_archived=False,
        board=None,
        workflow_template_id=None,
        current_step_key=None,
    )

    graph = next(g for g in payload["swarm_graphs"] if g["root_id"] == created.root_id)
    assert graph["goal"] == "Ship profile polish"
    assert graph["worker_ids"] == created.worker_ids
    assert graph["verifier_id"] == created.verifier_id
    assert graph["synthesizer_id"] == created.synthesizer_id
    assert graph["counts"]["total"] == 5
    assert {node["role"] for node in graph["nodes"]} == {
        "root",
        "worker",
        "verifier",
        "synthesizer",
    }
    assert {edge["child_id"] for edge in graph["edges"]} >= set(created.worker_ids)
    assert {edge["child_id"] for edge in graph["edges"]} >= {
        created.verifier_id,
        created.synthesizer_id,
    }


def test_dashboard_swarm_graph_reuses_board_payload_and_task_drawer():
    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    css = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "style.css"
    js = bundle.read_text(encoding="utf-8")
    styles = css.read_text(encoding="utf-8")

    assert "function SwarmGraphStrip(props)" in js
    assert "function collectSwarmGraphs(boardData)" in js
    assert "boardData.swarm_graphs" in js
    assert "h(SwarmGraphStrip, {" in js
    assert "onClick: function () { props.onOpen(node.id); }" in js
    assert ".hermes-kanban-swarms" in styles
    assert ".hermes-kanban-swarm-node--running" in styles
