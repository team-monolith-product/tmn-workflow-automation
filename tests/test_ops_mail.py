"""ops@ 메일 수신·발송 테스트"""

import asyncio
import base64
import datetime
import json
from contextlib import contextmanager
from email.message import EmailMessage

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ops_mail, ops_mail_sns
from api import ses
from service.ops_mail import draft as drafts
from service.ops_mail import inbound, mime, sns

TOPIC = "arn:aws:sns:ap-northeast-2:000000000000:tmn-topic-ops-mail"
CHANNEL = "C_OPS"


def _raw_mail(headers: dict[str, str] | None = None) -> bytes:
    """본문(텍스트+HTML), 첨부 하나, 서명 로고(본문 삽입 이미지) 하나가 든 메일."""
    message = EmailMessage()
    for name, value in (headers or {}).items():
        message[name] = value
    message["Message-ID"] = "<m2@abc.kr>"
    message["From"] = "김선생 <Kim@ABC.kr>"
    message["Reply-To"] = "office@abc.kr"
    message["To"] = "ops@team-mono.com"
    message["Subject"] = "견적 문의"
    message["References"] = "<m0@abc.kr> <m1@abc.kr>"
    message["In-Reply-To"] = "<m1@abc.kr>"
    message.set_content("견적 부탁드립니다")
    message.add_alternative("<p>견적 <b>부탁</b>드립니다</p>", subtype="html")
    message.add_attachment(
        b"%PDF", maintype="application", subtype="pdf", filename="계획안.pdf"
    )
    message.add_attachment(
        b"PNG", maintype="image", subtype="png", disposition="inline", cid="<logo>"
    )
    return message.as_bytes()


def test_메일을_읽으면_텍스트_본문과_첨부만_꺼낸다():
    # When : 텍스트·HTML 본문, 첨부, 본문 삽입 로고가 든 메일을 읽으면
    mail = mime.parse(_raw_mail())

    # Then : 텍스트 본문과 진짜 첨부만 남고, 상대 주소는 Reply-To 를 소문자로 쓴다
    assert mail.body_text == "견적 부탁드립니다"
    assert [a.filename for a in mail.attachments] == ["계획안.pdf"]
    assert mail.from_address == "kim@abc.kr"
    assert mail.reply_to_address == "office@abc.kr"
    assert mail.references_ids == ["<m0@abc.kr>", "<m1@abc.kr>"]


def test_HTML만_있으면_태그를_걷어낸다():
    message = EmailMessage()
    message["From"] = "a@b.kr"
    message.set_content("<p>안녕<br>하세요</p>", subtype="html")

    # When : HTML 본문만 있는 메일을 읽으면
    mail = mime.parse(message.as_bytes())

    # Then : 태그 없는 텍스트가 나온다
    assert mail.body_text == "안녕\n하세요"


def test_답장_원문은_마지막_메일에_건다():
    # When : 이어지는 메일 두 통을 references 로 넘겨 답장을 만들면
    message_id, raw = mime.build(
        from_header="팀모노리스 <ops@team-mono.com>",
        to_address="office@abc.kr",
        subject=mime.reply_subject("Re: 견적 문의"),
        body="안내드립니다",
        references_ids=["<m1@abc.kr>", "<m2@abc.kr>"],
        attachments=[],
    )
    sent = mime.parse(raw)

    # Then : In-Reply-To 는 마지막 메일, References 는 전부, 제목에 Re: 가 겹치지 않는다
    assert b"In-Reply-To: <m2@abc.kr>" in raw
    assert sent.references_ids == ["<m1@abc.kr>", "<m2@abc.kr>"]
    assert sent.subject == "Re: 견적 문의"
    assert message_id.endswith("@team-mono.com>")


def test_보낼_메일에_파일을_붙인다():
    # When : 첨부 하나를 넣어 원문을 만들면
    _, raw = mime.build(
        from_header="팀모노리스 <ops@team-mono.com>",
        to_address="office@abc.kr",
        subject="자료",
        body="첨부 확인 부탁드립니다",
        references_ids=[],
        attachments=[mime.Attachment("계획안.pdf", "application/pdf", b"%PDF")],
    )
    sent = mime.parse(raw)

    # Then : 받는 쪽에서 읽으면 본문과 같은 파일이 나온다
    assert sent.body_text == "첨부 확인 부탁드립니다"
    assert sent.attachments == [
        mime.Attachment("계획안.pdf", "application/pdf", b"%PDF")
    ]


def _slack_file(name: str, size: int = 100) -> dict:
    return {
        "name": name,
        "mimetype": "application/pdf",
        "size": size,
        "url_private_download": f"https://files.slack.com/{name}",
    }


def test_첨부는_스레드에_올라온_파일에서만_고른다():
    messages = [
        {"text": "자료", "files": [_slack_file("계획안.pdf", 1)]},
        {"text": "고친 것", "files": [_slack_file("계획안.pdf", 2)]},
        {"text": "답장해줘"},
    ]

    # When : 있는 파일과 없는 파일을 고르면
    picked = drafts.pick_files(messages, ["계획안.pdf"])
    with pytest.raises(drafts.DraftProblem) as missing:
        drafts.pick_files(messages, ["견적서.pdf"])

    # Then : 있는 것은 나중에 올린 것을 쓰고, 없으면 스레드의 파일 목록을 알려준다
    assert [(f["name"], f["size"]) for f in picked] == [("계획안.pdf", 2)]
    assert "견적서.pdf 파일이 없습니다" in str(missing.value)
    assert "계획안.pdf" in str(missing.value)


def test_첨부가_너무_크면_초안을_막는다():
    messages = [{"files": [_slack_file("큰파일.zip", drafts.MAX_ATTACHMENT_BYTES + 1)]}]

    # When : 상한을 넘는 파일을 고르면
    with pytest.raises(drafts.DraftProblem) as result:
        drafts.pick_files(messages, ["큰파일.zip"])

    # Then : 보낼 수 없다고 한다
    assert "보낼 수 없습니다" in str(result.value)


