from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from http.cookies import SimpleCookie
from datetime import UTC, datetime
from pathlib import Path

import httpx
from yt_dlp import DownloadError, YoutubeDL

from creator_report.adapters.base import CreatorAdapter
from creator_report.config import Settings
from creator_report.models import ChannelProfile, TranscriptChunk, VideoRecord


class ChargingVideoError(RuntimeError):
    """Bilibili upower-exclusive (充电专属) video that cannot be fetched or transcribed."""


class BilibiliAdapter(CreatorAdapter):
    platform = "bilibili"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    async def resolve_channel(self, source_url: str) -> ChannelProfile:
        uid = self._extract_uid(source_url)
        owner = await self._fetch_channel_card(uid)
        return ChannelProfile(
            source_id=f"bilibili:{uid}",
            platform=self.platform,
            title=owner.get("name") or f"Bilibili UP {uid}",
            description=owner.get("sign") or "",
            source_url=source_url,
            avatar_url=owner.get("face"),
        )

    async def list_videos(
        self,
        source_url: str,
        *,
        limit: int = 50,
    ) -> list[VideoRecord]:
        try:
            info = await asyncio.to_thread(
                self._extract_space_info,
                source_url,
                limit,
            )
        except DownloadError as exc:
            message = str(exc)
            if "412" in message or "blocked by server" in message.lower():
                raise RuntimeError(self._blocked_message()) from exc
            raise RuntimeError(f"Bilibili channel fetch failed: {message}") from exc

        videos: list[VideoRecord] = []
        for entry in info.get("entries") or []:
            bvid = entry.get("id")
            if not bvid:
                continue
            published_at = (
                datetime.fromtimestamp(entry.get("timestamp"), tz=UTC) if entry.get("timestamp") else None
            )
            video = VideoRecord(
                video_id=bvid,
                platform=self.platform,
                source_url=(entry.get("url") or f"https://www.bilibili.com/video/{bvid}").rstrip("/") + "/",
                title=entry.get("title") or bvid,
                description=entry.get("description") or "",
                published_at=published_at,
                duration_seconds=entry.get("duration"),
            )
            videos.append(video)
        return videos

    async def _fetch_channel_card(self, uid: str) -> dict:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            cookies=self._cookie_jar(),
            headers=self._base_headers(),
        ) as client:
            response = await client.get(
                "https://api.bilibili.com/x/web-interface/card",
                params={"mid": uid, "photo": "true"},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError(
                f"Bilibili channel profile failed ({payload.get('code')}): "
                f"{payload.get('message') or 'unknown error'}"
            )
        card = (payload.get("data") or {}).get("card")
        if not isinstance(card, dict):
            raise RuntimeError("Bilibili channel profile did not include creator data.")
        return card

    async def fetch_transcript(self, video: VideoRecord, work_dir: Path) -> tuple[list[TranscriptChunk], str]:
        """Fetch transcript for a Bilibili video. Tries multiple strategies in order:

        1. Player API (view -> player/wbi/v2 -> subtitle JSON), same approach as
           browser extensions; retries anonymously when the cookie session is 412-blocked.
        2. Page __INITIAL_STATE__ extraction (fast, no extra deps)
        3. yt-dlp subtitle download (handles AI subs, more reliable)
        """
        chunks: list[TranscriptChunk] = []
        source = "bilibili_subtitle"
        errors: list[str] = []

        # Strategy 1: player API subtitle tracks
        try:
            chunks = await self._fetch_via_player_api(video)
            if chunks:
                return chunks, "bilibili_subtitle"
        except ChargingVideoError:
            # Charging videos must be skipped entirely (no subtitle, no audio, no ASR).
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"player_api: {exc}")

        # Strategy 2: Extract from page __INITIAL_STATE__
        try:
            chunks = await self._fetch_from_initial_state(video.source_url)
            if chunks:
                return chunks, "bilibili_subtitle"
        except Exception as exc:
            errors.append(f"page: {exc}")

        # Strategy 3: yt-dlp subtitle extraction
        try:
            chunks = self._fetch_via_ytdlp(video, work_dir)
            if chunks:
                return chunks, "ytdlp_bilibili"
        except Exception as exc:
            errors.append(f"ytdlp: {exc}")

        raise RuntimeError(
            f"All Bilibili transcript strategies failed: {'; '.join(errors)}"
        )

    async def _fetch_via_player_api(self, video: VideoRecord) -> list[TranscriptChunk]:
        """Strategy 1: view API -> player/wbi/v2 -> subtitle URL.

        Mirrors the approach used by browser extensions such as
        bilibili-subtitle-to-chatgpt: resolve aid/cid through the view API,
        list subtitle tracks through the player API, then download the chosen
        subtitle JSON. Cookie is tried first and transparently retried without
        cookies on 412 so a stale/flagged session cannot block public subtitles.
        """
        bvid = video.video_id or ""
        if not bvid:
            match = re.search(r"(BV[0-9A-Za-z]+)", video.source_url)
            if not match:
                raise RuntimeError(f"Cannot determine BV id from {video.source_url}")
            bvid = match.group(1)

        view_payload = await self._api_get_json_with_fallback(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        )
        if view_payload.get("code") != 0:
            raise RuntimeError(
                f"Bilibili view API failed ({view_payload.get('code')}): "
                f"{view_payload.get('message') or 'unknown error'}"
            )
        video_data = view_payload.get("data") or {}
        aid = video_data.get("aid")
        cid = video_data.get("cid")
        if not aid or not cid:
            raise RuntimeError("Bilibili view API returned no aid/cid.")

        player_payload = await self._api_get_json_with_fallback(
            f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}"
        )
        if player_payload.get("code") != 0:
            raise RuntimeError(
                f"Bilibili player API failed ({player_payload.get('code')}): "
                f"{player_payload.get('message') or 'unknown error'}"
            )
        player_data = player_payload.get("data") or {}
        if player_data.get("is_upower_exclusive") or player_data.get("is_ugc_pay_preview"):
            raise ChargingVideoError(
                "Bilibili charging video (充电专属), skipped: no subtitle/audio access "
                "for non-subscribers."
            )

        tracks = (player_data.get("subtitle") or {}).get("subtitles") or []
        if not tracks:
            if player_data.get("need_login_subtitle"):
                raise RuntimeError(
                    "Bilibili subtitles require login (need_login_subtitle); "
                    "refresh the Bilibili cookie in data/bilibili_cookie.txt to fetch them directly."
                )
            raise RuntimeError("No Bilibili subtitle tracks (AI/CC) available for this video.")

        track = self._pick_subtitle_track(tracks)
        subtitle_url = str(track.get("subtitle_url") or "").strip()
        if not subtitle_url:
            raise RuntimeError("Selected subtitle track has no URL.")
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        elif not subtitle_url.startswith("http"):
            subtitle_url = "https://" + subtitle_url

        body_payload = await self._api_get_json_with_fallback(subtitle_url)
        items = body_payload.get("body") or []
        chunks = [
            TranscriptChunk(
                start=float(item.get("from", 0)),
                end=float(item.get("to", 0)),
                text=str(item.get("content", "")).strip(),
            )
            for item in items
            if str(item.get("content", "")).strip()
        ]
        if not chunks:
            raise RuntimeError("Subtitle track body was empty.")
        return chunks

    @staticmethod
    def _pick_subtitle_track(tracks: list[dict]) -> dict:
        """Prefer a Chinese track (ai-zh / zh-CN / zh-Hans), otherwise the first one."""
        for track in tracks:
            lan = str(track.get("lan") or "").lower()
            lan_doc = str(track.get("lan_doc") or "").lower()
            if re.search(r"zh", lan) or "zh" in lan_doc:
                return track
        return tracks[0]

    async def _api_get_json_with_fallback(self, url: str) -> dict:
        """GET JSON from a Bilibili API/JSON endpoint, retrying anonymously on failure."""
        last_error: Exception | None = None
        for with_cookie in (True, False):
            try:
                return await self._api_get_json(url, with_cookie=with_cookie)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        raise RuntimeError(f"Bilibili API request failed: {last_error}")

    async def _api_get_json(self, url: str, *, with_cookie: bool) -> dict:
        kwargs: dict[str, object] = {}
        if with_cookie:
            kwargs["cookies"] = self._cookie_jar()
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers=self._base_headers(),
            **kwargs,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def _fetch_from_initial_state(self, video_url: str) -> list[TranscriptChunk]:
        """Strategy 2: Extract subtitle URL from __INITIAL_STATE__ on the video page."""
        page_data = await self._fetch_video_page_data(video_url)
        subtitle = page_data.get("videoData", {}).get("subtitle") or {}

        for entry in subtitle.get("list") or []:
            subtitle_url: str | None = entry.get("subtitle_url")
            if not subtitle_url:
                continue
            if subtitle_url.startswith("//"):
                subtitle_url = f"https:{subtitle_url}"

            async with httpx.AsyncClient(
                timeout=30,
                cookies=self._cookie_jar(),
                headers=self._base_headers(),
            ) as client:
                response = await client.get(subtitle_url)
                response.raise_for_status()
                data = response.json()

            chunks = [
                TranscriptChunk(
                    start=float(item.get("from", 0)),
                    end=float(item.get("to", 0)),
                    text=str(item.get("content", "")).strip(),
                )
                for item in data.get("body", [])
                if str(item.get("content", "")).strip()
            ]
            if chunks:
                return chunks

        raise RuntimeError("No Bilibili subtitles found in page data.")

    def _fetch_via_ytdlp(self, video: VideoRecord, work_dir: Path) -> list[TranscriptChunk]:
        """Strategy 3: Use yt-dlp to download subtitles without downloading the video."""
        work_dir.mkdir(parents=True, exist_ok=True)
        outtmpl = str(work_dir / "%(id)s.%(ext)s")

        opts = {
            "quiet": True,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["all", "-danmaku"],
            "subtitlesformat": "json3",
            "outtmpl": outtmpl,
            "no_warnings": True,
            "cookiefile": str(self._cookiefile_path()) if self._cookiefile_path() else None,
            "http_headers": self._base_headers(),
            "extractor_args": {
                "bilibili": {"player_client": ["android", "ios"]},
            },
        }

        def _run(options: dict) -> None:
            with YoutubeDL(options) as ydl:
                ydl.download([video.source_url])

        try:
            _run(opts)
        except DownloadError as exc:
            if self._cookiefile_path() and "412" in str(exc):
                # A stale/flagged cookie session can 412-block everything;
                # retry once without cookies for public subtitle tracks.
                anonymous_opts = {key: value for key, value in opts.items() if key != "cookiefile"}
                _run(anonymous_opts)
            else:
                raise

        # yt-dlp writes subtitle files like: work_dir/{video_id}.zh-CN.json3 or .srt etc.
        # Try to find and parse the subtitle file
        for path in sorted(work_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.suffix in (".json3", ".vtt", ".srt") and path.stem != "audio":
                return self._parse_subtitle_file(path)

        raise RuntimeError("yt-dlp did not produce any subtitle files.")

    def _parse_subtitle_file(self, path: Path) -> list[TranscriptChunk]:
        """Parse a subtitle file (json3, vtt, or srt) into TranscriptChunks."""
        text = path.read_text(encoding="utf-8")

        if path.suffix == ".json3":
            return self._parse_json3(text)
        elif path.suffix == ".vtt":
            return self._parse_vtt(text)
        elif path.suffix == ".srt":
            return self._parse_srt(text)
        return []

    @staticmethod
    def _parse_json3(text: str) -> list[TranscriptChunk]:
        """Parse YouTube/Bilibili json3 subtitle format."""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        events = data.get("events") or []
        chunks: list[TranscriptChunk] = []
        for event in events:
            t_start_ms = event.get("tStartMs", 0)
            d_duration_ms = event.get("dDurationMs", 0)
            t_start = t_start_ms / 1000.0
            t_end = (t_start_ms + (d_duration_ms or 0)) / 1000.0
            segs = event.get("segs") or []
            text_parts = []
            for seg in segs:
                utf8 = seg.get("utf8", "")
                if utf8:
                    text_parts.append(utf8)
            line = "".join(text_parts).strip()
            if line:
                chunks.append(TranscriptChunk(start=round(t_start, 2), end=round(t_end, 2), text=line))
        return chunks

    @staticmethod
    def _parse_vtt(text: str) -> list[TranscriptChunk]:
        """Parse WebVTT subtitle format."""
        chunks: list[TranscriptChunk] = []
        # Remove WEBVTT header
        lines = text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if "-->" in line:
                # Parse timestamp line
                times = line.split("-->")
                start = BilibiliAdapter._parse_vtt_time(times[0].strip())
                end = BilibiliAdapter._parse_vtt_time(times[1].strip()) if len(times) > 1 else start + 5
                i += 1
                text_lines = []
                while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                    text_lines.append(lines[i].strip())
                    i += 1
                text_str = " ".join(text_lines).strip()
                if text_str:
                    chunks.append(TranscriptChunk(start=round(start, 2), end=round(end, 2), text=text_str))
            else:
                i += 1
        return chunks

    @staticmethod
    def _parse_vtt_time(time_str: str) -> float:
        """Parse VTT timestamp like '00:01:23.456' to seconds."""
        # Remove any styling tags
        time_str = time_str.split()[0] if time_str else "0"
        parts = time_str.replace(",", ".").split(":")
        if len(parts) == 3:
            h, m, s = parts
            return int(h) * 3600 + int(m) * 60 + float(s)
        elif len(parts) == 2:
            m, s = parts
            return int(m) * 60 + float(s)
        return float(parts[0]) if parts else 0.0

    @staticmethod
    def _parse_srt(text: str) -> list[TranscriptChunk]:
        """Parse SRT subtitle format."""
        chunks: list[TranscriptChunk] = []
        blocks = text.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) >= 3:
                # Line 1: index (skip)
                # Line 2: timestamp range
                times = lines[1].split("-->")
                if len(times) == 2:
                    start = BilibiliAdapter._parse_vtt_time(times[0].strip())
                    end = BilibiliAdapter._parse_vtt_time(times[1].strip())
                    text_str = " ".join(line.strip() for line in lines[2:])
                    if text_str:
                        chunks.append(
                            TranscriptChunk(start=round(start, 2), end=round(end, 2), text=text_str)
                        )
        return chunks

    async def hydrate_video(self, video: VideoRecord) -> VideoRecord:
        try:
            page_data = await self._fetch_video_page_data(video.source_url)
        except Exception:
            return video
        video_data = page_data.get("videoData", {})
        owner = video_data.get("owner", {})
        tags = page_data.get("tags") or []
        tag_names = [tag.get("tag_name") for tag in tags if tag.get("tag_name")]

        video.title = video_data.get("title") or video.title
        desc = (video_data.get("desc") or "").strip()
        owner_name = owner.get("name")
        owner_sign = owner.get("sign") or ""
        pieces = [
            piece
            for piece in [
                desc,
                f"作者：{owner_name}" if owner_name else "",
                f"作者简介：{owner_sign}" if owner_sign else "",
                f"标签：{'、'.join(tag_names)}" if tag_names else "",
            ]
            if piece
        ]
        if pieces:
            video.description = "\n".join(pieces)
        if video_data.get("pubdate"):
            video.published_at = datetime.fromtimestamp(video_data["pubdate"], tz=UTC)
        if video_data.get("duration"):
            video.duration_seconds = int(video_data["duration"])
        if video_data.get("pic"):
            video.thumbnail_url = str(video_data["pic"])
        return video

    def _extract_space_info(self, url: str, limit: int = 50) -> dict:
        with YoutubeDL(
            {
                "quiet": True,
                "extract_flat": True,
                "skip_download": True,
                "playlistend": max(1, min(50, limit)),
                "cookiefile": str(self._cookiefile_path()) if self._cookiefile_path() else None,
                "http_headers": self._base_headers(),
                "extractor_args": {
                    "bilibili": {"player_client": ["android", "ios"]},
                },
            }
        ) as ydl:
            return ydl.extract_info(url.rstrip("/"), download=False)

    async def _fetch_video_page_data(self, url: str) -> dict:
        """Fetch Bilibili video page and extract __INITIAL_STATE__ JSON.

        Uses a robust brace-counter instead of a fragile regex to handle
        deeply nested JSON objects that contain closing braces in strings/values.
        """
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            cookies=self._cookie_jar(),
            headers=self._base_headers(),
        ) as client:
            response = await client.get(url.rstrip("/") + "/")
            response.raise_for_status()

        html = response.text
        marker = "__INITIAL_STATE__="
        pos = html.find(marker)
        if pos == -1:
            raise RuntimeError(
                "__INITIAL_STATE__ not found in Bilibili page. "
                "The page may require login or may have changed structure."
            )

        pos += len(marker)
        # Skip any whitespace
        while pos < len(html) and html[pos] in (" ", "\t"):
            pos += 1

        if pos >= len(html) or html[pos] != "{":
            raise RuntimeError("__INITIAL_STATE__ does not start with JSON object.")

        # Brace counter to find the matching closing brace
        depth = 0
        end = pos
        in_string = False
        escape_next = False
        for i in range(pos, len(html)):
            ch = html[i]
            if escape_next:
                escape_next = False
                continue
            if ch == "\\":
                escape_next = True
                continue
            if ch == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        json_str = html[pos:end]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as exc:
            # Try fallback: old regex approach for backward compatibility
            match = re.search(r"__INITIAL_STATE__=(\{.*?\});(?=\s*\(function|\s*$)", html, re.S)
            if match:
                try:
                    return json.loads(match.group(1))
                except json.JSONDecodeError:
                    pass
            raise RuntimeError(f"Failed to parse __INITIAL_STATE__ JSON: {exc}") from exc

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }

    def _cookie_jar(self) -> dict[str, str]:
        raw = self.settings.bilibili_cookie if self.settings else None
        if not raw:
            return {}
        cookie = SimpleCookie()
        cookie.load(raw)
        # 保留 cookie 值原样（percent-encoding 如 SESSDATA 里的 %2C）。
        # 解码成逗号等字符会导致 B 站风控返回 412（服务端校验的是编码后的值）。
        return {key: morsel.value for key, morsel in cookie.items()}

    def _cookiefile_path(self) -> Path | None:
        if not self.settings or not self.settings.bilibili_cookie:
            return None
        cookiefile = self.settings.cache_dir / "bilibili.cookies.txt"
        cookiefile.parent.mkdir(parents=True, exist_ok=True)
        cookiefile.write_text(self._to_netscape_cookie_file(), encoding="utf-8")
        return cookiefile

    def _to_netscape_cookie_file(self) -> str:
        cookie = SimpleCookie()
        cookie.load(self.settings.bilibili_cookie if self.settings and self.settings.bilibili_cookie else "")
        lines = [
            "# Netscape HTTP Cookie File",
            "# This file was generated automatically from a cookie header string.",
        ]
        for key, morsel in cookie.items():
            value = morsel.value
            domain = ".bilibili.com"
            path = morsel["path"] or "/"
            secure = "TRUE" if morsel["secure"] else "FALSE"
            expires = morsel["expires"]
            expiry = "0"
            if expires.isdigit():
                expiry = expires
            lines.append(f"{domain}\tTRUE\t{path}\t{secure}\t{expiry}\t{key}\t{value}")
        return "\n".join(lines) + "\n"

    def _blocked_message(self) -> str:
        cookie_path = (
            str(self.settings.bilibili_cookie_file)
            if self.settings and self.settings.bilibili_cookie_file
            else "data/bilibili_cookie.txt"
        )
        if self.settings and self.settings.bilibili_cookie:
            return (
                "Bilibili blocked the channel request (HTTP 412). "
                "Your cookie file is loaded, but this session is still being rate-limited or challenged. "
                "Try again later, lower request frequency, or refresh your Bilibili login cookie."
            )
        return (
            "Bilibili blocked the channel request (HTTP 412). "
            f"No local Bilibili cookie was loaded. Put your logged-in cookie string into: {cookie_path}"
        )

    @staticmethod
    def _extract_uid(source_url: str) -> str:
        match = re.search(r"space\.bilibili\.com/(\d+)", source_url)
        if not match:
            raise ValueError("Bilibili source URL must be a space URL with numeric UID.")
        return match.group(1)
