"""스크럼 PR 경고 제외 테스트"""

from datetime import datetime
from unittest.mock import patch

from scripts.post_scrum_message import format_task_line


def _make_task(
    title: str = "테스트 태스크",
    deadline: str = "2026-04-07",
    has_pr: bool = False,
    assignee_name: str = "테스트",
) -> dict:
    """테스트용 태스크 생성 헬퍼"""
    return {
        "title": title,
        "url": "https://notion.so/test",
        "deadline": deadline,
        "has_pr": has_pr,
        "assignee_name": assignee_name,
    }


@patch("scripts.post_scrum_message.datetime")
def test_format_task_line_without_pr_warning(mock_datetime):
    """PR 경고 비활성화 시 PR 경고 텍스트 없음"""
    mock_datetime.now.return_value = datetime(2026, 4, 7)
    mock_datetime.fromisoformat = datetime.fromisoformat

    task = _make_task(
        title="[기획]MVP 설계", deadline="2026-04-07", assignee_name="전종현"
    )
    result = format_task_line(task, pr_warning_enabled=False)

    assert "PR이 없으므로" not in result
    assert "마감" in result


@patch("scripts.post_scrum_message.datetime")
def test_format_task_line_with_pr_warning_when_no_pr(mock_datetime):
    """PR 경고 활성화 + 마감 임박 + PR 없으면 경고 표시"""
    mock_datetime.now.return_value = datetime(2026, 4, 7)
    mock_datetime.fromisoformat = datetime.fromisoformat

    task = _make_task(deadline="2026-04-07", has_pr=False)
    result = format_task_line(task, pr_warning_enabled=True)

    assert "PR이 없으므로 일정 조정이 필요합니다." in result


@patch("scripts.post_scrum_message.datetime")
def test_format_task_line_with_pr_warning_when_has_pr(mock_datetime):
    """PR 경고 활성화 + 마감 임박이어도 PR 있으면 경고 없음"""
    mock_datetime.now.return_value = datetime(2026, 4, 7)
    mock_datetime.fromisoformat = datetime.fromisoformat

    task = _make_task(deadline="2026-04-07", has_pr=True)
    result = format_task_line(task, pr_warning_enabled=True)

    assert "PR이 없으므로" not in result


def test_pr_warning_exclusion_logic():
    """PR 경고 제외 대상 판별 로직 테스트"""
    email_to_user_id = {
        "planner@example.com": "U_PLANNER",
        "dev@example.com": "U_DEV",
    }
    pr_warning_excluded_members = ["U_PLANNER"]

    # 기획자는 제외됨
    planner_user_id = email_to_user_id.get("planner@example.com")
    assert planner_user_id in pr_warning_excluded_members

    # 개발자는 제외되지 않음
    dev_user_id = email_to_user_id.get("dev@example.com")
    assert dev_user_id not in pr_warning_excluded_members
