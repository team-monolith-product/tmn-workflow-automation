from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from scripts import summarize_tko_updates as job


def test_merge_window(monkeypatch):
    end = datetime(2026, 9, 20, 9, tzinfo=job.KST)
    start = end - timedelta(days=1)
    times = [None, start - timedelta(seconds=1), start, end - timedelta(seconds=1), end]
    pulls = [
        SimpleNamespace(
            number=i,
            merged_at=t.astimezone(timezone.utc) if t else None,
            title=f"PR {i}",
            body="",
            get_files=lambda: [
                SimpleNamespace(filename="game.gd", status="modified", patch="diff")
            ],
        )
        for i, t in enumerate(times)
    ]
    fetch = MagicMock(return_value={job.REPOSITORY: list(reversed(pulls))})
    monkeypatch.setattr(job, "fetch_pull_requests_parallel", fetch)
    github = MagicMock()
    result = job.collect_updates(github, end)
    assert [pr["number"] for pr in result] == [2, 3]
    assert result[0]["files"][0]["patch"] == "diff"
    fetch.assert_called_once_with(github, [job.REPOSITORY], start)


@pytest.mark.parametrize(
    "hour, expected_day, pulls, items, dry_run",
    [
        (9, 20, [{"title": "fix"}], ["서버에 연결하지 못하는 이슈 완화"], False),
        (10, 20, [{"title": "fix"}], ["장판 생성 오류 수정"], True),
        (8, 19, [], [], False),
        (9, 20, [{"title": "CI"}], [], False),
    ],
)
def test_delivery(monkeypatch, capsys, hour, expected_day, pulls, items, dry_run):
    monkeypatch.setenv("GITHUB_TOKEN", "test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test")
    monkeypatch.setattr(job, "load_dotenv", lambda: None)
    monkeypatch.setattr(job, "Github", MagicMock())
    collect = MagicMock(return_value=pulls)
    monkeypatch.setattr(job, "collect_updates", collect)
    openai = MagicMock()
    parse = openai.return_value.__enter__.return_value.beta.chat.completions.parse
    parse.return_value.choices = [
        SimpleNamespace(message=SimpleNamespace(parsed=job.Updates(items=items)))
    ]
    monkeypatch.setattr(job, "OpenAI", openai)
    slack = MagicMock()
    monkeypatch.setattr(job, "WebClient", slack)
    job.main(dry_run=dry_run, now=datetime(2026, 9, 20, hour, 5, tzinfo=job.KST))
    assert collect.call_args.args[1] == datetime(
        2026, 9, expected_day, 9, tzinfo=job.KST
    )
    if not pulls:
        openai.assert_not_called()
    if items and not dry_run:
        slack.return_value.chat_postMessage.assert_called_once_with(
            channel="C0C10FTGNTH",
            text="\n".join(items),
            mrkdwn=False,
            unfurl_links=False,
            unfurl_media=False,
        )
    else:
        slack.assert_not_called()
    if dry_run:
        assert capsys.readouterr().out.strip() == "\n".join(items)


def test_daily_schedule():
    path = job.Path(job.__file__).parent.parent / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    scheduled = next(
        entry
        for entry in config["scheduled_jobs"]
        if entry["name"] == "summarize_tko_updates"
    )
    assert scheduled["cron"] == {"hour": 9, "minute": 0}
    assert scheduled["business_day_only"] is False
    assert scheduled["module"] == job.__name__
    assert scheduled["function"] == "main"
