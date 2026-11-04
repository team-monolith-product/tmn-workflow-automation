"""ops@ 받은 메일 처리와 정리 작업 테스트"""

import asyncio
import datetime

import pytest

from ops_mail_fakes import CHANNEL, FakeSlack, fake_db, parse_headers, raw_mail
from service.ops_mail import inbound


def test_같은_상대의_메일은_그_상대_스레드에_첨부와_함께_단다(monkeypatch):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: raw_mail())
    inserted = fake_db(monkeypatch, inbound, thread="ts-office")

    # When : 이미 스레드가 있는 상대에게서 메일이 오면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 새 스레드를 열지 않고 그 스레드에 메일과 첨부를 달고, 상대 주소로 남긴다
    slack = FakeSlack.last
    assert [post["thread_ts"] for post in slack.posts] == ["ts-office"]
    assert slack.uploads[0]["thread_ts"] == "ts-office"
    assert inserted[0].counterpart == "office@abc.kr"
    assert inserted[0].ses_message_id == "abc"


def test_처음_오는_상대는_스레드를_열고_바이러스_의심이면_첨부를_올리지_않는다(
    monkeypatch,
):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(
        inbound,
        "get_object",
        lambda bucket, key: raw_mail({"X-SES-Virus-Verdict": "FAIL"}),
    )
    sent_before = {
        "subject": "2학기 안내",
        "sent_by": "U1",
        "created_at": datetime.datetime(2026, 11, 3, 10, 0),
    }
    fake_db(monkeypatch, inbound, thread=None, sent_to=[sent_before])

    # When : 새 메일로 먼저 보냈던 상대가 처음 회신했는데 바이러스 의심이면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 먼저 보낸 메일을 보여주며 스레드를 열어 그 상대의 스레드로 등록하고,
    #        회신을 달되 첨부는 올리지 않는다
    slack = FakeSlack.last
    assert "thread_ts" not in slack.posts[0]
    assert "먼저 보낸 메일: 2학기 안내" in slack.posts[0]["text"]
    assert inbound.store.claimed == [("office@abc.kr", "ts-1")]
    assert slack.posts[1]["thread_ts"] == "ts-1"
    assert "바이러스 의심" in slack.posts[1]["text"]
    assert slack.uploads == []


def test_같은_상대의_스레드를_다른_쪽이_먼저_열었으면_내_것을_지우고_그쪽에_단다(
    monkeypatch,
):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: raw_mail())
    fake_db(monkeypatch, inbound, thread=None, claimed_by="ts-other")

    # When : 스레드를 열었는데 같은 상대의 다른 메일이 먼저 스레드를 등록했으면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 내가 연 스레드는 지우고 먼저 등록된 스레드에 단다
    slack = FakeSlack.last
    assert slack.deletes == [{"channel": CHANNEL, "ts": "ts-1"}]
    assert slack.posts[1]["thread_ts"] == "ts-other"


def test_다른_쪽이_처리했거나_처리_중인_원본은_슬랙에_올리지_않는다(monkeypatch):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    read = []
    monkeypatch.setattr(
        inbound, "get_object", lambda bucket, key: read.append(key) or raw_mail()
    )
    FakeSlack.last = None
    inserted = fake_db(monkeypatch, inbound, thread="ts-office", attempt=None)

    # When : SNS 재전송이나 정리 작업이 이미 맡겨진 원본을 다시 집으면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 자리를 못 잡아서 원본을 읽지도, 슬랙에 올리지도, 남기지도 않는다
    assert read == []
    assert FakeSlack.last is None
    assert inserted == []


def test_올리다_실패하면_끝났다고_표시하지_않아_정리_작업이_다시_맡는다(monkeypatch):
    class _FailingUpload(FakeSlack):
        async def files_upload_v2(self, **kwargs):
            raise ConnectionError

    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", _FailingUpload)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: raw_mail())
    inserted = fake_db(monkeypatch, inbound, thread="ts-office")

    # When : 메일은 올렸는데 첨부 올리기가 실패하면
    with pytest.raises(ConnectionError):
        asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 끝났다고 남기지 않아 자리를 잡은 지 시간이 지나면 정리 작업이 다시 맡고,
    #        아직 횟수가 남아 채널에 알리지 않는다
    assert inserted == []
    assert len(FakeSlack.last.posts) == 1


