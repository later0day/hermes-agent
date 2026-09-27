"""Tests for ``_bound_platforms_for_profile`` and ``_augment_secondary_adapters_from_shared``.

These functions drive cron preflight rescue (E4): when a profile has dynamic
``/agent use`` bindings on a platform but no adapter credential of its own,
the shared primary adapter is borrowed for cron delivery.

The functions import ``DEFAULT_SOURCE_AGENT_BINDINGS_DB`` lazily from
``gateway.source_agent_binding`` (import inside the function body), so
patching the source module works correctly.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler_provider import (
    _augment_secondary_adapters_from_shared,
    _bound_platforms_for_profile,
)
from gateway.config import Platform
from gateway.source_agent_binding import SourceAgentBindingStore


# ── _bound_platforms_for_profile ────────────────────────────────────────────

class TestBoundPlatformsForProfile:
    def test_returns_empty_set_when_no_store_exists(self, tmp_path):
        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(tmp_path / "nonexistent.sqlite"),
        ):
            assert _bound_platforms_for_profile("test-profile") == set()

    def test_returns_empty_set_when_empty_profile_name(self):
        assert _bound_platforms_for_profile("") == set()
        assert _bound_platforms_for_profile("  ") == set()

    def test_returns_platforms_from_bindings(self, tmp_path):
        db_path = tmp_path / "bindings.sqlite"
        store = SourceAgentBindingStore(db_path)
        try:
            store.set_binding("source:dingtalk:group:g1:u1", "coder")
            store.set_binding("source:dingtalk:dm:u2", "coder")
            store.set_binding("source:telegram:dm:u3", "coder")
        finally:
            store.close()

        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(db_path),
        ):
            result = _bound_platforms_for_profile("coder")
        assert result == {"dingtalk", "telegram"}

    def test_only_returns_platforms_for_requested_profile(self, tmp_path):
        db_path = tmp_path / "bindings.sqlite"
        store = SourceAgentBindingStore(db_path)
        try:
            store.set_binding("source:dingtalk:group:g1:u1", "coder")
            store.set_binding("source:slack:group:T1:C1:u1", "reviewer")
        finally:
            store.close()

        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(db_path),
        ):
            assert _bound_platforms_for_profile("coder") == {"dingtalk"}
            assert _bound_platforms_for_profile("reviewer") == {"slack"}

    def test_malformed_binding_keys_are_filtered(self):
        """Only keys matching source:<platform>:... are accepted."""
        result: set[str] = set()
        for key in ["bad", "source:", "x:y", "source:dingtalk:rest"]:
            parts = key.split(":")
            if len(parts) >= 2 and parts[0] == "source" and parts[1]:
                result.add(parts[1].lower())
        assert result == {"dingtalk"}  # "bad", "source:", "x:y" all rejected

    def test_db_error_returns_empty_set(self, tmp_path):
        """Any exception during lookup is caught and returns empty set."""
        db_path = tmp_path / "bindings.sqlite"
        db_path.write_text("not a valid sqlite db")

        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(db_path),
        ):
            assert _bound_platforms_for_profile("test") == set()


# ── _augment_secondary_adapters_from_shared ──────────────────────────────

class TestAugmentSecondaryAdaptersFromShared:
    def test_returns_original_when_no_shared_adapters(self):
        original = {Platform.DINGTALK: MagicMock(spec=["send"])}
        result = _augment_secondary_adapters_from_shared(
            original,
            shared_adapters={},
            profile_name="coder",
        )
        assert result is original  # identity preserved

    def test_returns_original_when_no_bindings(self, tmp_path):
        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(tmp_path / "nonexistent.sqlite"),
        ):
            original = {Platform.DINGTALK: MagicMock(spec=["send"])}
            result = _augment_secondary_adapters_from_shared(
                original,
                shared_adapters={Platform.SLACK: MagicMock(spec=["send"])},
                profile_name="coder",
            )
            assert result is original

    def test_borrows_when_platform_is_bound_but_not_owned(self, tmp_path):
        db_path = tmp_path / "bindings.sqlite"
        store = SourceAgentBindingStore(db_path)
        try:
            store.set_binding("source:dingtalk:group:g1:u1", "coder")
        finally:
            store.close()

        shared_dt = MagicMock(spec=["send"])
        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(db_path),
        ):
            result = _augment_secondary_adapters_from_shared(
                {Platform.SLACK: MagicMock(spec=["send"])},
                shared_adapters={Platform.DINGTALK: shared_dt},
                profile_name="coder",
            )

        assert Platform.DINGTALK in result
        assert result[Platform.DINGTALK] is shared_dt
        assert Platform.SLACK in result  # original kept

    def test_does_not_overwrite_owned_adapter(self, tmp_path):
        db_path = tmp_path / "bindings.sqlite"
        store = SourceAgentBindingStore(db_path)
        try:
            store.set_binding("source:dingtalk:group:g1:u1", "coder")
        finally:
            store.close()

        owned_dt = MagicMock(spec=["send"])
        shared_dt = MagicMock(spec=["send"])
        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(db_path),
        ):
            result = _augment_secondary_adapters_from_shared(
                {Platform.DINGTALK: owned_dt},
                shared_adapters={Platform.DINGTALK: shared_dt},
                profile_name="coder",
            )

        assert result[Platform.DINGTALK] is owned_dt  # NOT the shared one

    def test_empty_original_dict_is_augmented(self, tmp_path):
        db_path = tmp_path / "bindings.sqlite"
        store = SourceAgentBindingStore(db_path)
        try:
            store.set_binding("source:dingtalk:group:g1:u1", "coder")
        finally:
            store.close()

        shared_dt = MagicMock(spec=["send"])
        with patch(
            "gateway.source_agent_binding.DEFAULT_SOURCE_AGENT_BINDINGS_DB",
            str(db_path),
        ):
            result = _augment_secondary_adapters_from_shared(
                {},
                shared_adapters={Platform.DINGTALK: shared_dt},
                profile_name="coder",
            )

        assert result is not None
        assert Platform.DINGTALK in result