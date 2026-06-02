"""
노션 페이지 ID 입력 검증(NotionPageId / _validate_notion_page_id) 테스트

LLM 에이전트가 Slack 유저 ID 등 잘못된 값을 page_id로 넘기는 사고를 막기 위한
공용 검증 로직을 검증한다. (Sentry WORKFLOW-AUTOMATION-5M/5N)
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from app.common import (
    _validate_notion_page_id,
    get_notion_page_tool,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        # 32자리 hex (노션 기본 형식)
        ("12d1cc820da680ba82d1e6d560aaf4c3", "12d1cc820da680ba82d1e6d560aaf4c3"),
        # 대시 포함 UUID (8-4-4-4-12)
        (
            "12d1cc82-0da6-80ba-82d1-e6d560aaf4c3",
            "12d1cc82-0da6-80ba-82d1-e6d560aaf4c3",
        ),
        # 대문자 hex 허용 (IGNORECASE)
        ("12D1CC820DA680BA82D1E6D560AAF4C3", "12D1CC820DA680BA82D1E6D560AAF4C3"),
        # 앞뒤 공백은 strip 후 통과
        ("  12d1cc820da680ba82d1e6d560aaf4c3  ", "12d1cc820da680ba82d1e6d560aaf4c3"),
    ],
)
def test_valid_page_id_passes(raw, expected):
    assert _validate_notion_page_id(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "U07TGP2KBPC",  # Slack 유저 ID
        "12d1cc820da680ba82d1e6d560aaf4",  # 32자리 미만
        "12d1cc820da680ba82d1e6d560aaf4c3ff",  # 32자리 초과
        "12d1cc82-0da6-80ba-82d1e6d560aaf4c3",  # 부분적으로만 대시 (UUID 아님)
        "zzd1cc820da680ba82d1e6d560aaf4c3",  # hex 아닌 문자 포함
        "",  # 빈 문자열
    ],
)
def test_invalid_page_id_rejected(bad):
    with pytest.raises(ValueError) as exc_info:
        _validate_notion_page_id(bad)
    # 에이전트가 자기수정할 수 있도록 안내 문구를 포함한다
    assert "Slack 유저 ID" in str(exc_info.value)


def test_validator_fires_through_tool_schema():
    """공용 타입이 도구 args_schema 에 연결되어 잘못된 입력을 차단하는지 검증."""
    tool = get_notion_page_tool()
    with pytest.raises(ValidationError):
        tool.invoke({"page_id": "U07TGP2KBPC"})


def test_valid_page_id_reaches_tool_body():
    """유효한 page_id 는 검증을 통과하여 도구 본문(노션 조회)까지 도달한다."""
    tool = get_notion_page_tool()
    with patch("app.common.notion_page_to_markdown", return_value="# md") as mock_md:
        result = tool.invoke({"page_id": "  12d1cc820da680ba82d1e6d560aaf4c3  "})
    assert result == "# md"
    # strip 후 정규화된 값으로 조회되었는지 확인
    mock_md.assert_called_once_with("12d1cc820da680ba82d1e6d560aaf4c3")