def _signed_message(key, extra: dict | None = None) -> dict:
    message = {
        "Type": "Notification",
        "MessageId": "id-1",
        "TopicArn": TOPIC,
        "Message": "{}",
        "Timestamp": "2026-11-03T00:00:00.000Z",
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.ap-northeast-2.amazonaws.com/cert.pem",
    }
    signature = key.sign(
        sns.string_to_sign(message), padding.PKCS1v15(), hashes.SHA256()
    )
    message["Signature"] = base64.b64encode(signature).decode()
    message.update(extra or {})
    return message


def _cert(key) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def test_SNS_서명이_맞으면_통과하고_본문이_바뀌면_막는다():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # When : 서명한 뒤 Message 를 바꾸면
    signed = _signed_message(key)
    tampered = _signed_message(key, {"Message": '{"x":1}'})

    # Then : 원본은 통과하고 바뀐 것은 막힌다
    assert sns.verify(signed, _cert(key))
    assert not sns.verify(tampered, _cert(key))


def test_인증서_URL이_AWS가_아니면_막는다():
    good = "https://sns.ap-northeast-2.amazonaws.com/SimpleNotificationService-a.pem"

    # When / Then : sns.<region>.amazonaws.com 의 https .pem 만 받고, 받아올 URL 은
    #               검사한 호스트와 경로로만 다시 만든다
    assert sns.aws_cert_url(good, TOPIC) == good
    for bad in (
        "https://sns.ap-northeast-2.amazonaws.com.evil.kr/a.pem",
        "http://sns.ap-northeast-2.amazonaws.com/a.pem",
        # urlparse 는 AWS 로, requests 는 evil.kr 로 읽는다
        "https://evil.kr\\@sns.ap-northeast-2.amazonaws.com/a.pem",
        "https://u@sns.ap-northeast-2.amazonaws.com/a.pem",
        "https://sns.ap-northeast-2.amazonaws.com:8443/a.pem",
        # 남의 S3 버킷 "sns" 의 주소들
        "https://sns.s3.amazonaws.com/a.pem",
        "https://sns.s3-us-west-2.amazonaws.com/a.pem",
        # 토픽과 다른 리전
        "https://sns.us-east-1.amazonaws.com/a.pem",
    ):
        assert sns.aws_cert_url(bad, TOPIC) is None, bad


def test_SNS_서명_대상은_AWS_문서의_모양이다():
    message = {
        "Type": "Notification",
        "MessageId": "id-1",
        "TopicArn": TOPIC,
        "Message": "{}",
        "Timestamp": "2026-11-03T00:00:00.000Z",
    }

    # When : Subject 없는 알림의 서명 대상을 만들면
    signed = sns.string_to_sign(message)

    # Then : 정해진 키 순서로 "키\n값\n" 을 잇고, 없는 Subject 는 키째 빠진다
    #        (https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html)
    assert (
        signed
        == (
            "Message\n{}\nMessageId\nid-1\n"
            "Timestamp\n2026-11-03T00:00:00.000Z\n"
            f"TopicArn\n{TOPIC}\nType\nNotification\n"
        ).encode()
    )


def test_SNS_검증은_검사한_URL_에서만_인증서를_받는다(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fetched = []

    class _Response:
        content = _cert(key)

        def raise_for_status(self):
            pass

    def get(url, timeout, allow_redirects):
        # 다른 곳으로 넘겨 주면 따라가지 않는다
        assert allow_redirects is False
        fetched.append(url)
        return _Response()

    monkeypatch.setattr(sns.requests, "get", get)
    sns._certs.clear()
    cert_url = "https://sns.ap-northeast-2.amazonaws.com/a.pem"

    # When : 서명이 맞는 메시지와, 인증서 URL 만 공격자 쪽으로 바꾼 메시지를 확인하면
    good = sns.is_authentic(_signed_message(key, {"SigningCertURL": cert_url}))
    forged = sns.is_authentic(
        _signed_message(
            key,
            {
                "SigningCertURL": "https://evil.kr\\@sns.ap-northeast-2.amazonaws.com/a.pem"
            },
        )
    )

    # Then : 맞는 것만 통과하고, 인증서는 AWS 호스트에서만 받는다
    assert good and not forged
    assert fetched == [cert_url]


def _client(monkeypatch, authentic: bool = True):
    monkeypatch.setenv("OPS_MAIL_SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setattr(sns, "is_authentic", lambda message: authentic)
    app = FastAPI()
    app.include_router(ops_mail_sns.router)
    return TestClient(app)


def test_다른_토픽이나_서명이_틀린_요청은_막는다(monkeypatch):
    # When : 다른 토픽에서 왔거나 서명이 틀리면
    other_topic = _client(monkeypatch).post(
        "/ops-mail/sns",
        content=json.dumps({"TopicArn": "arn:other", "Type": "Notification"}),
    )
    bad_signature = _client(monkeypatch, authentic=False).post(
        "/ops-mail/sns", content=json.dumps({"TopicArn": TOPIC, "Type": "Notification"})
    )

    # Then : 각각 403, 401 로 막는다
    assert other_topic.status_code == 403
    assert bad_signature.status_code == 401


def test_수신_알림은_응답_뒤에_처리한다(monkeypatch):
    handled = []
    monkeypatch.setattr(
        ops_mail_sns, "process_object", lambda bucket, key: handled.append(key)
    )
    notification = {
        "notificationType": "Received",
        "receipt": {"action": {"bucketName": "bucket", "objectKey": "ops/abc"}},
    }

    # When : SES 수신 알림이 오면
    response = _client(monkeypatch).post(
        "/ops-mail/sns",
        content=json.dumps(
            {
                "TopicArn": TOPIC,
                "Type": "Notification",
                "Message": json.dumps(notification),
            }
        ),
    )

    # Then : 받았다고 답하고 그 원본을 처리한다
    assert response.json() == {"status": "accepted"}
    assert handled == ["ops/abc"]


class _FakeSlack:
    token = "xoxb-test"

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.uploads = []
        self.updates = []
        self.deletes = []
        self.ephemerals = []
        _FakeSlack.last = self

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"ts-{len(self.posts)}"}

    async def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)

    async def chat_delete(self, **kwargs):
        self.deletes.append(kwargs)

    async def chat_postEphemeral(self, **kwargs):
        self.ephemerals.append(kwargs)


