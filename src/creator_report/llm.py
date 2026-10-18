from __future__ import annotations

import json
from typing import Any

import httpx

from creator_report.config import Settings
from creator_report.models import ChannelProfile, VideoRecord, VideoSummary


class DeepSeekClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.request_count = 0
        self.usage_totals = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    def _headers(self) -> dict[str, str]:
        if not self.settings.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured.")
        return {"Authorization": f"Bearer {self.settings.deepseek_api_key}"}

    async def _chat_json(self, prompt: str) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self.settings.deepseek_base_url, timeout=120) as client:
            response = await client.post(
                "/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.settings.deepseek_model,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You extract structured summaries from creator videos. "
                                "Always answer with valid JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            response.raise_for_status()
        data = response.json()
        usage = data.get("usage") or {}
        self.request_count += 1
        for name in self.usage_totals:
            self.usage_totals[name] += int(usage.get(name) or 0)
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)

    async def summarize_video(self, video: VideoRecord, output_language: str) -> VideoSummary:
        transcript_text = "\n".join(chunk.text for chunk in video.transcript[:400])
        prompt = (
            f"Language: {output_language}\n"
            "Summarize the following creator video transcript. "
            "Return a JSON object with exactly these keys and types:\n"
            '  "overview": string (a 150-300 Chinese-character editorial review covering '
            "the thesis, argument quality, and who should watch)\n"
            '  "key_points": array of strings (3-5 key takeaways)\n'
            '  "viewpoints": array of strings (opinions/positions stated, MUST be strings not objects)\n'
            '  "topics": array of strings (3-5 topic keywords)\n'
            '  "snippets": array of strings (1-3 direct quotes or paraphrased highlights, MUST be strings not objects)\n'
            "IMPORTANT: viewpoints and snippets must be flat arrays of strings, not objects/dicts. "
            "Example: {\"viewpoints\": [\"AI will replace jobs.\"], \"snippets\": [\"Quote text here.\"]}\n\n"
            f"Title: {video.title}\nDescription: {video.description}\nTranscript:\n{transcript_text}"
        )
        payload = await self._chat_json(prompt)
        # Normalize malformed LLM responses
        payload = self._normalize_summary_payload(payload)
        return VideoSummary.model_validate(payload)

    @staticmethod
    def _normalize_summary_payload(payload: dict) -> dict:
        """Fix common LLM JSON output issues for VideoSummary fields."""
        # viewpoints: dict {text, ...} -> string
        if "viewpoints" in payload and isinstance(payload["viewpoints"], list):
            payload["viewpoints"] = [
                v if isinstance(v, str) else str(v.get("text", v)) if isinstance(v, dict) else str(v)
                for v in payload["viewpoints"]
            ]
        # snippets: dict {text, ...} -> string
        if "snippets" in payload and isinstance(payload["snippets"], list):
            payload["snippets"] = [
                s if isinstance(s, str) else str(s.get("text", s)) if isinstance(s, dict) else str(s)
                for s in payload["snippets"]
            ]
        # topics: ensure strings
        if "topics" in payload and isinstance(payload["topics"], list):
            payload["topics"] = [
                t if isinstance(t, str) else str(t) for t in payload["topics"]
            ]
        # key_points: ensure strings
        if "key_points" in payload and isinstance(payload["key_points"], list):
            payload["key_points"] = [
                k if isinstance(k, str) else str(k) for k in payload["key_points"]
            ]
        return payload

    async def filter_relevance(self, videos: list[VideoRecord], topic_prompt: str) -> list[str]:
        candidate_lines = [
            f"{video.video_id}\t{video.title}\t{video.description[:160]}" for video in videos
        ]
        prompt = (
            "Select relevant video_ids for the user topic. Return JSON object with one key: relevant_ids.\n"
            f"Topic: {topic_prompt}\nCandidates:\n" + "\n".join(candidate_lines)
        )
        payload = await self._chat_json(prompt)
        return list(payload.get("relevant_ids", []))

    async def synthesize_channel_summary(
        self,
        channel: ChannelProfile,
        videos: list[VideoRecord],
        output_language: str,
    ) -> str:
        digest = []
        for video in videos[:100]:
            if not video.summary:
                continue
            digest.append(
                f"- {video.title}: {video.summary.overview}; 观点: {' / '.join(video.summary.viewpoints[:3])}"
            )
        prompt = (
            f"Language: {output_language}\n"
            "Write a concise channel-level summary of the creator's recurring themes and viewpoints.\n"
            f"Creator: {channel.title}\nVideos:\n" + "\n".join(digest)
        )
        payload = await self._chat_json(prompt)
        return str(payload.get("summary", "")).strip()


class MockDeepSeekClient(DeepSeekClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    async def summarize_video(self, video: VideoRecord, output_language: str) -> VideoSummary:
        text = " ".join(chunk.text for chunk in video.transcript[:8]) or video.description or video.title
        snippet = text[:200] or video.title
        topics = [word.lower() for word in video.title.replace("/", " ").split()[:3]] or ["general"]
        return VideoSummary(
            overview=f"{video.title} 的自动摘要。",
            key_points=[video.description[:120] or snippet],
            viewpoints=[f"{video.title} 强调可复用的方法。"],
            topics=topics,
            snippets=[snippet],
        )

    async def filter_relevance(self, videos: list[VideoRecord], topic_prompt: str) -> list[str]:
        lowered = topic_prompt.lower()
        ids = []
        for video in videos:
            haystack = f"{video.title} {video.description}".lower()
            if lowered in haystack:
                ids.append(video.video_id)
        return ids or [video.video_id for video in videos[:10]]

    async def synthesize_channel_summary(
        self,
        channel: ChannelProfile,
        videos: list[VideoRecord],
        output_language: str,
    ) -> str:
        del output_language
        topics = []
        for video in videos[:5]:
            if video.summary:
                topics.extend(video.summary.topics[:2])
        joined = "、".join(dict.fromkeys(topics)) or "稳定内容"
        return f"{channel.title} 主要围绕 {joined} 输出内容。"
