"""
Slack API를 활용하는 Service Layer입니다.
"""

from slack_sdk import WebClient


def get_email_to_slack_id(slack_client: WebClient):
    """
    Slack의 사용자 목록을 가져와서 이메일 -> Slack user_id 매핑 딕셔너리를 반환합니다.
    """
    email_to_slack_id = {}
    cursor = None

    while True:
        response = slack_client.users_list(cursor=cursor)
        members = response["members"]

        for member in members:
            profile = member.get("profile", {})
            email = profile.get("email")
            if email:
                email_to_slack_id[email] = member["id"]

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    return email_to_slack_id