def _fake_db(
    monkeypatch,
    module,
    *,
    thread: str | None,
    target: dict | None = None,
    sent_to: list[dict] | None = None,
    claimed_by: str | None = None,
    attempt: int | None = 1,
):
    inserted = []
    claimed = []

    def finish(conn, row):
        inserted.append(row)

    @contextmanager
    def connect():
        yield None

    def claim_thread(conn, channel, counterpart, ts):
        claimed.append((counterpart, ts))
        return claimed_by or ts

    # store 함수는 모두 store.run 이 연 커넥션으로 불린다.
    monkeypatch.setattr(module.store, "connect", connect)
    monkeypatch.setattr(module.store, "claim", lambda conn, ses_id, key: attempt)
    monkeypatch.setattr(module.store, "finish", finish)
    monkeypatch.setattr(module.store, "find_thread", lambda conn, channel, cp: thread)
    monkeypatch.setattr(module.store, "reply_target", lambda conn, c, t: target)
    monkeypatch.setattr(
        module.store, "sent_to", lambda conn, c, cp: list(sent_to or [])
    )
    monkeypatch.setattr(module.store, "claim_thread", claim_thread)
    monkeypatch.setattr(module.store, "claimed", claimed, raising=False)
    return inserted


def test_같은_상대의_메일은_그_상대_스레드에_첨부와_함께_단다(monkeypatch):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: _raw_mail())
    inserted = _fake_db(monkeypatch, inbound, thread="ts-office")

    # When : 이미 스레드가 있는 상대에게서 메일이 오면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 새 스레드를 열지 않고 그 스레드에 메일과 첨부를 달고, 상대 주소로 남긴다
    slack = _FakeSlack.last
    assert [post["thread_ts"] for post in slack.posts] == ["ts-office"]
    assert slack.uploads[0]["thread_ts"] == "ts-office"
    assert inserted[0].counterpart == "office@abc.kr"
    assert inserted[0].ses_message_id == "abc"


def test_처음_오는_상대는_스레드를_열고_바이러스_의심이면_첨부를_올리지_않는다(
    monkeypatch,
):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(
        inbound,
        "get_object",
        lambda bucket, key: _raw_mail({"X-SES-Virus-Verdict": "FAIL"}),
    )
    sent_before = {
        "subject": "2학기 안내",
        "sent_by": "U1",
        "created_at": datetime.datetime(2026, 11, 3, 10, 0),
    }
    _fake_db(monkeypatch, inbound, thread=None, sent_to=[sent_before])

    # When : 새 메일로 먼저 보냈던 상대가 처음 회신했는데 바이러스 의심이면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 먼저 보낸 메일을 보여주며 스레드를 열어 그 상대의 스레드로 등록하고,
    #        회신을 달되 첨부는 올리지 않는다
    slack = _FakeSlack.last
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
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: _raw_mail())
    _fake_db(monkeypatch, inbound, thread=None, claimed_by="ts-other")

    # When : 스레드를 열었는데 같은 상대의 다른 메일이 먼저 스레드를 등록했으면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 내가 연 스레드는 지우고 먼저 등록된 스레드에 단다
    slack = _FakeSlack.last
    assert slack.deletes == [{"channel": CHANNEL, "ts": "ts-1"}]
    assert slack.posts[1]["thread_ts"] == "ts-other"


def test_다른_쪽이_처리했거나_처리_중인_원본은_슬랙에_올리지_않는다(monkeypatch):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    read = []
    monkeypatch.setattr(
        inbound, "get_object", lambda bucket, key: read.append(key) or _raw_mail()
    )
    _FakeSlack.last = None
    inserted = _fake_db(monkeypatch, inbound, thread="ts-office", attempt=None)

    # When : SNS 재전송이나 정리 작업이 이미 맡겨진 원본을 다시 집으면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 자리를 못 잡아서 원본을 읽지도, 슬랙에 올리지도, 남기지도 않는다
    assert read == []
    assert _FakeSlack.last is None
    assert inserted == []


def test_올리다_실패하면_끝났다고_표시하지_않아_정리_작업이_다시_맡는다(monkeypatch):
    class _FailingUpload(_FakeSlack):
        async def files_upload_v2(self, **kwargs):
            raise ConnectionError

    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", _FailingUpload)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: _raw_mail())
    inserted = _fake_db(monkeypatch, inbound, thread="ts-office")

    # When : 메일은 올렸는데 첨부 올리기가 실패하면
    with pytest.raises(ConnectionError):
        asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 끝났다고 남기지 않아 자리를 잡은 지 시간이 지나면 정리 작업이 다시 맡고,
    #        아직 횟수가 남아 채널에 알리지 않는다
    assert inserted == []
    assert len(_FakeSlack.last.posts) == 1


