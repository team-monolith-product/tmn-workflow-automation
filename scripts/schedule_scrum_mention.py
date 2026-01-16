"""
매일 16:30에 스크럼 작성 멘션을 각 팀 스레드에 답글로 발송하는 스크립트
"""

import os
import time

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.teams import get_team_mention

# 환경 변수 로드
load_dotenv()

SCRUM_CHANNEL_ID = "C09277NGUET"

# 팀별 멘션 그룹 ID (service.teams에서 가져온 값 + 개인)
TEAM_MENTIONS = {
    "기획": get_team_mention("기획"),
    "FE": get_team_mention("fe"),
    "BE": get_team_mention("be"),
    "IE": get_team_mention("ie"),
    "이창환": "<@U02HT4EU4VD>",
}


def main():
    """메인 함수"""
    print("=== 스크럼 멘션 스케줄러 시작 ===")

    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # 16:00에 보낸 스크럼 메시지 찾기 (최근 2시간 이내)
    now = time.time()
    oldest = now - 3600 * 2  # 2시간 전

    print(f"현재 시간: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
    print(
        f"검색 시작 시간: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(oldest))}"
    )
    print(f"채널 ID: {SCRUM_CHANNEL_ID}")

    try:
        # 채널의 최근 메시지 조회
        # Slack API는 oldest를 정수 형태의 문자열로 전달해야 함
        print("\n메시지 조회 중...")
        response = slack_client.conversations_history(
            channel=SCRUM_CHANNEL_ID,
            oldest=str(int(oldest)),  # float를 int로 변환 후 문자열로
            limit=50,
        )

        messages = response.get("messages", [])
        print(f"조회된 메시지 수: {len(messages)}")

        # 각 팀 스크럼 메시지 찾기
        team_threads = {}
        print("\n=== 메시지 분석 시작 ===")
        for idx, message in enumerate(messages, 1):
            text = message.get("text", "")
            ts = message.get("ts", "")
            msg_time = time.strftime("%H:%M:%S", time.localtime(float(ts)))

            print(f"\n[메시지 {idx}] 시간: {msg_time}")
            print(f"텍스트: {text[:100]}{'...' if len(text) > 100 else ''}")

            # 팀별 스크럼 메시지 매칭 (타임스탬프 제거됨)
            for team_name in ["기획", "FE", "BE", "IE", "이창환"]:
                if f"{team_name}팀 스크럼" in text or f"{team_name} 스크럼" in text:
                    team_threads[team_name] = message["ts"]
                    print(f"✓ {team_name} 팀 스레드 발견 (ts: {ts})")
                    break

        print(f"\n=== 팀 스레드 매칭 결과 ===")
        print(f"발견된 팀 수: {len(team_threads)}")
        for team_name, thread_ts in team_threads.items():
            print(f"- {team_name}: {thread_ts}")

        # 각 팀 스레드에 멘션 답글 추가
        print("\n=== 멘션 발송 시작 ===")
        for team_name, thread_ts in team_threads.items():
            mention = TEAM_MENTIONS.get(team_name)
            if mention:
                reply_text = f"{mention} 스크럼 작성 부탁드립니다."
                print(f"\n{team_name} 팀에 멘션 발송 중...")
                print(f"  - 스레드 ts: {thread_ts}")
                print(f"  - 멘션: {mention}")

                slack_client.chat_postMessage(
                    channel=SCRUM_CHANNEL_ID,
                    thread_ts=thread_ts,
                    text=reply_text,
                )
                print(f"✓ {team_name} 팀 멘션 발송 완료")
            else:
                print(f"✗ {team_name} 팀의 멘션 정보 없음")

        print("\n=== 스크럼 멘션 스케줄러 완료 ===")

    except Exception as e:
        print(f"Error posting scrum mentions: {e}")
        raise


if __name__ == "__main__":
    main()