def _reconcile(monkeypatch, objects, process, *, settled=(), given_up=()):
    from scripts import reconcile_ops_mail

    alerted = []

    store = reconcile_ops_mail.store
    monkeypatch.setattr(
        store, "settled", lambda ids: {key.rsplit("/", 1)[-1] for key in settled}
    )
    monkeypatch.setattr(store, "given_up", lambda: list(given_up))
    monkeypatch.setattr(store, "mark_alerted", alerted.append)

    monkeypatch.setenv("OPS_MAIL_S3_BUCKET", "bucket")
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(reconcile_ops_mail, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(reconcile_ops_mail, "list_objects", lambda b, p: objects)
    monkeypatch.setattr(reconcile_ops_mail, "process_object", process)
    FakeSlack.last = None
    reconcile_ops_mail.main()
    return alerted


def test_정리_작업은_횟수를_다_쓴_원본을_채널에_한_번_알린다(monkeypatch):
    async def process(bucket, key):
        raise AssertionError(key)

    # When : 횟수를 다 썼는데 아직 알리지 않은 원본이 있으면
    alerted = _reconcile(
        monkeypatch,
        [],
        process,
        given_up=[
            {
                "ses_message_id": "bad",
                "s3_key": "ops/bad",
                "attempts": 3,
                "last_error": "ValueError: 깨진 MIME",
            },
            {
                "ses_message_id": "died",
                "s3_key": "ops/died",
                "attempts": 1,
                "last_error": None,
            },
        ],
    )

    # Then : 더 다시 하지 않는다는 것과 이유, 원본 위치를 채널에 남기고, 알렸다고 표시한다.
    #        이유가 없으면 처리 중에 서버가 멈춘 것이다
    bad, died = (post["text"] for post in FakeSlack.last.posts)
    assert "3번 처리하지 못해 더 다시 하지 않습니다" in bad
    assert "ValueError: 깨진 MIME" in bad
    assert "s3://bucket/ops/bad" in bad
    # 한 번만 맡고 하루가 지난 원본은 세 번 실패했다고 하지 않는다
    assert "하루 넘게 끝나지 않아" in died and "3번" not in died
    assert "서버가 멈춤" in died
    assert alerted == ["bad", "died"]


def test_처리가_실패하면_이유를_남긴다(monkeypatch):
    def broken(bucket, key):
        raise ValueError("깨진 MIME")

    failed = []
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(inbound, "get_object", broken)
    fake_db(monkeypatch, inbound, thread=None)
    monkeypatch.setattr(
        inbound.store,
        "fail",
        lambda ses_id, error: failed.append((ses_id, error)),
    )

    # When : 원본을 처리하다 실패하면
    with pytest.raises(ValueError):
        asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 그 이유를 남겨 횟수를 다 쓰면 알림에 보이게 한다
    assert failed == [("abc", "ValueError: 깨진 MIME")]


def _inbound(monkeypatch, headers: dict[str, str], *, thread: str | None):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: raw_mail(headers))
    inserted = fake_db(monkeypatch, inbound, thread=thread)
    asyncio.run(inbound.process_object("bucket", "ops/abc"))
    return FakeSlack.last, inserted, inbound.store.claimed


def test_자동응답은_스레드가_없으면_슬랙에_올리지_않고_있으면_그_스레드에_단다(
    monkeypatch,
):
    # When : 부재중 자동응답이 스레드 없는 상대와 스레드 있는 상대에게서 오면
    new_slack, new_inserted, new_claimed = _inbound(
        monkeypatch, {"Auto-Submitted": "auto-replied"}, thread=None
    )
    old_slack, _, _ = _inbound(monkeypatch, {"Precedence": "auto_reply"}, thread="ts-x")

    # Then : 스레드 없는 상대는 기록만 남기고, 있는 상대는 그 스레드에 단다.
    #        자동응답이라고 남겨 답장할 메일을 고를 때 건너뛴다
    assert new_slack.posts == [] and len(new_inserted) == 1
    assert new_inserted[0].auto_reply
    assert new_claimed == []
    assert [post["thread_ts"] for post in old_slack.posts] == ["ts-x"]