def _reconcile(monkeypatch, objects, process, *, settled=(), given_up=()):
    from scripts import reconcile_ops_mail

    alerted = []

    async def run(fn, *args):
        if fn is reconcile_ops_mail.store.settled:
            return {key.rsplit("/", 1)[-1] for key in settled}
        if fn is reconcile_ops_mail.store.given_up:
            return list(given_up)
        if fn is reconcile_ops_mail.store.mark_alerted:
            alerted.append(args[0])
            return None
        raise AssertionError(fn)

    monkeypatch.setenv("OPS_MAIL_S3_BUCKET", "bucket")
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(reconcile_ops_mail, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(reconcile_ops_mail, "list_objects", lambda b, p: objects)
    monkeypatch.setattr(reconcile_ops_mail.store, "run", run)
    monkeypatch.setattr(reconcile_ops_mail, "process_object", process)
    _FakeSlack.last = None
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
    bad, died = (post["text"] for post in _FakeSlack.last.posts)
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
    _fake_db(monkeypatch, inbound, thread=None)
    monkeypatch.setattr(
        inbound.store,
        "fail",
        lambda conn, ses_id, error: failed.append((ses_id, error)),
    )

    # When : 원본을 처리하다 실패하면
    with pytest.raises(ValueError):
        asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 그 이유를 남겨 횟수를 다 쓰면 알림에 보이게 한다
    assert failed == [("abc", "ValueError: 깨진 MIME")]


def test_상대_스레드_안에서는_그_상대에게_답장을_만든다():
    target = {
        "counterpart": "office@abc.kr",
        "subject": "견적 문의",
        "message_id": "<m2@abc.kr>",
        "references_ids": ["<m1@abc.kr>"],
    }

    # When : 상대 스레드에서 본문만 주거나, 다른 사람을 받는 사람으로 주면
    draft = drafts.make(
        thread_ts="ts-office", target=target, body="안내드립니다", to=[], subject=""
    )
    with pytest.raises(drafts.DraftProblem) as other:
        drafts.make(
            thread_ts="ts-office",
            target=target,
            body="안내드립니다",
            to=["x@y.kr"],
            subject="",
        )

    # Then : 그 상대에게 마지막 메일에 거는 답장이 되고, 받는 사람은 바꿀 수 없다
    assert draft.is_reply
    assert draft.recipients == ["office@abc.kr"]
    assert draft.subject == "Re: 견적 문의"
    assert draft.references_ids == ["<m1@abc.kr>", "<m2@abc.kr>"]
    assert "바꿀 수 없습니다" in str(other.value)


def test_스레드_밖에서_새_메일은_받는_사람과_제목이_있어야_한다():
    # When : 메일 스레드가 아닌 곳에서 제목 없이 부르거나, 같은 주소를 겹쳐 주면
    with pytest.raises(drafts.DraftProblem) as missing:
        drafts.make(
            thread_ts="ts-chat",
            target=None,
            body="안녕하세요",
            to=["a@b.kr"],
            subject="",
        )
    draft = drafts.make(
        thread_ts="ts-chat",
        target=None,
        body="안녕하세요",
        to=["A@B.kr", "c@d.kr", "a@b.kr"],
        subject="안내",
    )

    # Then : 제목을 달라고 하고, 다 있으면 받는 사람을 소문자로 한 번씩만 둔 새 메일이 된다
    assert "제목" in str(missing.value)
    assert draft == drafts.Draft(
        recipients=["a@b.kr", "c@d.kr"],
        subject="안내",
        body="안녕하세요",
        references_ids=[],
        thread_ts="ts-chat",
        is_reply=False,
    )


class _FakeApp:
    def __init__(self):
        self.handlers = {}

    def action(self, action_id):
        def register(handler):
            self.handlers[action_id] = handler
            return handler

        return register

    view = action


def test_보내기를_누르면_첨부를_내려받아_함께_보낸다(monkeypatch):
    sent = []
    recorded = []

    async def download(token, file):
        return mime.Attachment(file["name"], file["mimetype"], b"%PDF")

    monkeypatch.setattr(ops_mail, "_download", download)
    monkeypatch.setattr(
        ops_mail, "send_mime", lambda raw: sent.append(raw) or {"MessageId": "s"}
    )
    monkeypatch.setattr(ops_mail, "_record", _fake_record(recorded))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d1"] = drafts.Draft(
        recipients=["office@abc.kr"],
        subject="Re: 견적 문의",
        body="자료 보내드립니다",
        references_ids=["<m2@abc.kr>"],
        thread_ts="ts-office",
        is_reply=True,
        attachments=[
            {"name": "계획안.pdf", "mimetype": "application/pdf", "size": 4, "url": "u"}
        ],
    )
    slack = _FakeSlack()

    async def ack():
        pass

    # When : 카드의 [보내기] 를 누르면
    asyncio.run(
        app.handlers[ops_mail.APPROVE](
            ack=ack,
            body={
                "actions": [{"value": "d1"}],
                "container": {"channel_id": CHANNEL, "message_ts": "ts-card"},
                "user": {"id": "U1"},
            },
            client=slack,
        )
    )

    # Then : 첨부가 든 메일이 한 통 나가고, 카드에 첨부가 보이고, 기록이 남는다
    assert len(sent) == 1
    assert [a.filename for a in mime.parse(sent[0]).attachments] == ["계획안.pdf"]
    assert "첨부: 계획안.pdf" in slack.updates[-1]["text"]
    assert len(recorded) == 1


def _fake_record(recorded: list):
    async def record(*args):
        recorded.append(args)

    return record


def _new_mail(recipients: list[str]) -> drafts.Draft:
    return drafts.Draft(
        recipients=recipients,
        subject="안내",
        body="안녕하세요",
        references_ids=[],
        thread_ts="ts-chat",
        is_reply=False,
    )


def _approve(app, slack, draft_id: str) -> None:
    async def ack():
        pass

    asyncio.run(
        app.handlers[ops_mail.APPROVE](
            ack=ack,
            body={
                "actions": [{"value": draft_id}],
                "container": {"channel_id": CHANNEL, "message_ts": "ts-card"},
                "user": {"id": "U1"},
            },
            client=slack,
        )
    )


def test_받는_사람이_여럿이면_한_명씩_따로_보내고_중간에_실패하면_멈춘다(monkeypatch):
    sent = []
    recorded = []

    def send(raw):
        to = mime.parse(raw).to_addresses
        if to == ["b@school.kr"]:
            raise TimeoutError
        sent.append(to)
        return {"MessageId": "s"}

    monkeypatch.setattr(ops_mail, "send_mime", send)
    monkeypatch.setattr(ops_mail, "_record", _fake_record(recorded))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d2"] = _new_mail(["a@school.kr", "b@school.kr", "c@school.kr"])
    slack = _FakeSlack()

    # When : 세 명 중 두 번째에게 보내다 타임아웃이 나면
    with pytest.raises(TimeoutError):
        _approve(app, slack, "d2")

    # Then : 첫 번째만 혼자 받은 메일로 나가고, 세 번째는 보내지 않으며,
    #        카드에 누가 받았고 누가 모르는지 남기고, 나간 것만 기록하며,
    #        상대별 스레드에는 아무것도 올리지 않는다
    assert sent == [["a@school.kr"]]
    assert "b@school.kr 는 발송 여부를 모릅니다" in slack.updates[-1]["text"]
    assert "보내지 않은 1명: c@school.kr" in slack.updates[-1]["text"]
    assert [args[1] for args in recorded] == ["a@school.kr"]
    assert slack.posts == []


