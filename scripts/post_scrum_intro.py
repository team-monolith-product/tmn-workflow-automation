"""
매일 09:00에 스크럼 안내 + 팀/개인 스크럼 메인(껍데기) 메시지를 발송하는 스크립트

태스크 요약 답글은 16:00 post_scrum_message가 같은 스레드에 추가하고,
작성 멘션은 16:30 schedule_scrum_mention이 같은 스레드에 추가한다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

import sentry_sdk
from dotenv import load_dotenv
from slack_sdk import WebClient

from service.config import PersonalScrum, ScrumSquadConfig, load_config

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="스크럼 안내/껍데기 메시지 발송 스크립트"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="메시지를 Slack에 전송하지 않고 콘솔에만 출력합니다.",
    )
    args = parser.parse_args()

    config = load_config()
    slack_client = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))

    if args.dry_run:
        print("=== DRY RUN MODE ===")

    scrum = config.scrum
    squad_channel_ids = list(dict.fromkeys(s.channel_id for s in scrum.squads))

    for channel_id in squad_channel_ids:
        send_intro_message(slack_client, channel_id, squad_channel_ids, args.dry_run)

    for squad in scrum.squads:
        try:
            send_team_scrum_shell(slack_client, squad, args.dry_run)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            print(f"Error in send_team_scrum_shell for {squad.squad.display_name}: {e}")

    for personal in scrum.personal_scrums:
        try:
            send_personal_scrum(slack_client, personal, args.dry_run)
        except Exception as e:
            sentry_sdk.capture_exception(e)
            print(f"Error in send_personal_scrum for {personal.name}: {e}")

    if args.dry_run:
        print("\n=== DRY RUN COMPLETED ===")


def send_intro_message(
    slack_client: WebClient,
    channel_id: str,
    all_channel_ids: list[str],
    dry_run: bool = False,
):
    """채널에 스크럼 안내 메시지(메인 + 스레드 상세) 발송"""
    intro_text = (
        "좋은 아침입니다. 오늘 업무도 즐겁게 시작해서 마무리 잘 해봐요. "
        "스크럼은 4:40까지 작성 부탁드립니다!"
    )

    other_channels = " ".join(
        f"<#{cid}>" for cid in all_channel_ids if cid != channel_id
    )

    detail_text = f"""스크럼의 목적은 팀내의 진행상황을 확인하고, 장애를 파악하는 것입니다.
팀원이 스크럼 중 장애를 보고하면 각 마스터(김예원 엄은상 이창환)는
장애를 최소화 하기 위해 스크럼 후 다른 팀의 협조로 연계해주시면 되겠습니다.
다른 팀 스크럼이 궁금하시면 본인 팀의 스크럼이 끝나고 서면으로 남겨진 내용을 자유롭게 확인하시면 됩니다.
{other_channels}
다른 팀의 지원이 필요하시면 본인 팀 스크럼 때, 마스터를 통해 지원을 요청 주시면 됩니다.
(사실 스크럼이 아니더라도 업무 중 자유롭게 요청 주셔도 됩니다.)
---------------------------------------------------------------------------
오늘 한 일:
- XXX 운영 진행
내일 할 일:
- YYY 건에 대한 대응
오늘의 이슈:
- ZZZ 문제 발생, 해결 방안 모색 중
- AAA 이슈로 B팀과 협업 요청"""

    if dry_run:
        print(f"\n[안내 메시지] 채널: {channel_id}")
        print(intro_text)
        print(f"  └─ 스레드: {detail_text[:100]}...")
        return

    response = slack_client.chat_postMessage(channel=channel_id, text=intro_text)
    slack_client.chat_postMessage(
        channel=channel_id,
        thread_ts=response["ts"],
        text=detail_text,
    )


def send_team_scrum_shell(
    slack_client: WebClient,
    squad: ScrumSquadConfig,
    dry_run: bool = False,
):
    """팀 스크럼 메인 메시지(껍데기) 발송. 16시 태스크 요약 답글이 이 스레드에 달림."""
    text = squad.squad.display_name

    if dry_run:
        print(f"\n[{squad.squad.display_name}] 채널: {squad.channel_id}")
        print(text)
        return

    slack_client.chat_postMessage(channel=squad.channel_id, text=text)


def send_personal_scrum(
    slack_client: WebClient,
    personal: PersonalScrum,
    dry_run: bool = False,
):
    """개인 스크럼 메인 메시지 발송"""
    text = personal.name

    if dry_run:
        print(f"\n[{personal.name}] 채널: {personal.channel_id}")
        print(text)
        return

    slack_client.chat_postMessage(channel=personal.channel_id, text=text)


if __name__ == "__main__":
    main()
