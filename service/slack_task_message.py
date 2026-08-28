"""Slack 작업 메시지의 링크 해석, 검증, 표시 형식을 공유합니다."""

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from slack_sdk.web.async_client import AsyncWebClient

SECRET_PATTERN = re.compile(
    r"(?:xox[baprs]-\S+|sk-(?:proj-)?[A-Za-z0-9_-]{16,}|"
    r"AKIA[0-9A-Z]{16}|Bearer\s+\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
LOCAL_PATH_PATTERN = re.compile(
    r"(?:^|[\s(])(?:/Users/|/home/|file://|[A-Za-z]:\\)", re.MULTILINE
)
OUTPUT_PATTERN = re.compile(r"^(.+?):\s*(https://[^\s<>]+)$", re.IGNORECASE)
MAX_REFERENCE_COUNT = 30
SLACK_SECTION_TEXT_LIMIT = 3_000


@dataclass(frozen=True)
class SlackMessageLocation:
    """Slack 메시지와 그 메시지가 속한 루트 스레드 위치입니다."""

    channel_id: str
    ts: str
    root_ts: str
    permalink: str | None = None


def message_location(reference: dict[str, Any]) -> SlackMessageLocation:
    """Slack message 셀이나 permalink를 API 호출 위치로 바꿉니다."""
    permalink = reference.get("value") or reference.get("permalink")
    channel_id = reference.get("channel_id")
    ts = reference.get("ts")
    thread_ts = reference.get("thread_ts")

    if permalink:
        parsed = urlparse(str(permalink))
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) >= 3 and re.fullmatch(r"[CDG][A-Z0-9]+", segments[-2]):
            channel_id = channel_id or segments[-2]
            path_ts = segments[-1]
            if path_ts.startswith("p") and path_ts[1:].isdigit():
                digits = path_ts[1:]
                if len(digits) > 6:
                    ts = ts or f"{digits[:-6]}.{digits[-6:]}"
        query = parse_qs(parsed.query)
        thread_ts = thread_ts or (query.get("thread_ts") or [None])[0]
        channel_id = channel_id or (query.get("cid") or [None])[0]

    if not channel_id or not ts:
        raise ValueError("List의 Slack 메시지 링크에서 채널과 시각을 읽지 못했습니다.")

    return SlackMessageLocation(
        channel_id=str(channel_id),
        ts=str(ts),
        root_ts=str(thread_ts or ts),
        permalink=str(permalink) if permalink else None,
    )


async def get_permalink(
    client: AsyncWebClient, location: SlackMessageLocation, root: bool = False
) -> str:
    """Slack 메시지 또는 루트 스레드 permalink를 반환합니다."""
    if location.permalink and not root:
        return location.permalink
    message_ts = location.root_ts if root else location.ts
    response = await client.chat_getPermalink(
        channel=location.channel_id, message_ts=message_ts
    )
    return response["permalink"]


def escape_slack(value: str) -> str:
    return html.escape(value, quote=False)


def slack_link(url: str, label: str) -> str:
    return f"<{escape_slack(url)}|{escape_slack(label)}>"


def clean_list(name: str, values: list[str] | None, maximum: int) -> list[str]:
    cleaned = [value.strip() for value in (values or []) if value.strip()]
    if len(cleaned) > maximum:
        raise ValueError(f"{name}은 최대 {maximum}개만 남겨주세요.")
    if any(len(value) > 600 for value in cleaned):
        raise ValueError(f"{name}의 각 항목은 600자 이내로 요약해주세요.")
    return cleaned


def clean_outputs(values: list[str]) -> list[tuple[str, str]]:
    """`산출물 이름: https://링크` 입력을 검증합니다."""
    outputs = clean_list("산출물", values, 10)
    parsed = []
    for output in outputs:
        match = OUTPUT_PATTERN.fullmatch(output)
        if not match:
            raise ValueError(
                '산출물은 각각 "산출물 이름: https://공유링크" 형식으로 작성해주세요.'
            )
        parsed.append((match.group(1).strip(), match.group(2)))
    return parsed


def clean_references(values: list[str] | None) -> list[tuple[str, str]]:
    """`자료 이름: https://링크` 입력을 중복 없이 검증합니다."""
    references = clean_list("참고 자료", values, MAX_REFERENCE_COUNT)
    parsed = []
    seen_urls = set()
    for reference in references:
        match = OUTPUT_PATTERN.fullmatch(reference)
        if not match:
            raise ValueError(
                '참고 자료는 각각 "자료 이름: https://공유링크" 형식으로 작성해주세요.'
            )
        name, url = match.group(1).strip(), match.group(2)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        parsed.append((name, url))
    return parsed


def validate_publishable(parts: list[str], max_chars: int = 6_000) -> None:
    text = "\n".join(parts)
    if len(text) > max_chars:
        raise ValueError(f"Slack 기록은 전체 {max_chars:,}자 이내로 요약해주세요.")
    if SECRET_PATTERN.search(text):
        raise ValueError("Slack 결과에 토큰이나 비밀값으로 보이는 문자열이 있습니다.")
    if LOCAL_PATH_PATTERN.search(text):
        raise ValueError("Slack 결과에는 로컬 절대경로를 넣지 말고 공유 링크를 쓰세요.")


def reference_section(references: list[tuple[str, str]]) -> str:
    return "\n".join(
        [
            f"참고 자료 ({len(references)}건):",
            *[f"• {slack_link(url, name)}" for name, url in references],
        ]
    )


def section_blocks(text: str, *, expand: bool) -> list[dict[str, Any]]:
    """Slack section 한도를 넘지 않게 줄바꿈 경계로 나눕니다."""
    blocks = []
    remaining = text
    while remaining:
        if len(remaining) <= SLACK_SECTION_TEXT_LIMIT:
            chunk, remaining = remaining, ""
        else:
            cut = remaining.rfind("\n", 0, SLACK_SECTION_TEXT_LIMIT + 1)
            if cut <= 0:
                cut = SLACK_SECTION_TEXT_LIMIT
            chunk = remaining[:cut]
            remaining = remaining[cut:].lstrip("\n")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": chunk},
                "expand": expand,
            }
        )
    return blocks


def message_blocks(body: str, details: str) -> list[dict[str, Any]]:
    blocks = section_blocks(body, expand=True)
    if details:
        blocks.append({"type": "divider"})
        blocks.extend(section_blocks(details, expand=False))
    return blocks
