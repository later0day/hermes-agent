"""Tests for DingTalk adapter pure helper functions.

Tests the stateless helpers that don't require a live gateway or network:
_metadata_values, _metadata_bool, _first_raw_value, _fill_missing_raw_fields,
_rich_item_type, _rich_item_filename, _extract_emotion_tags, _set_media_error,
_media_error_for_item, _default_media_type, _looks_like_mp4, _looks_like_native_audio.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from plugins.platforms.dingtalk.adapter import DingTalkAdapter


# ── Helpers to construct test objects ────────────────────────────────────────

def _obj(**fields):
    """Minimal dict-like with attribute access, like the DingTalk SDK objects."""
    class _O:
        pass
    o = _O()
    for k, v in fields.items():
        setattr(o, k, v)
    return o


# ── _metadata_values ─────────────────────────────────────────────────────────

class TestMetadataValues:
    def test_single_key_string(self):
        assert DingTalkAdapter._metadata_values({"a": "hello"}, "a") == ["hello"]

    def test_multiple_keys_first_wins(self):
        assert DingTalkAdapter._metadata_values({"b": "world"}, "a", "b") == ["world"]

    def test_comma_separated_splits(self):
        assert DingTalkAdapter._metadata_values({"u": "u1,u2,u3"}, "u") == ["u1", "u2", "u3"]

    def test_list_value(self):
        assert DingTalkAdapter._metadata_values({"u": ["u1", "u2"]}, "u") == ["u1", "u2"]

    def test_strips_whitespace(self):
        assert DingTalkAdapter._metadata_values({"u": " a , b , c "}, "u") == ["a", "b", "c"]

    def test_skips_none_keys(self):
        assert DingTalkAdapter._metadata_values({"x": "val"}, "missing") == []

    def test_skips_empty_strings_in_results(self):
        assert DingTalkAdapter._metadata_values({"u": ",,a,,"}, "u") == ["a"]


# ── _metadata_bool ───────────────────────────────────────────────────────────

class TestMetadataBool:
    def test_true_strings(self):
        for v in ("true", "True", "TRUE", "1", "yes", "on"):
            assert DingTalkAdapter._metadata_bool({"f": v}, "f") is True

    def test_false_strings(self):
        for v in ("false", "False", "0", "no", "off", "anything"):
            assert DingTalkAdapter._metadata_bool({"f": v}, "f") is False

    def test_none_key(self):
        assert DingTalkAdapter._metadata_bool({}, "missing") is False

    def test_non_string_values(self):
        assert DingTalkAdapter._metadata_bool({"f": 1}, "f") is True
        assert DingTalkAdapter._metadata_bool({"f": 0}, "f") is False
        assert DingTalkAdapter._metadata_bool({"f": True}, "f") is True

    def test_first_key_wins(self):
        assert DingTalkAdapter._metadata_bool({"x": True, "y": False}, "x", "y") is True
        assert DingTalkAdapter._metadata_bool({"x": None, "y": True}, "x", "y") is True


# ── _first_raw_value ─────────────────────────────────────────────────────────

class TestFirstRawValue:
    def test_returns_first_non_empty(self):
        assert DingTalkAdapter._first_raw_value({"a": "1", "b": "2"}, "a", "b") == "1"

    def test_skips_none_and_empty(self):
        assert DingTalkAdapter._first_raw_value({"a": None, "b": "", "c": "got"}, "a", "b", "c") == "got"

    def test_returns_none_when_all_missing(self):
        assert DingTalkAdapter._first_raw_value({}, "a", "b") is None


# ── _fill_missing_raw_fields ─────────────────────────────────────────────────

class TestFillMissingRawFields:
    def test_backfills_message_id_from_raw_dict(self):
        msg = _obj(message_id=None, conversation_id=None)
        DingTalkAdapter._fill_missing_raw_fields(msg, {"msgId": "abc123"})
        assert msg.message_id == "abc123"

    def test_does_not_overwrite_existing(self):
        msg = _obj(message_id="existing")
        DingTalkAdapter._fill_missing_raw_fields(msg, {"msgId": "new"})
        assert msg.message_id == "existing"

    def test_backfills_multiple_fields(self):
        msg = _obj(message_id=None, conversation_id=None, sender_id=None)
        DingTalkAdapter._fill_missing_raw_fields(msg, {
            "msgId": "m1",
            "conversationId": "c1",
            "senderId": "s1",
        })
        assert msg.message_id == "m1"
        assert msg.conversation_id == "c1"
        assert msg.sender_id == "s1"


# ── _rich_item_type / _rich_item_filename ────────────────────────────────────

class TestRichItemHelpers:
    def test_item_type_from_type_key(self):
        item = {"type": "image", "msgtype": "text"}
        assert DingTalkAdapter._rich_item_type(item) == "image"

    def test_item_type_falls_back_to_msgtype(self):
        item = {"msgtype": "voice"}
        assert DingTalkAdapter._rich_item_type(item) == "voice"

    def test_item_type_returns_empty_for_missing(self):
        assert DingTalkAdapter._rich_item_type({}) == ""

    def test_item_type_case_insensitive(self):
        assert DingTalkAdapter._rich_item_type({"type": "IMAGE"}) == "image"

    def test_item_filename_from_filename_key(self):
        assert DingTalkAdapter._rich_item_filename({"fileName": "photo.png"}) == "photo.png"

    def test_item_filename_from_file_name_key(self):
        assert DingTalkAdapter._rich_item_filename({"file_name": "photo.jpg"}) == "photo.jpg"

    def test_item_filename_strips_path(self):
        assert (
            DingTalkAdapter._rich_item_filename({"fileName": "/some/path/video.mp4"})
            == "video.mp4"
        )

    def test_item_filename_returns_none_when_missing(self):
        assert DingTalkAdapter._rich_item_filename({}) is None


# ── _extract_emotion_tags ────────────────────────────────────────────────────

class TestExtractEmotionTags:
    def test_single_emotion(self):
        content, emotions = DingTalkAdapter._extract_emotion_tags(
            "hello [[emotion:smile]] world"
        )
        assert content == "hello  world"
        assert emotions == ["smile"]

    def test_multiple_emotions(self):
        content, emotions = DingTalkAdapter._extract_emotion_tags(
            "[[emotion:laugh]] so funny [[emotion:heart]]"
        )
        assert "laugh" in emotions and "heart" in emotions
        assert len(emotions) == 2
        assert content.strip() == "so funny"

    def test_trims_emotion_name_whitespace(self):
        content, emotions = DingTalkAdapter._extract_emotion_tags(
            "[[emotion: big grin ]] haha"
        )
        assert emotions == ["big grin"]

    def test_empty_input(self):
        assert DingTalkAdapter._extract_emotion_tags("") == ("", [])
        assert DingTalkAdapter._extract_emotion_tags(None) == (None, [])  # type: ignore[arg-type]

    def test_no_emotion_tags(self):
        content, emotions = DingTalkAdapter._extract_emotion_tags("plain text")
        assert content == "plain text"
        assert emotions == []

    def test_caps_emotion_name_at_64_chars(self):
        long_name = "a" * 100
        content, emotions = DingTalkAdapter._extract_emotion_tags(
            f"[[emotion:{long_name}]]"
        )
        assert len(emotions[0]) == 64


# ── _set_media_error / _media_error_for_item ─────────────────────────────────

class TestMediaError:
    def test_set_media_error_on_dict(self):
        item = {}
        DingTalkAdapter._set_media_error(item, "unsupported format")
        assert DingTalkAdapter._media_error_for_item(item) == "unsupported format"

    def test_set_media_error_on_object(self):
        item = _obj()
        DingTalkAdapter._set_media_error(item, "network timeout")
        assert DingTalkAdapter._media_error_for_item(item) == "network timeout"

    def test_media_error_for_item_returns_none_when_no_error(self):
        assert DingTalkAdapter._media_error_for_item({}) is None

    def test_media_error_for_item_handles_non_dict_non_object(self):
        assert DingTalkAdapter._media_error_for_item("not an item") is None


# ── _default_media_type ──────────────────────────────────────────────────────

class TestDefaultMediaType:
    def test_mimetype_from_filename(self):
        assert DingTalkAdapter._default_media_type("image", "photo.jpg") == "image/jpeg"
        assert DingTalkAdapter._default_media_type("audio", "sound.mp3") == "audio/mpeg"

    def test_mapped_fallback_when_no_filename(self):
        assert DingTalkAdapter._default_media_type("image") == "image/jpeg"
        assert DingTalkAdapter._default_media_type("audio") == "audio/ogg"
        assert DingTalkAdapter._default_media_type("video") == "video/mp4"

    def test_unknown_type_octet_stream(self):
        assert DingTalkAdapter._default_media_type("unknown") == "application/octet-stream"


# ── _looks_like_mp4 ──────────────────────────────────────────────────────────

class TestLooksLikeMp4:
    def test_real_mp4_magic(self, tmp_path):
        f = tmp_path / "test.mp4"
        # Minimal valid MP4 header: ftyp box
        import struct
        f.write_bytes(
            b"\x00\x00\x00\x18"  # box size
            b"ftyp"              # box type
            b"mp42"              # major brand
            b"\x00\x00\x00\x00"  # minor version
            b"mp42"              # compatible brand
        )
        assert DingTalkAdapter._looks_like_mp4(f) is True

    def test_text_file_not_mp4(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("not an mp4 file")
        assert DingTalkAdapter._looks_like_mp4(f) is False

    def test_short_file(self, tmp_path):
        f = tmp_path / "tiny"
        f.write_bytes(b"short")
        assert DingTalkAdapter._looks_like_mp4(f) is False

    def test_nonexistent_file(self):
        assert DingTalkAdapter._looks_like_mp4(Path("/nonexistent/file.mp4")) is False


# ── _looks_like_native_audio ─────────────────────────────────────────────────

class TestLooksLikeNativeAudio:
    def test_ogg_magic(self, tmp_path):
        f = tmp_path / "test.ogg"
        f.write_bytes(b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00")
        assert DingTalkAdapter._looks_like_native_audio(f, "ogg") is True

    def test_not_ogg_with_wrong_header(self, tmp_path):
        f = tmp_path / "test.ogg"
        f.write_bytes(b"RIFFxxxxWAVE")
        assert DingTalkAdapter._looks_like_native_audio(f, "ogg") is False

    def test_amr_magic(self, tmp_path):
        f = tmp_path / "test.amr"
        f.write_bytes(b"#!AMR\n.....")
        assert DingTalkAdapter._looks_like_native_audio(f, "amr") is True

    def test_amr_wb_magic(self, tmp_path):
        f = tmp_path / "test.amr"
        f.write_bytes(b"#!AMR-WB\n...")
        assert DingTalkAdapter._looks_like_native_audio(f, "amr") is True

    def test_unknown_audio_extension(self, tmp_path):
        f = tmp_path / "test.flac"
        f.write_bytes(b"fLaC\x00\x00\x00")
        assert DingTalkAdapter._looks_like_native_audio(f, "flac") is False

    def test_nonexistent_audio_file(self):
        assert (
            DingTalkAdapter._looks_like_native_audio(Path("/nonexistent/test.ogg"), "ogg")
            is False
        )