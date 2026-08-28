"""운영 실패를 원인 스레드에 남기고 개선 작업으로 연결합니다."""

import asyncio
import json
import uuid
from urllib.parse import urlencode, urlparse, urlunparse

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from service.slack_task_message import (
    SlackMessageLocation,
    clean_list,
    clean_references,
    escape_slack,
    get_permalink,
    message_location,
    section_blocks,
    slack_link,
    validate_publishable,
)
from service.slack_task_list import default_due_date
from service.slack_task_thread import (
    SlackTaskListSchema,
    acquire_task_record_lock,
    parse_slack_list_task_url,
    release_task_record_lock,
    task_list_schema,
)

MAX_LIST_PAGES = 20
LIST_PAGE_SIZE = 100


def _clean_text(name: str, value: str, minimum: int, maximum: int) -> str:
    cleaned = value.strip()
    if len(cleaned) < minimum or len(cleaned) > maximum:
        raise ValueError(f"{name}은 {minimum}자 이상 {maximum}자 이내로 작성해주세요.")
    return cleaned


def _same_root(left: SlackMessageLocation, right: SlackMessageLocation) -> bool:
    return left.channel_id == right.channel_id and left.root_ts == right.root_ts


def _same_message(left: SlackMessageLocation, right: SlackMessageLocation) -> bool:
    return left.channel_id == right.channel_id and left.ts == right.ts


def _linked_locations(
    schema: SlackTaskListSchema, record: dict
) -> list[SlackMessageLocation]:
    references = [
        *schema.source_thread_references_of(record),
        *schema.work_thread_references_of(record),
    ]
    locations = []
    for reference in references:
        try:
            locations.append(message_location(reference))
        except ValueError:
            continue
    return locations


def _validate_linked_thread(
    thread_url: str,
    linked_locations: list[SlackMessageLocation],
    name: str,
) -> SlackMessageLocation:
    location = message_location({"value": thread_url})
    if not any(_same_root(location, linked) for linked in linked_locations):
        raise ValueError(
            f"{name}은 이 작업의 요청 맥락 또는 작업 기록 스레드여야 합니다."
        )
    return location


async def _actor_user_id(client: AsyncWebClient, actor: str) -> str | None:
    try:
        user = (await client.users_lookupByEmail(email=actor)).get("user") or {}
    except SlackApiError:
        return None
    return user.get("id")


def _reference_lines(references: list[tuple[str, str]]) -> list[str]:
    if not references:
        return []
    return [
        "",
        "참고 자료:",
        *[f"• {slack_link(url, name)}" for name, url in references],
    ]


def _list_section(lines: list[str], label: str, values: list[str]) -> None:
    if values:
        lines.extend(
            ["", f"{label}:", *[f"• {escape_slack(value)}" for value in values]]
        )


def _postmortem_message(
    automation_mention: str | None,
    title: str,
    expected: str,
    actual: str,
    confirmed_causes: list[str],
    hypotheses: list[str],
    missed_signals: list[str],
    investigation_items: list[str],
    system_changes: list[str],
    improvement_task_title: str | None,
    improvement_target: str | None,
    completion_criteria: list[str],
    references: list[tuple[str, str]],
) -> str:
    heading = f"[운영 포스트모템] {escape_slack(title)}"
    if automation_mention:
        heading = f"{automation_mention} {heading}"
    lines = [
        heading,
        "",
        "무슨 일이 있었는가:",
        f"• 기대: {escape_slack(expected)}",
        f"• 실제: {escape_slack(actual)}",
    ]
    _list_section(lines, "확인된 원인", confirmed_causes)
    _list_section(lines, "아직 조사할 원인", hypotheses)
    _list_section(lines, "놓친 신호·선행 조건", missed_signals)
    _list_section(lines, "추가 조사", investigation_items)
    _list_section(lines, "바꿀 시스템", system_changes)
    if improvement_task_title:
        lines.extend(
            [
                "",
                f"개선 작업: {escape_slack(improvement_task_title)}",
                f"변경 대상: {escape_slack(improvement_target or '')}",
            ]
        )
        _list_section(lines, "완료 기준", completion_criteria)
    lines.extend(_reference_lines(references))
    return "\n".join(lines)


def _task_url(list_url: str, record_id: str) -> str:
    parsed = urlparse(list_url)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode({"record_id": record_id}),
            "",
        )
    )


async def _find_task_for_postmortem(
    client: AsyncWebClient,
    list_id: str,
    schema: SlackTaskListSchema,
    post_location: SlackMessageLocation,
) -> str | None:
    cursor = None
    for _ in range(MAX_LIST_PAGES):
        response = await client.slackLists_items_list(
            list_id=list_id,
            limit=LIST_PAGE_SIZE,
            cursor=cursor,
        )
        for item in response.get("items", []):
            for reference in schema.source_thread_references_of(item):
                try:
                    location = message_location(reference)
                except ValueError:
                    continue
                if _same_message(location, post_location):
                    return str(item["id"])
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return None
    raise ValueError(
        "개선 작업 중복 확인 범위를 초과했습니다. List를 정리한 뒤 다시 시도해주세요."
    )


