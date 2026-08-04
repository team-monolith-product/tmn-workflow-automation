"""
지식베이스 수집 채널을 등록하거나 내립니다.

data_source 테이블이 SOT입니다. 이 스크립트가 유일한 등록 인터페이스이고,
등록과 동시에 봇을 채널에 입장시켜 둘이 어긋난 상태를 만들지 않습니다.

내릴 때는 enabled만 끄고 채널에서 나가지 않습니다. 대표 봇이 버그 라우팅
같은 다른 역할로 같은 채널에 있을 수 있어서, 멤버십은 수집 여부와 무관하게
둡니다. 적재 여부는 어차피 data_source.enabled가 정합니다.

사용법:
    python scripts/register_knowledge_channel.py C04F0S33HCL
    python scripts/register_knowledge_channel.py C04F0S33HCL --disable
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
import os

from dotenv import load_dotenv
from slack_sdk import WebClient

from service.knowledge.db import connect

UPSERT = """
INSERT INTO data_source (source, external_id, name, enabled, joined_at)
VALUES ('slack', %(channel_id)s, %(name)s, true, now())
ON CONFLICT (source, external_id) DO UPDATE SET
    name      = EXCLUDED.name,
    enabled   = true,
    joined_at = coalesce(data_source.joined_at, EXCLUDED.joined_at)
RETURNING id
"""

DISABLE = """
UPDATE data_source SET enabled = false
WHERE source = 'slack' AND external_id = %(channel_id)s
RETURNING id
"""


def register(client: WebClient, channel_id: str) -> None:
    """채널을 등록하고 봇을 입장시킵니다.

    Args:
        client: Slack 클라이언트
        channel_id: Slack 채널 ID
    """
    info = client.conversations_info(channel=channel_id)["channel"]
    # 공개 채널만 수집한다. DM에 API 키가 평문으로 오간 사례를 확인했다.
    if info.get("is_private") or not info.get("is_channel"):
        raise SystemExit(f"공개 채널이 아닙니다: {channel_id}")

    client.conversations_join(channel=channel_id)

    with connect() as conn:
        row = conn.execute(UPSERT, {"channel_id": channel_id, "name": info["name"]})
        print(
            f"등록됨: #{info['name']} ({channel_id}) → data_source {row.fetchone()['id']}"
        )


def disable(channel_id: str) -> None:
    """채널 수집을 내립니다. 봇은 채널에 남습니다.

    Args:
        channel_id: Slack 채널 ID
    """
    with connect() as conn:
        row = conn.execute(DISABLE, {"channel_id": channel_id}).fetchone()
    if row is None:
        raise SystemExit(f"등록된 적 없는 채널입니다: {channel_id}")
    print(f"내려감: {channel_id} → data_source {row['id']}")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="지식베이스 수집 채널 등록")
    parser.add_argument("channel_id", help="Slack 채널 ID (C로 시작)")
    parser.add_argument(
        "--disable", action="store_true", help="수집을 내린다. 봇은 채널에 남는다"
    )
    args = parser.parse_args()

    if args.disable:
        disable(args.channel_id)
    else:
        register(WebClient(token=os.environ["SLACK_BOT_TOKEN"]), args.channel_id)


if __name__ == "__main__":
    main()
