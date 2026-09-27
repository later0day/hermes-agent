"""Tests for image resend pattern matching + attached-image path scanning.

Covers the ``_wants_recent_image_resend`` and ``_find_latest_attached_image_path``
helpers from ``gateway.run_turn``, which handle DingTalk users asking for an
image re-send without re-uploading it.
"""
from __future__ import annotations

import os
import re
import tempfile

import pytest

from gateway.run_turn import (
    _IMAGE_ATTACHED_RE,
    _RECENT_IMAGE_RESEND_NOT_HANDLED,
    _RESEND_IMAGE_PATTERNS,
    _find_latest_attached_image_path,
    _wants_recent_image_resend,
)


# ── _wants_recent_image_resend ──────────────────────────────────────────────

class TestWantsRecentImageResend:
    """Pattern-match tests for the resend-intent regex."""

    # --- Chinese patterns ---
    @pytest.mark.parametrize("text", [
        "把刚才的图片重新发一下",
        "重新发一下那个图片",
        "图片重新发",
        "刚才那张图片发过来",
        "发一下刚才的图片",
    ])
    def test_chinese_patterns_match(self, text):
        assert _wants_recent_image_resend(text) is True

    # --- English patterns ---
    @pytest.mark.parametrize("text", [
        "resend the last image",
        "resend last image",
        "resend the latest image",
        "resend the recent image",
        "send the last image",
        "send the latest image",
        "send the recent image",
        "send last image",
    ])
    def test_english_patterns_match(self, text):
        assert _wants_recent_image_resend(text) is True

    # --- Non-matching patterns ---
    @pytest.mark.parametrize("text", [
        "hello world",
        "send a message",
        "resend the message",
        "",  # empty
        "image",
        "图片",  # just "image" without resend context
    ])
    def test_non_matching_patterns(self, text):
        assert _wants_recent_image_resend(text) is False

    def test_none_input(self):
        assert _wants_recent_image_resend(None) is False  # type: ignore[arg-type]

    # --- Case insensitivity ---
    def test_case_insensitive(self):
        assert _wants_recent_image_resend("RESEND THE LAST IMAGE") is True
        assert _wants_recent_image_resend("Resend The Latest Image") is True

    # --- Mixed context: resend appears but unrelated ---
    def test_resend_without_image_context_does_not_match(self):
        """'resend' alone or with 'message' is not enough."""
        assert _wants_recent_image_resend("please resend") is False
        assert _wants_recent_image_resend("resend that message") is False


# ── _IMAGE_ATTACHED_RE ──────────────────────────────────────────────────────

class TestImageAttachedRegex:
    """Tests for the [Image attached at: ...] marker regex."""

    def test_extracts_posix_path(self):
        m = _IMAGE_ATTACHED_RE.search("[Image attached at: /tmp/photo.png]")
        assert m is not None
        assert m.group(1) == "/tmp/photo.png"

    def test_extracts_windows_path(self):
        m = _IMAGE_ATTACHED_RE.search(r"[Image attached at: C:\Users\me\photo.jpg]")
        assert m is not None
        assert m.group(1) == r"C:\Users\me\photo.jpg"

    def test_extracts_path_with_spaces(self):
        m = _IMAGE_ATTACHED_RE.search("[Image attached at: /tmp/my photo.png]")
        assert m is not None
        assert m.group(1) == "/tmp/my photo.png"

    def test_no_match_without_marker(self):
        assert _IMAGE_ATTACHED_RE.search("just a regular message") is None

    def test_multiple_matches_finds_all(self):
        text = "[Image attached at: /tmp/a.png] [Image attached at: /tmp/b.png]"
        matches = list(_IMAGE_ATTACHED_RE.finditer(text))
        assert len(matches) == 2
        assert matches[0].group(1) == "/tmp/a.png"
        assert matches[1].group(1) == "/tmp/b.png"


# ── _find_latest_attached_image_path ─────────────────────────────────────────

class TestFindLatestAttachedImagePath:
    """Tests for scanning messages newest-first for an image path."""

    def test_finds_latest_in_last_message(self, tmp_path):
        f = tmp_path / "img.png"
        f.write_text("fake image")
        messages = [
            {"role": "assistant", "content": f"[Image attached at: {f}]"},
        ]
        assert _find_latest_attached_image_path(messages) == str(f)

    def test_finds_latest_across_multiple_messages(self, tmp_path):
        f1 = tmp_path / "old.png"
        f2 = tmp_path / "new.png"
        f1.write_text("old"); f2.write_text("new")
        messages = [
            {"role": "assistant", "content": f"[Image attached at: {f1}]"},
            {"role": "assistant", "content": f"[Image attached at: {f2}]"},
        ]
        # newest-first: f2 before f1
        assert _find_latest_attached_image_path(messages) == str(f2)

    def test_returns_none_when_no_image_marker(self):
        messages = [
            {"role": "assistant", "content": "hello, no image here"},
        ]
        assert _find_latest_attached_image_path(messages) is None

    def test_returns_none_when_path_does_not_exist(self):
        messages = [
            {"role": "assistant", "content": "[Image attached at: /nonexistent/nope.png]"},
        ]
        assert _find_latest_attached_image_path(messages) is None

    def test_returns_none_for_empty_messages(self):
        assert _find_latest_attached_image_path([]) is None

    def test_handles_message_without_content_key(self):
        messages = [{"role": "user"}]
        assert _find_latest_attached_image_path(messages) is None

    def test_skips_nonexistent_paths_and_returns_real_one(self, tmp_path):
        real = tmp_path / "real.png"
        real.write_text("real")
        messages = [
            {"role": "assistant",
             "content": f"[Image attached at: /nonexistent/fake.png] [Image attached at: {real}]"},
        ]
        assert _find_latest_attached_image_path(messages) == str(real)

    def test_newest_message_wins_even_if_path_removed(self, tmp_path):
        """If the newest message has a reference to a missing file, we skip it
        and fall through to the previous one."""
        old = tmp_path / "old.png"
        old.write_text("old")
        messages = [
            {"role": "assistant", "content": f"[Image attached at: {old}]"},
            {"role": "assistant", "content": "[Image attached at: /removed.png]"},
        ]
        # /removed.png doesn't exist → skip, fall through to old.png
        assert _find_latest_attached_image_path(messages) == str(old)


# ── Sentinel ─────────────────────────────────────────────────────────────────

def test_sentinel_is_unique_object():
    assert _RECENT_IMAGE_RESEND_NOT_HANDLED is not None
    assert _RECENT_IMAGE_RESEND_NOT_HANDLED != object()