async def publish_operational_postmortem(
    client: AsyncWebClient,
    actor: str,
    list_url: str,
    incident_key: str,
    target_thread_url: str,
    title: str,
    expected: str,
    actual: str,
    confirmed_causes: list[str] | None = None,
    hypotheses: list[str] | None = None,
    missed_signals: list[str] | None = None,
    investigation_items: list[str] | None = None,
    system_changes: list[str] | None = None,
    improvement_task_title: str | None = None,
    improvement_target: str | None = None,
    completion_criteria: list[str] | None = None,
    references: list[str] | None = None,
    related_thread_url: str | None = None,
) -> str:
    """분석된 포스트모템을 원인 스레드에 게시하고 개선 작업을 만듭니다."""
    incident_key = _clean_text("사건 키", incident_key, 3, 120)
    title = _clean_text("포스트모템 제목", title, 5, 160)
    expected = _clean_text("기대 결과", expected, 3, 500)
    actual = _clean_text("실제 결과", actual, 3, 500)
    confirmed_causes = clean_list("확인된 원인", confirmed_causes, 4)
    hypotheses = clean_list("아직 조사할 원인", hypotheses, 4)
    missed_signals = clean_list("놓친 신호·선행 조건", missed_signals, 4)
    investigation_items = clean_list("추가 조사", investigation_items, 4)
    system_changes = clean_list("바꿀 시스템", system_changes, 4)
    completion_criteria = clean_list("완료 기준", completion_criteria, 5)
    references = clean_references(references)

    if not confirmed_causes and not hypotheses:
        raise ValueError("확인된 원인 또는 아직 조사할 원인을 하나 이상 남겨주세요.")

    if improvement_task_title is not None:
        improvement_task_title = _clean_text(
            "개선 작업 제목", improvement_task_title, 5, 160
        )
        improvement_target = _clean_text("변경 대상", improvement_target or "", 2, 200)
        if not completion_criteria:
            raise ValueError("개선 작업에는 완료 기준이 하나 이상 필요합니다.")
        if not system_changes and not investigation_items:
            raise ValueError("개선 작업에는 조사할 사항 또는 바꿀 시스템이 필요합니다.")
    elif improvement_target or completion_criteria:
        raise ValueError("변경 대상과 완료 기준은 개선 작업을 만들 때만 사용합니다.")

    reference = parse_slack_list_task_url(list_url)
    lock = await asyncio.to_thread(acquire_task_record_lock, reference)
    try:
        response = await client.slackLists_items_info(
            list_id=reference.list_id,
            id=reference.record_id,
        )
        schema = task_list_schema(response["list"]["list_metadata"]["schema"])
        record = response["record"]
        linked_locations = _linked_locations(schema, record)
        target = _validate_linked_thread(
            target_thread_url, linked_locations, "포스트모템 대상 스레드"
        )
        related = None
        if related_thread_url:
            related = _validate_linked_thread(
                related_thread_url, linked_locations, "교차 링크 대상 스레드"
            )
            if _same_root(target, related):
                raise ValueError(
                    "교차 링크는 포스트모템 원문과 다른 스레드에 남겨야 합니다."
                )

        automation_mention = None
        actor_user_id = None
        if improvement_task_title:
            if schema.source_thread_column_id is None:
                raise ValueError(
                    '개선 작업을 만들려면 List에 message 타입의 "요청 맥락" 열이 필요합니다.'
                )
            actor_user_id = await _actor_user_id(client, actor)
            auth = await client.auth_test()
            bot_user_id = auth.get("user_id")
            if not bot_user_id:
                raise ValueError(
                    "@자동화에 사용할 Slack 봇 사용자 ID를 찾지 못했습니다."
                )
            automation_mention = f"<@{bot_user_id}>"

        body = _postmortem_message(
            automation_mention,
            title,
            expected,
            actual,
            confirmed_causes,
            hypotheses,
            missed_signals,
            investigation_items,
            system_changes,
            improvement_task_title,
            improvement_target,
            completion_criteria,
            references,
        )
        validate_publishable([body], max_chars=8_000)
        message_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"operational-postmortem:{reference.list_id}:{reference.record_id}:{incident_key}",
        )
        posted = await client.chat_postMessage(
            channel=target.channel_id,
            thread_ts=target.root_ts,
            text=body,
            blocks=section_blocks(body, expand=True),
            client_msg_id=str(message_id),
        )
        post_location = SlackMessageLocation(
            channel_id=str(posted.get("channel", target.channel_id)),
            ts=str(posted["ts"]),
            root_ts=target.root_ts,
        )
        post_permalink = await get_permalink(client, post_location)

        improvement_task = None
        if improvement_task_title:
            improvement_record_id = await _find_task_for_postmortem(
                client,
                reference.list_id,
                schema,
                post_location,
            )
            created = improvement_record_id is None
            if created:
                created_response = await client.slackLists_items_create(
                    list_id=reference.list_id,
                    initial_fields=schema.new_task_cells(
                        improvement_task_title,
                        actor_user_id,
                        default_due_date(),
                        post_permalink,
                        post_permalink,
                    ),
                )
                improvement_record_id = str(created_response["item"]["id"])
            improvement_task = {
                "created": created,
                "record_id": improvement_record_id,
                "list_url": _task_url(list_url, improvement_record_id),
                "status": "pending",
            }

        related_link_posted = False
        if related:
            link_text = (
                "[포스트모템] 실패 원인과 개선 작업은 "
                f"{slack_link(post_permalink, '원문')}에 남겼습니다."
            )
            related_id = uuid.uuid5(message_id, "related-thread-link")
            await client.chat_postMessage(
                channel=related.channel_id,
                thread_ts=related.root_ts,
                text=link_text,
                client_msg_id=str(related_id),
            )
            related_link_posted = True

        return json.dumps(
            {
                "posted": True,
                "permalink": post_permalink,
                "target_thread": target_thread_url,
                "related_link_posted": related_link_posted,
                "improvement_task": improvement_task,
            },
            ensure_ascii=False,
        )
    finally:
        await asyncio.to_thread(release_task_record_lock, lock)