def test_받는_사람이_많아도_카드는_한_장이고_전체_명단은_파일로_올린다(monkeypatch):
    _fake_db(monkeypatch, ops_mail, thread=None)
    slack = _FakeSlack()
    draft_ops_mail = ops_mail.get_draft_ops_mail_tool(slack, CHANNEL, "ts-chat")
    recipients = [f"school{i}@abc.kr" for i in range(12)]

    # When : 받는 사람 12명으로 새 메일 초안을 만들면
    answer = asyncio.run(
        draft_ops_mail.coroutine(body="안내드립니다", to=recipients, subject="안내")
    )

    # Then : 카드 한 장에 인원수와 앞쪽 10명만 보이고, 12명 전체 명단은 파일로 올라간다
    card = slack.posts[0]["blocks"][0]["text"]["text"]
    assert len(slack.posts) == 1
    assert "*12명*" in card and "외 2명" in card
    assert slack.uploads[0]["file_uploads"][0]["content"].splitlines() == recipients
    assert "12명" in answer


def test_본문이_길면_카드에는_앞부분만_싣고_전체는_파일로_올린다(monkeypatch):
    _fake_db(monkeypatch, ops_mail, thread=None)
    slack = _FakeSlack()
    draft_ops_mail = ops_mail.get_draft_ops_mail_tool(slack, CHANNEL, "ts-chat")
    body = "가" * 4000

    # When : 카드 한도(3000자)를 넘는 본문으로 초안을 만들면
    asyncio.run(draft_ops_mail.coroutine(body=body, to=["a@b.kr"], subject="안내"))

    # Then : 카드는 한도 안에 들어가고, 본문 전체는 파일로 올라간다
    card = slack.posts[0]["blocks"][0]["text"]["text"]
    assert len(card) < 3000
    assert slack.uploads[0]["file_uploads"] == [
        {"content": body, "filename": "본문.txt", "title": "본문"}
    ]


def test_진행_표시가_실패해도_발송은_멈추지_않는다(monkeypatch):
    recorded = []
    monkeypatch.setattr(ops_mail, "PROGRESS_EVERY", 1)
    monkeypatch.setattr(ops_mail, "send_mime", lambda raw: {"MessageId": "s"})
    monkeypatch.setattr(ops_mail, "_record", _fake_record(recorded))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d4"] = _new_mail(["a@school.kr", "b@school.kr", "c@school.kr"])
    slack = _FakeSlack()
    updates = slack.updates

    async def chat_update(**kwargs):
        if "보내는 중… 1/" in kwargs["text"]:
            raise TimeoutError
        updates.append(kwargs)

    slack.chat_update = chat_update

    # When : 중간 진행 표시가 실패하면
    _approve(app, slack, "d4")

    # Then : 세 명 모두에게 나가고 카드는 결과로 바뀐다
    assert [args[1] for args in recorded] == [
        "a@school.kr",
        "b@school.kr",
        "c@school.kr",
    ]
    assert "3명에게 보냈습니다" in updates[-1]["text"]


def _inbound(monkeypatch, headers: dict[str, str], *, thread: str | None):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: _raw_mail(headers))
    inserted = _fake_db(monkeypatch, inbound, thread=thread)
    asyncio.run(inbound.process_object("bucket", "ops/abc"))
    return _FakeSlack.last, inserted, inbound.store.claimed


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


def test_상대_스레드인데_받은_메일_기록이_없으면_새_메일로_넘어가지_않는다():
    # 스레드는 열렸는데 받은 메일 기록이 아직 없다(처리 중이거나 기록에 실패함)
    target = {
        "counterpart": "office@abc.kr",
        "subject": None,
        "message_id": None,
        "references_ids": None,
    }

    # When : 그 스레드에서 다른 사람과 제목을 주고 초안을 만들면
    with pytest.raises(drafts.DraftProblem) as other:
        drafts.make(
            thread_ts="ts-office", target=target, body="x", to=["x@y.kr"], subject="s"
        )
    with pytest.raises(drafts.DraftProblem) as unrecorded:
        drafts.make(thread_ts="ts-office", target=target, body="x", to=[], subject="")

    # Then : 받는 사람은 여전히 그 상대로 고정되고, 기록이 생길 때까지 답장도 막는다
    assert "바꿀 수 없습니다" in str(other.value)
    assert "아직 기록되지 않아" in str(unrecorded.value)


def test_보낸_뒤_기록이_실패하면_멈추고_카드에_남긴다(monkeypatch):
    monkeypatch.setattr(ops_mail, "send_mime", lambda raw: {"MessageId": "s"})

    async def record(*args):
        raise ConnectionError

    monkeypatch.setattr(ops_mail, "_record", record)
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d3"] = _new_mail(["a@school.kr", "b@school.kr"])
    slack = _FakeSlack()

    # When : 첫 번째에게 보낸 뒤 기록이 실패하면
    with pytest.raises(ConnectionError):
        _approve(app, slack, "d3")

    # Then : 카드가 "보내는 중" 에 멈추지 않고, 보냈지만 기록이 없다는 것과
    #        보내지 않은 사람을 남긴다
    text = slack.updates[-1]["text"]
    assert "a@school.kr 에게는 보냈지만 기록하지 못했습니다" in text
    assert "보내지 않은 1명: b@school.kr" in text


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
    assert _FakeSlack.last is None


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
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(
        inbound, "get_object", lambda bucket, key: b"Amazon SES setup notification"
    )
    _FakeSlack.last = None
    inserted = _fake_db(monkeypatch, inbound, thread=None)

    # When : SES 안내처럼 보낸 사람이 없는 원본이 오면
    asyncio.run(inbound.process_object("bucket", "ops/AMAZON_SES_SETUP_NOTIFICATION"))

    # Then : 주소 없는 스레드를 열지 않고 기록만 남겨 다시 처리하지 않는다
    assert _FakeSlack.last is None
    assert inserted[0].counterpart == ""


