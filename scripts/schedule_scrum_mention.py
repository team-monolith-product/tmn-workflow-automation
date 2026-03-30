"""
매일 16:30에 스크럼 작성 멘션을 각 팀 스레드에 답글로 발송하는 스크립트
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os
import time
from collections import defaultdict

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.scrum_config import load_scrum_config

# 환경 변수 로드
load_dotenv()


def main():
    """메인 함수"""
    print("=== 스크럼 멘션 스케줄러 시작 ===")

    config = load_scrum_config()
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    # config에서 팀별 멘션 구성 (display_name -> mention, channel_id)
    team_entries = {}
    for squad in config.squads:
        team_entries[squad.display_name] = {
            "mention": f"<!subteam^{squad.slack_usergroup_id}>",
            "channel_id": squad.slack_channel_id,
        }
    for personal in config.personal_scrums:
        team_entries[personal.name] = {
            "mention": f"<@{personal.slack_user_id}>",
            "channel_id": personal.slack_channel_id,
        }

    # 채널별로 검색할 팀 이름 그룹화
    channel_to_team_names: dict[str, list[str]] = defaultdict(list)
    for team_name, entry in team_entries.items():
        channel_to_team_names[entry["channel_id"]].append(team_name)

    # 16:00에 보낸 스크럼 메시지 찾기 (최근 2시간 이내)
    now = time.time()
    oldest = now - 3600 * 2  # 2시간 전

    print(f"현재 시간: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}")
    print(
        f"검색 시작 시간: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(oldest))}"
    )
    print(f"검색 대상 채널: {list(channel_to_team_names.keys())}")

    try:
        # 채널별로 메시지 검색 및 멘션 발송
        for channel_id, team_names in channel_to_team_names.items():
            print(f"\n=== 채널 {channel_id} 검색 ===")
            print(f"매칭 대상: {team_names}")

            response = slack_client.conversations_history(
                channel=channel_id,
                oldest=str(int(oldest)),
                limit=50,
            )

            messages = response.get("messages", [])
            print(f"조회된 메시지 수: {len(messages)}")

            # 팀 스크럼 메시지 찾기
            team_threads = {}
            for idx, message in enumerate(messages, 1):
                text = message.get("text", "")
                ts = message.get("ts", "")
                msg_time = time.strftime("%H:%M:%S", time.localtime(float(ts)))

                print(f"[메시지 {idx}] 시간: {msg_time}, 텍스트: {text[:80]}")

                for team_name in team_names:
                    if team_name in text:
                        team_threads[team_name] = message["ts"]
                        print(f"  -> {team_name} 스레드 발견 (ts: {ts})")
                        break

            # 멘션 발송
            for team_name, thread_ts in team_threads.items():
                mention = team_entries[team_name]["mention"]
                reply_text = f"{mention} 스크럼 작성 부탁드립니다."
                print(f"\n{team_name} 멘션 발송: {mention}")

                slack_client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=reply_text,
                )
                print(f"  -> 발송 완료")

        print("\n=== 스크럼 멘션 스케줄러 완료 ===")

    except Exception as e:
        print(f"Error posting scrum mentions: {e}")
        raise


if __name__ == "__main__":
    main()
