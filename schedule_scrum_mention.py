"""
매일 16:30에 스크럼 작성 멘션을 각 팀 스레드에 답글로 발송하는 스크립트
"""

import os
import time

from dotenv import load_dotenv
from slack_sdk import WebClient

# 환경 변수 로드
load_dotenv()

SCRUM_CHANNEL_ID = "C09277NGUET"

# 팀별 멘션 그룹 ID
TEAM_MENTIONS = {
    "기획": "<!subteam^S092KHHE0AF>",
    "FE": "<!subteam^S07V4G2QJJY>",
    "BE": "<!subteam^S085DBK2TFD>",
    "IE": "<!subteam^S08628PEEUQ>",
    "이창환": "<@U02HT4EU4VD>",
}


def main():
    """메인 함수"""
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # 16:00에 보낸 스크럼 메시지 찾기 (최근 1시간 이내)
    now = time.time()
    oldest = now - 3600  # 1시간 전

    try:
        # 채널의 최근 메시지 조회
        response = slack_client.conversations_history(
            channel=SCRUM_CHANNEL_ID,
            oldest=oldest,
            limit=50,
        )

        messages = response.get("messages", [])

        # 각 팀 스크럼 메시지 찾기
        team_threads = {}
        for message in messages:
            text = message.get("text", "")

            # 팀별 스크럼 메시지 매칭 (타임스탬프 제거됨)
            for team_name in ["기획", "FE", "BE", "IE", "이창환"]:
                if f"{team_name}팀 스크럼" in text or f"{team_name} 스크럼" in text:
                    team_threads[team_name] = message["ts"]
                    break

        # 각 팀 스레드에 멘션 답글 추가
        for team_name, thread_ts in team_threads.items():
            mention = TEAM_MENTIONS.get(team_name)
            if mention:
                reply_text = f"{mention} 스크럼 작성 부탁드립니다."
                slack_client.chat_postMessage(
                    channel=SCRUM_CHANNEL_ID,
                    thread_ts=thread_ts,
                    text=reply_text,
                )
                print(f"Posted mention for {team_name} team")

    except Exception as e:
        print(f"Error posting scrum mentions: {e}")
        raise


if __name__ == "__main__":
    main()
