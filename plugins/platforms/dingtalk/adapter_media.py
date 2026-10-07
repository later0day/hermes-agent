"""DingTalk adapter: media in both directions: outbound image/file/video/voice sends and robot uploads, inbound
download-code resolution and caching, and the media-shape helpers both sides share.

Mixed into :class:`~plugins.platforms.dingtalk.adapter.DingTalkAdapter`; SDK modules are read through
the adapter module (``_dt()``) so the facade stays the one patch seam."""

import asyncio
import logging
import mimetypes
import os
import struct
import subprocess
import tempfile
import uuid
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from gateway.platforms.base import (
    SendResult,
    _ssrf_redirect_guard,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_video_from_bytes,
    safe_url_for_log,
)
from plugins.platforms.dingtalk.adapter_cards import DEFAULT_AI_CARD_TEMPLATE_ID
from plugins.platforms.dingtalk.inbound import collect_download_codes, _rich_list

if TYPE_CHECKING:
    from dingtalk_stream import ChatbotMessage


logger = logging.getLogger(__name__)

# DingTalk media upload endpoint (robot native media messages need a temporary media_id).
_DINGTALK_MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"
# File extensions natively supported by DingTalk's sampleVideo / sampleAudio robot messages.
_DINGTALK_NATIVE_AUDIO_EXTS = {"ogg", "amr"}
_DINGTALK_NATIVE_VIDEO_EXTS = {"mp4"}


def _dt():
    from plugins.platforms.dingtalk import adapter
    return adapter


