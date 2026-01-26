"""
팀 설정 서비스

Slack 사용자 그룹 ID와 팀 관련 유틸리티 함수를 제공합니다.
"""

from typing import Literal

# 개발팀 Slack 사용자 그룹 ID
TEAM_USERGROUP_IDS: dict[Literal["fe", "be", "ie"], str] = {
    "fe": "S07V4G2QJJY",
    "be": "S085DBK2TFD",
    "ie": "S08628PEEUQ",
}

# 전체 팀 Slack 사용자 그룹 ID (기획팀 포함)
ALL_TEAM_USERGROUP_IDS: dict[str, str] = {
    "기획": "S092KHHE0AF",
    **TEAM_USERGROUP_IDS,
}


def get_team_mention(team_handle: str) -> str:
    """
    팀 핸들로 Slack 멘션 문자열 생성

    Args:
        team_handle: 팀 핸들 (예: "fe", "be", "ie", "기획")

    Returns:
        str: Slack 멘션 문자열 (예: "<!subteam^S07V4G2QJJY>")

    Raises:
        ValueError: 알 수 없는 팀 핸들인 경우
    """
    usergroup_id = get_usergroup_id(team_handle)
    return f"<!subteam^{usergroup_id}>"


def get_usergroup_id(team_handle: str) -> str:
    """
    팀 핸들로 Slack 사용자 그룹 ID 반환

    Args:
        team_handle: 팀 핸들 (대소문자 구분 없음)

    Returns:
        str: 사용자 그룹 ID

    Raises:
        ValueError: 알 수 없는 팀 핸들인 경우
    """
    # 대소문자 구분 없이 검색
    normalized = team_handle.lower()
    for key, value in ALL_TEAM_USERGROUP_IDS.items():
        if key.lower() == normalized:
            return value
    raise ValueError(f"알 수 없는 팀 핸들: {team_handle}")