def test_첨부로_전달한_메일은_eml_로_꺼내고_빈_첨부는_뺀다():
    inner = EmailMessage()
    inner["From"] = "a@b.kr"
    inner["Subject"] = "원본"
    inner.set_content("원래 메일")
    outer = EmailMessage()
    outer["From"] = "c@d.kr"
    outer["Message-ID"] = "not-a-message-id"
    outer.set_content("전달합니다")
    outer.add_attachment(inner)
    outer.add_attachment(b"", maintype="application", subtype="pdf", filename="빈.pdf")

    # When : 메일을 첨부로 전달한 메일과 빈 첨부가 오면
    mail = mime.parse(outer.as_bytes())

    # Then : 전달한 메일은 원문 .eml 로 붙이고, 빈 첨부는 빼고,
    #        모양이 틀린 Message-ID 는 답장에 쓸 수 있는 값으로 바꾼다
    assert [(a.filename, a.content_type) for a in mail.attachments] == [
        ("attachment-1.eml", "message/rfc822")
    ]
    assert b"Subject:" in mail.attachments[0].data
    assert mail.message_id.startswith("<") and mail.message_id.endswith(">")


def test_첨부로_전달한_메일의_인코딩_안_된_한글_헤더는_받은_그대로_붙인다():
    inner = (
        "From: 김선생 <k@a.kr>\r\nSubject: 견적 문의\r\n"
        "Content-Type: text/plain; charset=euc-kr\r\n\r\n본문"
    ).encode("euc-kr")
    raw = (
        b"From: c@d.kr\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=XX\r\n\r\n"
        b"--XX\r\nContent-Type: text/plain\r\n\r\nfw\r\n"
        b"--XX\r\nContent-Type: message/rfc822\r\n"
        b"Content-Disposition: attachment\r\n\r\n" + inner + b"\r\n--XX--\r\n"
    )

    # When : 헤더의 한글을 인코딩하지 않은 메일을 첨부로 전달받으면
    mail = mime.parse(raw)

    # Then : .eml 의 헤더가 unknown-8bit 로 바뀌지 않고 받은 바이트 그대로 남는다
    data = mail.attachments[0].data
    assert b"unknown-8bit" not in data
    assert "Subject: 견적 문의".encode("euc-kr") in data


def test_받는_사람_한_칸에는_주소_하나만_받는다():
    def make(to, subject="안내"):
        return drafts.make(
            thread_ts="ts-chat", target=None, body="안녕하세요", to=to, subject=subject
        )

    # When / Then : 한 칸에 여럿, 주소 아닌 것, 줄바꿈 제목은 막고 이름 붙은 주소는 주소만 쓴다
    for bad in (
        ["a@b.kr, c@d.kr"],
        ["@"],
        ["a@b"],
        ["a b@c.kr"],
        ["a@b..kr"],
        ["홍길동@회사.kr"],
    ):
        with pytest.raises(drafts.DraftProblem):
            make(bad)
    with pytest.raises(drafts.DraftProblem):
        make(["a@b.kr"], subject="안내\nBcc: x@y.kr")
    assert make(["김선생 <Kim@ABC.kr>"]).recipients == ["kim@abc.kr"]


def test_SES가_받지_않았다고_답하면_안_나갔다고_카드에_남긴다(monkeypatch):
    from botocore.exceptions import ClientError

    def send(raw):
        raise ClientError({"Error": {"Code": "TooManyRequestsException"}}, "SendEmail")

    monkeypatch.setattr(ops_mail, "send_mime", send)
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d5"] = _new_mail(["a@school.kr", "b@school.kr"])
    slack = _FakeSlack()

    # When : SES 가 보내기 한도로 거절하면
    with pytest.raises(ClientError):
        _approve(app, slack, "d5")

    # Then : 발송 여부를 모른다고 하지 않고 안 나갔다고 남긴다
    text = slack.updates[-1]["text"]
    assert "a@school.kr 에게는 안 나갔습니다 (TooManyRequestsException)" in text
    assert ses.rejected_code(ValueError()) is None


def _parse_headers(headers: str, body: bytes = b"body") -> mime.ParsedMail:
    return mime.parse(headers.replace("\n", "\r\n").encode() + b"\r\n\r\n" + body)


def test_보낸_사람_머리가_특이해도_주소를_읽고_못_읽으면_실패로_올린다():
    # When / Then : 이름에 따옴표 없는 쉼표·대괄호가 있어도 꺾쇠 안 주소를 쓴다
    assert _parse_headers("From: Kim, J <a@b.com>").reply_to_address == "a@b.com"
    assert _parse_headers("From: John Doe [ACME] <A@B.com>").from_address == "a@b.com"
    # 이름 자리에 남의 주소를 넣어도 진짜 주소(마지막 꺾쇠)를 쓴다
    spoof = _parse_headers('From: "<partner@school.kr>" <me@evil.kr>')
    assert spoof.from_address == "me@evil.kr"
    # 주소를 못 읽으면 조용히 넘기지 않고 실패로 올려 포기 알림까지 가게 한다
    with pytest.raises(ValueError):
        _parse_headers("From: undisclosed-recipients")
    # From 이 아예 없는 원본(SES 안내)은 빈 주소다
    assert _parse_headers("Subject: setup").from_address == ""


def test_DB_와_답장을_깨뜨리는_머리와_본문을_미리_다듬는다():
    # When : NUL 이 든 제목, 줄바꿈이 든 제목, 모르는 문자셋, 모양이 틀린 Message-ID 가 오면
    nul = _parse_headers("From: a@b.kr\nSubject: =?utf-8?b?YQBi?=", b"x\x00y")
    newline = _parse_headers("From: a@b.kr\nSubject: =?utf-8?b?bGluZTEKbGluZTI=?=")
    charset = _parse_headers(
        "From: a@b.kr\nContent-Type: text/plain; charset=x-windows-949",
        "안녕하세요".encode("cp949"),
    )
    odd_id = _parse_headers("From: a@b.kr\nMessage-ID: <a@[b>")

    # Then : NUL 은 빼고, 제목은 한 줄로, 본문은 읽을 수 있게, 메일은 끝까지 읽는다
    assert (nul.subject, nul.body_text) == ("ab", "xy")
    assert newline.subject == "line1 line2"
    assert charset.body_text == "안녕하세요"
    assert odd_id.message_id == "<a@[b>"


