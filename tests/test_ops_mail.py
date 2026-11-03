"""ops@ 메일 수신·발송 테스트"""

import base64
import datetime
import json
from contextlib import contextmanager
from email.message import EmailMessage

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ops_mail, ops_mail_sns
from service.ops_mail import mime, sns

TOPIC = "arn:aws:sns:ap-northeast-2:000000000000:tmn-topic-ops-mail"
CHANNEL = "C_OPS"


def _raw_mail() -> bytes:
    """본문(텍스트+HTML), 첨부 하나, 서명 로고(본문 삽입 이미지) 하나가 든 메일."""
    message = EmailMessage()
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
    )
    sent = mime.parse(raw)

    # Then : In-Reply-To 는 마지막 메일, References 는 전부, 제목에 Re: 가 겹치지 않는다
    assert b"In-Reply-To: <m2@abc.kr>" in raw
    assert sent.references_ids == ["<m1@abc.kr>", "<m2@abc.kr>"]
    assert sent.subject == "Re: 견적 문의"
    assert message_id.endswith("@team-mono.com>")


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
    # When / Then : sns.<region>.amazonaws.com 의 https .pem 만 받는다
    assert sns.is_aws_cert_url("https://sns.ap-northeast-2.amazonaws.com/a.pem")
    assert not sns.is_aws_cert_url(
        "https://sns.ap-northeast-2.amazonaws.com.evil.kr/a.pem"
    )
    assert not sns.is_aws_cert_url("http://sns.ap-northeast-2.amazonaws.com/a.pem")


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
    monkeypatch.setattr(ops_mail_sns, "process_notification", handled.append)
    notification = {"notificationType": "Received", "mail": {}, "receipt": {}}

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

    # Then : 받았다고 답하고 알림을 처리한다
    assert response.json() == {"status": "accepted"}
    assert handled == [notification]


class _FakeSlack:
    def __init__(self, *args, **kwargs):
        self.posts = []
        self.uploads = []
        _FakeSlack.last = self

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"ts-{len(self.posts)}"}

    def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)


def _fake_db(monkeypatch, module, *, thread: str | None, latest: dict | None = None):
    inserted = []

    @contextmanager
    def connect():
        yield None

    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(module.store, "exists", lambda conn, message_id: False)
    monkeypatch.setattr(module.store, "find_thread", lambda conn, channel, cp: thread)
    monkeypatch.setattr(module.store, "latest_in_thread", lambda conn, c, t: latest)
    monkeypatch.setattr(module.store, "insert", lambda conn, row: inserted.append(row))
    return inserted


def _notification(virus: str = "PASS") -> dict:
    return {
        "mail": {"messageId": "ses-1"},
        "receipt": {
            "action": {"bucketName": "bucket", "objectKey": "ops/abc"},
            "spamVerdict": {"status": "PASS"},
            "virusVerdict": {"status": virus},
        },
    }


def test_같은_상대의_메일은_그_상대_스레드에_첨부와_함께_단다(monkeypatch):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(ops_mail_sns, "WebClient", _FakeSlack)
    monkeypatch.setattr(ops_mail_sns, "get_object", lambda bucket, key: _raw_mail())
    inserted = _fake_db(monkeypatch, ops_mail_sns, thread="ts-office")

    # When : 이미 스레드가 있는 상대에게서 메일이 오면
    ops_mail_sns.process_notification(_notification())

    # Then : 새 스레드를 열지 않고 그 스레드에 메일과 첨부를 달고, 같은 스레드로 남긴다
    slack = _FakeSlack.last
    assert [post["thread_ts"] for post in slack.posts] == ["ts-office"]
    assert slack.uploads[0]["thread_ts"] == "ts-office"
    assert inserted[0].thread_ts == "ts-office"
    assert inserted[0].counterpart == "office@abc.kr"


def test_처음_오는_상대는_스레드를_열고_바이러스_의심이면_첨부를_올리지_않는다(
    monkeypatch,
):
    monkeypatch.setenv("OPS_MAIL_SLACK_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(ops_mail_sns, "WebClient", _FakeSlack)
    monkeypatch.setattr(ops_mail_sns, "get_object", lambda bucket, key: _raw_mail())
    inserted = _fake_db(monkeypatch, ops_mail_sns, thread=None)

    # When : 처음 오는 상대의 메일이 바이러스 의심이면
    ops_mail_sns.process_notification(_notification(virus="FAIL"))

    # Then : 상대 주소로 스레드를 열어 메일을 달고, 첨부는 올리지 않는다
    slack = _FakeSlack.last
    assert "thread_ts" not in slack.posts[0]
    assert slack.posts[1]["thread_ts"] == "ts-1"
    assert "바이러스 의심" in slack.posts[1]["text"]
    assert slack.uploads == []
    assert inserted[0].thread_ts == "ts-1"


def test_상대_스레드_안에서는_그_상대에게_답장을_만든다(monkeypatch):
    latest = {
        "counterpart": "office@abc.kr",
        "subject": "견적 문의",
        "message_id": "<m2@abc.kr>",
        "references_ids": ["<m1@abc.kr>"],
    }
    _fake_db(monkeypatch, ops_mail, thread=None, latest=latest)

    # When : 상대 스레드에서 본문만 주거나, 다른 사람을 받는 사람으로 주면
    draft = ops_mail._prepare(CHANNEL, "ts-office", "안내드립니다", "", "")
    other = ops_mail._prepare(CHANNEL, "ts-office", "안내드립니다", "x@y.kr", "")

    # Then : 그 상대에게 마지막 메일에 거는 답장이 되고, 받는 사람은 바꿀 수 없다
    assert draft["to"] == "office@abc.kr"
    assert draft["subject"] == "Re: 견적 문의"
    assert draft["references_ids"] == ["<m1@abc.kr>", "<m2@abc.kr>"]
    assert "바꿀 수 없습니다" in other


def test_스레드_밖에서_새_메일은_받는_사람과_제목이_있어야_한다(monkeypatch):
    _fake_db(monkeypatch, ops_mail, thread=None, latest=None)

    # When : 메일 스레드가 아닌 곳에서 제목 없이 부르면
    missing = ops_mail._prepare(CHANNEL, "ts-chat", "안녕하세요", "a@b.kr", "")
    draft = ops_mail._prepare(CHANNEL, "ts-chat", "안녕하세요", "A@B.kr", "안내")

    # Then : 제목을 달라고 하고, 다 있으면 references 없는 새 메일이 된다
    assert "제목" in missing
    assert draft == {
        "to": "a@b.kr",
        "subject": "안내",
        "body": "안녕하세요",
        "references_ids": [],
    }