def test_정리_작업은_오래된_미처리_원본만_다시_처리한다(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    objects = [
        {"Key": "ops/done", "LastModified": now - datetime.timedelta(hours=1)},
        {"Key": "ops/lost", "LastModified": now - datetime.timedelta(hours=1)},
        {"Key": "ops/new", "LastModified": now},
        # 아무도 맡지 못한 채 며칠 지난 원본(주말 장애)은 다시 처리한다
        {"Key": "ops/weekend", "LastModified": now - datetime.timedelta(days=3)},
        {"Key": "ops/old", "LastModified": now - datetime.timedelta(days=8)},
    ]
    handled = []

    async def process(bucket, key):
        handled.append(key)

    # When : 처리된 것, 처리 안 된 것, 방금 들어온 것, 오래된 것이 섞여 있으면
    _reconcile(monkeypatch, objects, process, settled=["ops/done"])

    # Then : 웹훅이 처리 중일 수 있는 방금 것과 오래된 것은 두고,
    #        처리 안 된 것만 다시 처리하며, 알릴 것이 없으면 슬랙을 부르지 않는다
    assert handled == ["ops/lost", "ops/weekend"]
    assert FakeSlack.last is None


def test_정리_작업은_원본_하나가_실패해도_나머지를_처리한다(monkeypatch):
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    objects = [
        {"Key": "ops/bad", "LastModified": old},
        {"Key": "ops/ok", "LastModified": old},
    ]
    handled = []

    async def process(bucket, key):
        if key == "ops/bad":
            raise ValueError
        handled.append(key)

    # When : 앞의 원본이 실패하면
    _reconcile(monkeypatch, objects, process)

    # Then : 뒤의 원본도 처리한다
    assert handled == ["ops/ok"]


def test_보낸_사람_주소가_없는_원본은_슬랙에_올리지_않고_남긴다(monkeypatch):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(
        inbound, "get_object", lambda bucket, key: b"Amazon SES setup notification"
    )
    FakeSlack.last = None
    inserted = fake_db(monkeypatch, inbound, thread=None)

    # When : SES 안내처럼 보낸 사람이 없는 원본이 오면
    asyncio.run(inbound.process_object("bucket", "ops/AMAZON_SES_SETUP_NOTIFICATION"))

    # Then : 주소 없는 스레드를 열지 않고 기록만 남겨 다시 처리하지 않는다
    assert FakeSlack.last is None
    assert inserted[0].counterpart == ""


def test_회신_주소가_보낸_사람과_다르면_드러낸다():
    # When : 회신 주소에 다른 사람 주소를 적은 메일을 슬랙 글로 만들면
    text = inbound.format_inbound(
        parse_headers("From: me@evil.kr\nReply-To: partner@school.kr"), "s3://b/k"
    )

    # Then : 답장이 어디로 가는지 경고한다
    assert "회신 주소가 보낸 사람과 다릅니다" in text
    assert "partner@school.kr" in text


def test_메일링_리스트나_대량_메일은_처음_오는_상대여도_스레드를_연다(monkeypatch):
    # When : 처음 오는 상대의 메일이 구글 그룹·대량 발송 표시를 달고 오면
    slack, inserted, claimed = _inbound(
        monkeypatch,
        {"Precedence": "list", "Auto-Submitted": "auto-generated"},
        thread=None,
    )

    # Then : 자동응답으로 치지 않고 스레드를 열어 올린다
    assert claimed == [("office@abc.kr", "ts-1")]
    assert len(slack.posts) == 2
    assert not inserted[0].auto_reply


def test_받은_메일의_슬랙_문법은_글자로_올린다(monkeypatch):
    raw = (
        b"From: <!channel> <x@evil.kr>\r\nSubject: <https://evil.kr|https://bank.com>\r\n\r\n"
        b"<!here> <@U123>\r\n"
    )
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: raw)
    fake_db(monkeypatch, inbound, thread=None)

    # When : 보낸 사람·제목·본문에 멘션과 위장 링크를 넣은 메일이 오면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 스레드 첫 글과 메일 글 어디에도 슬랙 문법이 살아 있지 않다
    texts = " ".join(post["text"] for post in FakeSlack.last.posts)
    for markup in ("<!channel>", "<!here>", "<@U123>", "<https://evil.kr|"):
        assert markup not in texts