def test_본문_안쪽에_있거나_메일_전체가_파일인_첨부도_꺼낸다():
    apple = (
        b"From: a@b.kr\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/alternative; boundary=A\r\n\r\n"
        b"--A\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b"--A\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/html\r\n\r\n<p>hi</p>\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=x.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n--B--\r\n--A--\r\n"
    )
    scanner = (
        b"From: a@b.kr\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=scan.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERg==\r\n"
    )

    # When : 애플 메일이 본문 중간에 넣은 첨부와, 메일 전체가 파일 하나인 메일을 읽으면
    nested = mime.parse(apple)
    single = mime.parse(scanner)

    # Then : 둘 다 첨부를 꺼내고, 본문의 다른 형식(HTML)은 첨부로 치지 않는다
    assert [a.filename for a in nested.attachments] == ["x.pdf"]
    assert nested.body_text == "hi"
    assert [a.filename for a in single.attachments] == ["scan.pdf"]


def test_회신_주소가_보낸_사람과_다르면_드러낸다():
    # When : 회신 주소에 다른 사람 주소를 적은 메일을 슬랙 글로 만들면
    text = inbound.format_inbound(
        _parse_headers("From: me@evil.kr\nReply-To: partner@school.kr"), "s3://b/k"
    )

    # Then : 답장이 어디로 가는지 경고한다
    assert "회신 주소가 보낸 사람과 다릅니다" in text
    assert "partner@school.kr" in text


def test_스레드의_지운_파일이나_외부_파일은_첨부_후보에서_뺀다():
    messages = [
        {"files": [{"id": "F1", "mode": "tombstone"}]},
        {
            "files": [
                {
                    "name": "링크.pdf",
                    "mode": "external",
                    "is_external": True,
                    "url_private": "https://docs.google.com/d/abc",
                    "url_private_download": "https://docs.google.com/d/abc",
                }
            ]
        },
        {"files": [_slack_file("계획안.pdf")]},
    ]

    # When : 지운 파일과 외부 파일이 섞인 스레드에서 고르면
    picked = drafts.pick_files(messages, ["계획안.pdf"])
    with pytest.raises(drafts.DraftProblem) as external:
        drafts.pick_files(messages, ["링크.pdf"])

    # Then : 터지지 않고 슬랙에 올라간 파일만 고른다. 외부 파일은 봇 토큰을 실어
    #        슬랙 밖으로 내려받게 되므로 후보에 없다
    assert [f["url"] for f in picked] == ["https://files.slack.com/계획안.pdf"]
    assert "링크.pdf 파일이 없습니다" in str(external.value)


def test_슬랙_밖_주소의_파일은_내려받지_않는다():
    # When / Then : 슬랙 파일 서버가 아닌 주소면 봇 토큰을 싣고 가지 않는다
    with pytest.raises(ValueError):
        asyncio.run(
            ops_mail._download(
                "xoxb-secret",
                {"name": "a.pdf", "mimetype": "", "url": "https://evil.kr/a.pdf"},
            )
        )


def test_카드는_본문도_이스케이프해_실제로_나갈_글자를_보여준다():
    draft = drafts.Draft(
        recipients=["a@b.kr"],
        subject="안내",
        body="팀모노리스 <ops@team-mono.com> & <!channel>",
        references_ids=[],
        thread_ts="ts",
        is_reply=False,
    )

    # When : 카드 글을 만들면
    summary = ops_mail._summary(draft)

    # Then : 슬랙 문법으로 읽히지 않게 바꾼다
    assert "&lt;ops@team-mono.com&gt; &amp; &lt;!channel&gt;" in summary
    assert "<!channel>" not in summary


def test_원문을_만들다_실패하면_안_나갔다고_남기고_SES_를_부르지_않는다(monkeypatch):
    called = []

    def build(draft, recipient, attachments):
        raise ValueError("bad header")

    monkeypatch.setattr(ops_mail, "_build", build)
    monkeypatch.setattr(ops_mail, "send_mime", lambda raw: called.append(raw))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d6"] = _new_mail(["a@school.kr", "b@school.kr"])
    slack = _FakeSlack()

    # When : 원문을 만들다 실패하면
    with pytest.raises(ValueError):
        _approve(app, slack, "d6")

    # Then : 발송 여부를 모른다고 하지 않고 안 나갔다고 남긴다
    assert called == []
    assert "a@school.kr 에게는 안 나갔습니다. (ValueError)" in slack.updates[-1]["text"]


def test_버튼을_못_걷으면_초안을_되돌려_다시_누를_수_있게_한다(monkeypatch):
    sent = []
    monkeypatch.setattr(ops_mail, "send_mime", lambda raw: sent.append(raw))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d7"] = _new_mail(["a@school.kr"])
    slack = _FakeSlack()

    async def chat_update(**kwargs):
        raise ConnectionError

    slack.chat_update = chat_update

    # When : [보내기] 를 눌렀는데 카드의 버튼을 걷지 못하면
    with pytest.raises(ConnectionError):
        _approve(app, slack, "d7")

    # Then : 아무것도 보내지 않고 초안을 되돌린다
    assert sent == []
    assert "d7" in ops_mail._DRAFTS
    ops_mail._DRAFTS.pop("d7")


def test_다_보낸_뒤_카드를_못_고쳐도_실패로_올리지_않는다(monkeypatch):
    recorded = []
    monkeypatch.setattr(ops_mail, "send_mime", lambda raw: {"MessageId": "s"})
    monkeypatch.setattr(ops_mail, "_record", _fake_record(recorded))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d8"] = _new_mail(["a@school.kr"])
    slack = _FakeSlack()
    updates = slack.updates

    async def chat_update(**kwargs):
        if "보냈습니다" in kwargs["text"]:
            raise TimeoutError
        updates.append(kwargs)

    slack.chat_update = chat_update

    # When : 메일은 다 나갔는데 결과 카드로 고치다 실패하면
    _approve(app, slack, "d8")

    # Then : 던지지 않고(다시 보내게 만들지 않고), 기록은 남아 있다
    assert [args[1] for args in recorded] == ["a@school.kr"]