class DingTalkMediaMixin:
    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata=None) -> SendResult:
        """Render a remote image inline via markdown (session webhook has no native attachments)."""
        image_block = f"![image]({image_url})"
        return await self.send(chat_id=chat_id, content=f"{caption}\n\n{image_block}" if caption else image_block, reply_to=reply_to, metadata=metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, metadata=None, **kwargs) -> SendResult:
        """Upload a local image and send it via DingTalk's card_1_0 image delivery path."""
        if image_path.startswith(("http://", "https://")):
            return await self.send_image(chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata)
        path = Path(image_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {image_path}")
        upload = await self._upload_robot_media(str(path), media_type="image")
        if not upload.success:
            return upload
        return await self._send_robot_card_1_0_image(
            chat_id, upload.message_id, caption=caption, metadata=metadata,
        )

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None, reply_to=None, metadata=None, **kwargs) -> SendResult:
        """Upload a local file and send it as a DingTalk sampleFile robot message."""
        path = Path(file_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {file_path}")
        metadata = metadata or {}
        upload = await self._upload_robot_media(str(path), media_type="file")
        if not upload.success:
            return upload
        return await self._send_robot_native_message(
            chat_id=chat_id,
            msg_key="sampleFile",
            msg_param={"mediaId": upload.message_id, "fileName": file_name or path.name},
            metadata=metadata,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send local MP4 as native DingTalk video, falling back to file."""
        if video_path.startswith(("http://", "https://")):
            label = os.path.basename(video_path) or "video"
            link = f"[{label}]({video_path})"
            content = f"{caption}\n\n{link}" if caption else link
            return await self.send(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
            )

        path = Path(video_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {video_path}")

        metadata = metadata or {}
        ext = path.suffix.lstrip(".").lower()
        if ext in _DINGTALK_NATIVE_VIDEO_EXTS:
            if not self._looks_like_mp4(path):
                return await self.send_document(
                    chat_id=chat_id,
                    file_path=video_path,
                    caption=caption,
                    file_name=os.path.basename(video_path) or "video",
                    reply_to=reply_to,
                    metadata=metadata,
                )

            cover_path = self._metadata_path(
                metadata,
                "dingtalk_video_cover_path",
                "video_cover_path",
                "thumbnail_path",
            )
            generated_cover = False
            if not cover_path:
                cover_path = await asyncio.to_thread(self._generate_video_cover, path)
                generated_cover = bool(cover_path)
            if not cover_path:
                cover_path = self._write_default_video_cover()
                generated_cover = bool(cover_path)

            if cover_path:
                try:
                    video_media = await self._upload_robot_media(str(path), media_type="video")
                    cover_media = await self._upload_robot_media(str(cover_path), media_type="image")
                    if video_media.success and cover_media.success:
                        duration_ms = self._duration_ms_from_metadata(metadata)
                        if not duration_ms:
                            duration_ms = await asyncio.to_thread(self._probe_media_duration_ms, path)
                        duration_ms = duration_ms or 1000
                        native_result = await self._send_robot_native_message(
                            chat_id=chat_id,
                            msg_key="sampleVideo",
                            msg_param={
                                "videoMediaId": video_media.message_id,
                                "videoType": ext,
                                "picMediaId": cover_media.message_id,
                                "duration": str(duration_ms),
                            },
                            metadata=metadata,
                        )
                        if native_result.success:
                            if caption:
                                await self.send(
                                    chat_id=chat_id,
                                    content=caption,
                                    reply_to=reply_to,
                                    metadata=metadata,
                                )
                            return native_result
                finally:
                    if generated_cover:
                        try:
                            cover_path.unlink(missing_ok=True)
                        except Exception:
                            pass

        return await self.send_document(
            chat_id=chat_id,
            file_path=video_path,
            caption=caption,
            file_name=os.path.basename(video_path) or "video",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send OGG/AMR as native DingTalk audio, falling back to file."""
        if audio_path.startswith(("http://", "https://")):
            label = os.path.basename(audio_path) or "audio"
            link = f"[{label}]({audio_path})"
            content = f"{caption}\n\n{link}" if caption else link
            return await self.send(
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata=metadata,
            )

        path = Path(audio_path)
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {audio_path}")

        metadata = metadata or {}
        ext = path.suffix.lstrip(".").lower()
        if ext in _DINGTALK_NATIVE_AUDIO_EXTS:
            if not self._looks_like_native_audio(path, ext):
                return await self.send_document(
                    chat_id=chat_id,
                    file_path=audio_path,
                    caption=caption,
                    file_name=os.path.basename(audio_path) or "audio",
                    reply_to=reply_to,
                    metadata=metadata,
                )

            audio_media = await self._upload_robot_media(str(path), media_type="voice")
            if audio_media.success:
                duration_ms = self._duration_ms_from_metadata(metadata)
                if not duration_ms:
                    duration_ms = await asyncio.to_thread(self._probe_media_duration_ms, path)
                duration_ms = duration_ms or 1000
                native_result = await self._send_robot_native_message(
                    chat_id=chat_id,
                    msg_key="sampleAudio",
                    msg_param={
                        "mediaId": audio_media.message_id,
                        "duration": str(duration_ms),
                    },
                    metadata=metadata,
                )
                if native_result.success:
                    if caption:
                        await self.send(
                            chat_id=chat_id,
                            content=caption,
                            reply_to=reply_to,
                            metadata=metadata,
                        )
                    return native_result

        return await self.send_document(
            chat_id=chat_id,
            file_path=audio_path,
            caption=caption,
            file_name=os.path.basename(audio_path) or "audio",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def _upload_robot_media(self, file_path: str, media_type: str) -> SendResult:
        """Upload a local file to DingTalk's robot media endpoint for a temporary ``media_id``."""
        path = Path(file_path).expanduser()
        if not path.is_file():
            return SendResult(success=False, error=f"Local file not found: {file_path}")
        if media_type not in {"image", "file", "voice", "video"}:
            return SendResult(success=False, error=f"Unsupported DingTalk media type: {media_type}")
        if not self._http_client:
            return SendResult(success=False, error="HTTP client not initialized")

        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="DingTalk access token unavailable")

        # DingTalk's robot media upload rejects browser-renderable MIME types
        # such as text/html with errcode 40005, even though the same bytes are
        # accepted as a generic file. Upload file attachments as opaque bytes
        # so valid extensions are not blocked by Content-Type sniffing.
        if media_type == "file":
            mime_type = "application/octet-stream"
        else:
            mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        try:
            with path.open("rb") as fh:
                response = await self._http_client.post(
                    _DINGTALK_MEDIA_UPLOAD_URL,
                    params={"access_token": token, "type": media_type},
                    files={"media": (path.name, fh, mime_type)},
                    timeout=60.0,
                )
            try:
                body = response.json()
            except Exception:
                body = {}
            if response.status_code >= 300:
                return SendResult(
                    success=False,
                    error=f"DingTalk media upload failed HTTP {response.status_code}: {response.text[:200]}",
                )
            errcode = body.get("errcode", 0)
            if errcode not in (0, "0", None):
                errmsg = body.get("errmsg") or body.get("message") or "unknown error"
                return SendResult(
                    success=False,
                    error=f"DingTalk media upload failed: {errcode} {errmsg}",
                    raw_response=body,
                )
            media_id = body.get("media_id") or body.get("mediaId")
            if not media_id:
                return SendResult(
                    success=False,
                    error="DingTalk media upload failed: missing media_id",
                    raw_response=body,
                )
            logger.info(
                "[%s] DingTalk media uploaded: type=%s file=%s",
                self.name,
                media_type,
                path.name,
            )
            return SendResult(success=True, message_id=str(media_id), raw_response=body)
        except Exception as exc:
            logger.warning("[%s] DingTalk media upload failed: %s", self.name, exc)
            return SendResult(success=False, error=f"DingTalk media upload failed: {exc}")

    async def _send_robot_card_1_0_image(
        self,
        chat_id: str,
        media_id: str,
        *,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an uploaded image through DingTalk's card_1_0 delivery path for DM images."""
        metadata = metadata or {}
        if not self._card_sdk or not _dt().dingtalk_card_models or not _dt().tea_util_models:
            return SendResult(success=False, error="DingTalk card SDK is unavailable")

        current_message = self._message_contexts.get(chat_id)
        open_conversation_id = (
            metadata.get("dingtalk_open_conversation_id")
            or metadata.get("open_conversation_id")
            or chat_id
        )
        conversation_type = (
            metadata.get("dingtalk_conversation_type")
            or metadata.get("conversation_type")
            or getattr(current_message, "conversation_type", None)
        )
        sender_staff_id = (
            metadata.get("dingtalk_sender_staff_id")
            or metadata.get("sender_staff_id")
            or getattr(current_message, "sender_staff_id", None)
        )
        robot_code = (
            metadata.get("dingtalk_robot_code")
            or metadata.get("robot_code")
            or self._robot_code
            or getattr(current_message, "robot_code", None)
        )
        if not robot_code:
            return SendResult(success=False, error="DingTalk robotCode is unavailable")

        token = await self._get_access_token()
        if not token:
            return SendResult(success=False, error="DingTalk access token unavailable")

        out_track_id = f"hermes_img_{uuid.uuid4().hex[:12]}"
        runtime = _dt().tea_util_models.RuntimeOptions()
        route = "card_1_0_image"
        try:
            create_request = _dt().dingtalk_card_models.CreateCardRequest(
                card_template_id=DEFAULT_AI_CARD_TEMPLATE_ID,
                out_track_id=out_track_id,
                card_data=_dt().dingtalk_card_models.CreateCardRequestCardData(
                    card_param_map=self._image_card_param_map(media_id, caption),
                ),
                callback_type="STREAM",
                im_group_open_space_model=(
                    _dt().dingtalk_card_models.CreateCardRequestImGroupOpenSpaceModel(
                        support_forward=True,
                    )
                ),
                im_robot_open_space_model=(
                    _dt().dingtalk_card_models.CreateCardRequestImRobotOpenSpaceModel(
                        support_forward=True,
                    )
                ),
            )
            create_headers = _dt().dingtalk_card_models.CreateCardHeaders(
                x_acs_dingtalk_access_token=token,
            )
            await self._card_sdk.create_card_with_options_async(
                create_request, create_headers, runtime
            )

            route = "card_1_0_image_group"
            if str(conversation_type) == "1":
                if not sender_staff_id:
                    return SendResult(success=False, error="DingTalk sender_staff_id is unavailable")
                route = "card_1_0_image_single"
                deliver_request = _dt().dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=f"dtv1.card//IM_ROBOT.{sender_staff_id}",
                    im_robot_open_deliver_model=(
                        _dt().dingtalk_card_models.DeliverCardRequestImRobotOpenDeliverModel(
                            space_type="IM_ROBOT",
                        )
                    ),
                )
            else:
                if not open_conversation_id:
                    return SendResult(success=False, error="DingTalk openConversationId is unavailable")
                deliver_request = _dt().dingtalk_card_models.DeliverCardRequest(
                    out_track_id=out_track_id,
                    user_id_type=1,
                    open_space_id=f"dtv1.card//IM_GROUP.{open_conversation_id}",
                    im_group_open_deliver_model=(
                        _dt().dingtalk_card_models.DeliverCardRequestImGroupOpenDeliverModel(
                            robot_code=str(robot_code),
                        )
                    ),
                )
            deliver_headers = _dt().dingtalk_card_models.DeliverCardHeaders(
                x_acs_dingtalk_access_token=token,
            )
            await self._card_sdk.deliver_card_with_options_async(
                deliver_request, deliver_headers, runtime
            )

            logger.info(
                "[%s] DingTalk card_1_0 image sent: chat=%s route=%s",
                self.name,
                str(open_conversation_id)[:20],
                route,
            )
            return SendResult(
                success=True,
                message_id=out_track_id,
            )
        except Exception as exc:
            logger.warning(
                "[%s] DingTalk card_1_0 image failed: %s "
                "(chat=%s route=%s)",
                self.name,
                exc,
                str(open_conversation_id)[:20],
                route,
            )
            return SendResult(success=False, error=f"DingTalk card_1_0 image failed: {exc}")

    async def _cache_media_url(
        self,
        url: str,
        mapped: str,
        filename: Optional[str] = None,
    ) -> tuple[str, str]:
        """Download a media URL into the existing Hermes media caches."""
        if not _dt().HTTPX_AVAILABLE or _dt().httpx is None:
            raise RuntimeError("httpx is required to download DingTalk media")

        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(
                f"Blocked unsafe DingTalk media URL: {safe_url_for_log(url)}"
            )

        accept = {
            "image": "image/*,*/*;q=0.8",
            "audio": "audio/*,*/*;q=0.8",
            "video": "video/*,*/*;q=0.8",
        }.get(mapped, "application/octet-stream,*/*;q=0.8")
        async with _dt().httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            event_hooks={"response": [_ssrf_redirect_guard]},
            trust_env=False,
        ) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                    "Accept": accept,
                },
            )
            response.raise_for_status()

        response_type = response.headers.get("content-type", "")
        response_type = response_type.split(";", 1)[0].strip().lower()
        media_type = response_type or self._default_media_type(mapped, filename)
        if media_type == "application/octet-stream" and filename:
            media_type = self._default_media_type(mapped, filename)
        ext = self._extension_for_media(mapped, media_type, filename)

        if mapped == "image":
            return cache_image_from_bytes(response.content, ext), media_type
        if mapped == "audio":
            return cache_audio_from_bytes(response.content, ext), media_type
        if mapped == "video":
            return cache_video_from_bytes(response.content, ext), media_type

        doc_name = filename or f"dingtalk_attachment{ext}"
        return cache_document_from_bytes(response.content, doc_name), media_type

    async def _cache_resolved_media_url(
        self,
        url: str,
        obj: Any,
        key: str,
        mapped: str,
        filename: Optional[str] = None,
    ) -> None:
        """Cache a resolved DingTalk media URL and mutate the source object."""
        try:
            path, media_type = await self._cache_media_url(url, mapped, filename)
        except Exception as exc:
            self._set_media_error(
                obj,
                f"DingTalk media download failed: {exc}",
            )
            logger.warning(
                "[%s] Failed to cache DingTalk media %s; skipping media routing: %s",
                self.name,
                safe_url_for_log(url),
                exc,
            )
            return
        self._set_cached_media_ref(obj, key, path, media_type, filename)

    async def _resolve_media_codes(self, message: "ChatbotMessage") -> None:
        """Resolve download codes in the message to real URLs (in place, in parallel)."""
        token = await self._get_access_token()
        if not token:
            return
        robot_code = getattr(message, "robot_code", None) or self._client_id
        pairs = [(getattr(obj, key, None) if hasattr(obj, key) else obj.get(key), obj, key) for obj, key in collect_download_codes(message)]
        tasks = [self._fetch_download_url(code, robot_code, token, obj, key) for code, obj, key in pairs if code]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_download_url(self, code: str, robot_code: str, token: str, obj, key: str) -> None:
        """Fetch the download URL for one code via the robot SDK and write it back to ``obj[key]``."""
        if not self._robot_sdk:
            return logger.warning("[%s] Robot SDK not initialized, cannot resolve media code", self.name)
        try:
            response = await self._sdk_call(self._robot_sdk.robot_message_file_download_with_options_async,
                                            _dt().dingtalk_robot_models.RobotMessageFileDownloadRequest(download_code=code, robot_code=robot_code),
                                            _dt().dingtalk_robot_models.RobotMessageFileDownloadHeaders, token)
            body = response.body if response else None
            url = getattr(body, "download_url", None) if body else None
            if not body:
                logger.warning("[%s] Failed to download media: empty response for code %s", self.name, code)
            elif url and hasattr(obj, key):
                setattr(obj, key, url)
            elif url and isinstance(obj, dict):
                obj[key] = url
        except Exception as e:
            logger.error("[%s] Error resolving media code %s: %s", self.name, code, e)

    @staticmethod
    def _duration_ms_from_metadata(metadata: Dict[str, Any]) -> Optional[int]:
        """Read a media duration in ms from metadata (ms keys first, then seconds keys)."""
        for key in ("dingtalk_duration_ms", "duration_ms"):
            raw = metadata.get(key)
            if raw is None:
                continue
            try:
                value = int(float(str(raw)))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value

        for key in ("dingtalk_duration_seconds", "duration_seconds", "duration"):
            raw = metadata.get(key)
            if raw is None:
                continue
            try:
                value = int(float(str(raw)) * 1000)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _probe_media_duration_ms(path: Path) -> Optional[int]:
        """Probe media duration via ffprobe (best-effort; None on any failure)."""
        from tools.transcription_audio import _find_ffprobe_binary

        ffprobe = _find_ffprobe_binary()
        if not ffprobe:
            return None
        try:
            result = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            seconds = float((result.stdout or "").strip())
            duration_ms = int(seconds * 1000)
            return duration_ms if duration_ms > 0 else None
        except Exception:
            return None

    @staticmethod
    def _looks_like_mp4(path: Path) -> bool:
        """Quick magic-byte check: MP4 files contain ``ftyp`` in the first 16 bytes."""
        try:
            with path.open("rb") as fh:
                header = fh.read(32)
        except Exception:
            return False
        return len(header) >= 12 and b"ftyp" in header[:16]

    @staticmethod
    def _looks_like_native_audio(path: Path, ext: str) -> bool:
        """Quick magic-byte check for OGG / AMR headers DingTalk expects."""
        try:
            with path.open("rb") as fh:
                header = fh.read(16)
        except Exception:
            return False
        if ext == "ogg":
            return header.startswith(b"OggS")
        if ext == "amr":
            return header.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
        return False

    @staticmethod
    def _generate_video_cover(path: Path) -> Optional[Path]:
        """Generate a JPEG thumbnail from the first frame via ffmpeg (best-effort)."""
        from tools.transcription_audio import _find_ffmpeg_binary

        ffmpeg = _find_ffmpeg_binary()
        if not ffmpeg:
            return None
        output = Path(tempfile.gettempdir()) / f"hermes_dingtalk_video_{uuid.uuid4().hex}.jpg"
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(output),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=10,
            )
            if result.returncode == 0 and output.is_file() and output.stat().st_size > 0:
                return output
        except Exception:
            pass
        try:
            output.unlink(missing_ok=True)
        except Exception:
            pass
        return None

    @staticmethod
    def _write_default_video_cover() -> Optional[Path]:
        """Write a minimal dark-gray 320x180 PNG as a fallback video cover."""
        output = Path(tempfile.gettempdir()) / f"hermes_dingtalk_video_cover_{uuid.uuid4().hex}.png"
        try:
            width, height = 320, 180
            row = b"\x00" + (b"\x1f\x1f\x1f" * width)
            raw = row * height

            def chunk(kind: bytes, data: bytes) -> bytes:
                return (
                    struct.pack(">I", len(data))
                    + kind
                    + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
                )

            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw))
                + chunk(b"IEND", b"")
            )
            output.write_bytes(png)
            return output
        except Exception:
            try:
                output.unlink(missing_ok=True)
            except Exception:
                pass
            return None

    @staticmethod
    def _media_get(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        value = getattr(obj, key, default)
        return default if value is None else value

    @staticmethod
    def _media_set(obj: Any, key: str, value: Any) -> None:
        if isinstance(obj, dict):
            obj[key] = value
            return
        try:
            setattr(obj, key, value)
        except Exception:
            logger.debug("Failed to set DingTalk media field %s", key, exc_info=True)

    @classmethod
    def _first_media_ref(cls, item: Any) -> tuple[Optional[str], Optional[str], bool]:
        """Return ``(ref, key, is_download_code)`` for a DingTalk media item."""
        for key in cls._MEDIA_CODE_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value), key, True
        for key in cls._MEDIA_URL_KEYS:
            value = cls._media_get(item, key)
            if value:
                return str(value), key, False
        return None, None, False

    @staticmethod
    def _default_media_type(mapped: str, filename: Optional[str] = None) -> str:
        if filename:
            guessed, _ = mimetypes.guess_type(filename)
            if guessed:
                return guessed
        if mapped == "image":
            return "image/jpeg"
        if mapped == "audio":
            return "audio/ogg"
        if mapped == "video":
            return "video/mp4"
        return "application/octet-stream"

    @classmethod
    def _extension_for_media(
        cls,
        mapped: str,
        media_type: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        if filename:
            ext = Path(filename).suffix
            if ext:
                return ext
        if media_type:
            ext = mimetypes.guess_extension(media_type.split(";", 1)[0].strip())
            if ext:
                return ".jpg" if ext == ".jpe" else ext
        if mapped == "image":
            return ".jpg"
        if mapped == "audio":
            return ".ogg"
        if mapped == "video":
            return ".mp4"
        return ".bin"

    @classmethod
    def _media_type_for_item(
        cls,
        item: Any,
        mapped: str,
        filename: Optional[str] = None,
    ) -> str:
        explicit = cls._media_get(item, "_hermes_media_type")
        if explicit:
            return str(explicit)
        return cls._default_media_type(mapped, filename)

    @classmethod
    def _set_cached_media_ref(
        cls,
        obj: Any,
        key: str,
        value: str,
        media_type: str,
        filename: Optional[str],
    ) -> None:
        cls._media_set(obj, key, value)
        if isinstance(obj, dict):
            obj["_hermes_media_type"] = media_type
            if filename:
                obj["_hermes_file_name"] = filename
            return
        try:
            setattr(obj, "_hermes_media_type", media_type)
            if filename:
                setattr(obj, "_hermes_file_name", filename)
        except Exception:
            logger.debug("Failed to attach DingTalk media metadata", exc_info=True)

    @classmethod
    def _set_media_error(cls, obj: Any, message: str) -> None:
        if isinstance(obj, dict):
            obj["_hermes_media_error"] = message
            return
        try:
            setattr(obj, "_hermes_media_error", message)
        except Exception:
            logger.debug("Failed to attach DingTalk media error", exc_info=True)

    @classmethod
    def _media_error_for_item(cls, item: Any) -> Optional[str]:
        value = cls._media_get(item, "_hermes_media_error")
        return str(value) if value else None

    def _extract_media_errors(self, message: "ChatbotMessage") -> List[str]:
        """Collect per-attachment media-resolution failures attached by ``_resolve_media_codes``."""
        errors: List[str] = []
        image_content = getattr(message, "image_content", None)
        if image_content:
            error = self._media_error_for_item(image_content)
            if error:
                errors.append(error)
        for item in _rich_list(message) or ():
            error = self._media_error_for_item(item)
            if error:
                errors.append(error)
        return errors