def test_처리됐거나_만료된_카드의_버튼은_누른_사람에게만_알린다():
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    slack = _FakeSlack()

    # When : 초안이 없는 카드의 [보내기] 를 누르면
    _approve(app, slack, "gone")

    # Then : 카드는 건드리지 않고 누른 사람에게만 알린다
    assert slack.updates == []
    assert slack.ephemerals[0]["user"] == "U1"
    assert "이미 처리됐거나 만료됐습니다" in slack.ephemerals[0]["text"]


def test_SES_는_다시_보내지_않는_클라이언트로_부른다(monkeypatch):
    configs = []

    class _Session:
        def client(self, name, region_name, config):
            configs.append(config)

            class _Client:
                def send_email(self, Content):
                    return {"MessageId": "s"}

            return _Client()

    monkeypatch.setattr(ses.boto3.session, "Session", _Session)

    # When : 메일을 보내면
    ses.send_mime(b"raw")

    # Then : 타임아웃·5xx 에 같은 원문을 다시 보내지 않게 한 번만 시도한다
    assert configs[0].retries == {"total_max_attempts": 1}


def test_인코딩_안_된_한글_머리와_charset_없는_한글_본문도_읽는다():
    raw = (
        "From: 김선생 <k@a.kr>\r\nSubject: 견적 요청드립니다\r\n"
        "Content-Type: text/plain\r\n\r\n견적 부탁드립니다"
    ).encode("euc-kr")

    # When : 머리와 본문을 날 EUC-KR 로, charset 없이 보낸 메일을 읽으면
    mail = mime.parse(raw)

    # Then : 깨지지 않고, DB 에 넣을 수 있는 글자다
    assert mail.subject == "견적 요청드립니다"
    assert mail.from_header == "김선생 <k@a.kr>"
    assert mail.body_text == "견적 부탁드립니다"
    mail.subject.encode("utf-8")


def test_사진_뒤에_쓴_글도_본문에_남는다():
    raw = (
        b"From: a@b.kr\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=M\r\n\r\n"
        b"--M\r\nContent-Type: text/plain\r\n\r\nPlease see the photo:\r\n"
        b"--M\r\nContent-Type: image/jpeg\r\nContent-Disposition: inline;"
        b" filename=IMG_1.jpg\r\nContent-Transfer-Encoding: base64\r\n\r\n/9j/\r\n"
        b"--M\r\nContent-Type: text/plain\r\n\r\nThe crack is at the left edge.\r\n"
        b"--M--\r\n"
    )

    # When : 애플 메일처럼 "글, 사진, 글" 로 온 메일을 읽으면
    mail = mime.parse(raw)

    # Then : 사진 뒤의 글도 본문에 있고, 사진은 첨부로 꺼낸다
    assert "Please see the photo:" in mail.body_text
    assert "The crack is at the left edge." in mail.body_text
    assert [a.filename for a in mail.attachments] == ["IMG_1.jpg"]


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


def test_긴_Message_ID_도_답장_머리에_그대로_싣는다():
    outlook = "<DM6PR11MB4691C2B1E6B0A9A1D2F3B4C5D6E7F8A9@DM6PR11MB4691.namprd11.prod.outlook.com>"

    # When : 아웃룩의 긴 Message-ID 에 거는 답장을 만들면
    _, raw = mime.build(
        from_header="팀모노리스 <ops@team-mono.com>",
        to_address="a@b.kr",
        subject="Re: 견적",
        body="안녕하세요",
        references_ids=[outlook],
        attachments=[],
    )

    # Then : 인코딩하지 않고 받은 그대로 실어, 받는 쪽이 대화에 붙일 수 있다
    assert f"In-Reply-To: {outlook}".encode() in raw
    assert mime.parse(raw).references_ids == [outlook]


def test_보내기를_두_번_눌러도_한_번만_보낸다(monkeypatch):
    sent = []
    recorded = []
    monkeypatch.setattr(
        ops_mail, "send_mime", lambda raw: sent.append(raw) or {"MessageId": "s"}
    )
    monkeypatch.setattr(ops_mail, "_record", _fake_record(recorded))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d9"] = _new_mail(["a@school.kr"])
    slack = _FakeSlack()

    # When : 같은 카드의 [보내기] 를 두 번 누르면
    _approve(app, slack, "d9")
    _approve(app, slack, "d9")

    # Then : 한 번만 나가고, 두 번째 누른 사람에게는 이미 처리됐다고 알린다
    assert len(sent) == 1
    assert len(slack.ephemerals) == 1


def test_받은_메일의_슬랙_문법은_글자로_올린다(monkeypatch):
    raw = (
        b"From: <!channel> <x@evil.kr>\r\nSubject: <https://evil.kr|https://bank.com>\r\n\r\n"
        b"<!here> <@U123>\r\n"
    )
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(inbound, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(inbound, "get_object", lambda bucket, key: raw)
    _fake_db(monkeypatch, inbound, thread=None)

    # When : 보낸 사람·제목·본문에 멘션과 위장 링크를 넣은 메일이 오면
    asyncio.run(inbound.process_object("bucket", "ops/abc"))

    # Then : 스레드 첫 글과 메일 글 어디에도 슬랙 문법이 살아 있지 않다
    texts = " ".join(post["text"] for post in _FakeSlack.last.posts)
    for markup in ("<!channel>", "<!here>", "<@U123>", "<https://evil.kr|"):
        assert markup not in texts


def test_보낸_시각은_Date_헤더에서_읽고_없거나_깨졌으면_비운다():
    # When : Date 헤더가 정상, 시간대 없음, 깨짐, 없음인 메일을 읽으면
    normal = mime.parse(_raw_mail({"Date": "Tue, 03 Nov 2026 09:00:00 +0900"}))
    naive = mime.parse(_raw_mail({"Date": "Tue, 03 Nov 2026 09:00:00 -0000"}))
    broken = mime.parse(_raw_mail({"Date": "어제 아침"}))
    missing = mime.parse(_raw_mail())

    # Then : 시간대가 붙은 시각으로 읽고, 깨졌거나 없으면 None 이다
    assert normal.sent_at == datetime.datetime(
        2026, 11, 3, 0, 0, tzinfo=datetime.timezone.utc
    )
    assert naive.sent_at.tzinfo is not None
    assert broken.sent_at is None
    assert missing.sent_at is None
