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
    picked = ops_mail._pick_files(messages, ["계획안.pdf"])
    missing = ops_mail._pick_files(messages, ["견적서.pdf"])

    # Then : 있는 것은 나중에 올린 것을 쓰고, 없으면 스레드의 파일 목록을 알려준다
    assert [(f["name"], f["size"]) for f in picked] == [("계획안.pdf", 2)]
    assert "견적서.pdf 파일이 없습니다" in missing
    assert "계획안.pdf" in missing


def test_첨부가_너무_크면_초안을_막는다():
    messages = [
        {"files": [_slack_file("큰파일.zip", ops_mail.MAX_ATTACHMENT_BYTES + 1)]}
    ]

    # When : 상한을 넘는 파일을 고르면
    result = ops_mail._pick_files(messages, ["큰파일.zip"])

    # Then : 보낼 수 없다고 한다
    assert "보낼 수 없습니다" in result


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
    token = "xoxb-test"

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.uploads = []
        self.updates = []
        _FakeSlack.last = self

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ts": f"ts-{len(self.posts)}"}

    async def files_upload_v2(self, **kwargs):
        self.uploads.append(kwargs)

    async def chat_update(self, **kwargs):
        self.updates.append(kwargs)


def _fake_db(
    monkeypatch,
    module,
    *,
    thread: str | None,
    latest: dict | None = None,
    unthreaded: list[dict] | None = None,
):
    inserted = []
    attached = []

    @contextmanager
    def connect():
        yield None

    # store 함수는 모두 app.ops_mail.run_store 로 불린다.
    monkeypatch.setattr(ops_mail, "connect", connect)
    monkeypatch.setattr(module.store, "exists", lambda conn, message_id: False)
    monkeypatch.setattr(module.store, "find_thread", lambda conn, channel, cp: thread)
    monkeypatch.setattr(module.store, "latest_in_thread", lambda conn, c, t: latest)
    monkeypatch.setattr(module.store, "insert", lambda conn, row: inserted.append(row))
    monkeypatch.setattr(
        module.store, "unthreaded", lambda conn, c, cp: list(unthreaded or [])
    )
    monkeypatch.setattr(
        module.store,
        "attach_thread",
        lambda conn, c, cp, ts: attached.append((cp, ts)),
    )
    module.store.attached = attached
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
    monkeypatch.setattr(ops_mail_sns, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(ops_mail_sns, "get_object", lambda bucket, key: _raw_mail())
    inserted = _fake_db(monkeypatch, ops_mail_sns, thread="ts-office")

    # When : 이미 스레드가 있는 상대에게서 메일이 오면
    asyncio.run(ops_mail_sns.process_notification(_notification()))

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
    monkeypatch.setattr(ops_mail_sns, "AsyncWebClient", _FakeSlack)
    monkeypatch.setattr(ops_mail_sns, "get_object", lambda bucket, key: _raw_mail())
    sent_before = {
        "subject": "2학기 안내",
        "sent_by": "U1",
        "created_at": datetime.datetime(2026, 11, 3, 10, 0),
    }
    inserted = _fake_db(
        monkeypatch, ops_mail_sns, thread=None, unthreaded=[sent_before]
    )

    # When : 새 메일로 먼저 보냈던 상대가 처음 회신했는데 바이러스 의심이면
    asyncio.run(ops_mail_sns.process_notification(_notification(virus="FAIL")))

    # Then : 먼저 보낸 메일을 보여주며 스레드를 열어 그 메일들을 묶고,
    #        회신을 달되 첨부는 올리지 않는다
    slack = _FakeSlack.last
    assert "thread_ts" not in slack.posts[0]
    assert "먼저 보낸 메일: 2학기 안내" in slack.posts[0]["text"]
    assert ops_mail_sns.store.attached == [("office@abc.kr", "ts-1")]
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
    draft = ops_mail._prepare(CHANNEL, "ts-office", "안내드립니다", [], "")
    other = ops_mail._prepare(CHANNEL, "ts-office", "안내드립니다", ["x@y.kr"], "")

    # Then : 그 상대에게 마지막 메일에 거는 답장이 되고, 받는 사람은 바꿀 수 없다
    assert draft["recipients"] == ["office@abc.kr"]
    assert draft["subject"] == "Re: 견적 문의"
    assert draft["references_ids"] == ["<m1@abc.kr>", "<m2@abc.kr>"]
    assert "바꿀 수 없습니다" in other


def test_스레드_밖에서_새_메일은_받는_사람과_제목이_있어야_한다(monkeypatch):
    _fake_db(monkeypatch, ops_mail, thread=None, latest=None)

    # When : 메일 스레드가 아닌 곳에서 제목 없이 부르거나, 같은 주소를 겹쳐 주면
    missing = ops_mail._prepare(CHANNEL, "ts-chat", "안녕하세요", ["a@b.kr"], "")
    draft = ops_mail._prepare(
        CHANNEL, "ts-chat", "안녕하세요", ["A@B.kr", "c@d.kr", "a@b.kr"], "안내"
    )

    # Then : 제목을 달라고 하고, 다 있으면 받는 사람을 소문자로 한 번씩만 둔 새 메일이 된다
    assert "제목" in missing
    assert draft == {
        "recipients": ["a@b.kr", "c@d.kr"],
        "subject": "안내",
        "body": "안녕하세요",
        "references_ids": [],
    }


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
        ops_mail, "send_raw_email", lambda raw: sent.append(raw) or {"MessageId": "s"}
    )
    monkeypatch.setattr(ops_mail, "_record", lambda *args: recorded.append(args))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d1"] = {
        "recipients": ["office@abc.kr"],
        "subject": "Re: 견적 문의",
        "body": "자료 보내드립니다",
        "references_ids": ["<m2@abc.kr>"],
        "attachments": [
            {"name": "계획안.pdf", "mimetype": "application/pdf", "size": 4, "url": "u"}
        ],
        "thread_ts": "ts-office",
    }
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

    monkeypatch.setattr(ops_mail, "send_raw_email", send)
    monkeypatch.setattr(ops_mail, "_record", lambda *args: recorded.append(args))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d2"] = {
        "recipients": ["a@school.kr", "b@school.kr", "c@school.kr"],
        "subject": "안내",
        "body": "안녕하세요",
        "references_ids": [],
        "attachments": [],
        "thread_ts": "ts-chat",
    }
    slack = _FakeSlack()

    # When : 세 명 중 두 번째에게 보내다 타임아웃이 나면
    with pytest.raises(TimeoutError):
        _approve(app, slack, "d2")

    # Then : 첫 번째만 혼자 받은 메일로 나가고, 세 번째는 보내지 않으며,
    #        카드에 누가 받았고 누가 모르는지 남기고, 나간 것만 스레드 없이 기록하며,
    #        상대별 스레드에는 아무것도 올리지 않는다
    assert sent == [["a@school.kr"]]
    assert "b@school.kr 는 발송 여부를 모릅니다" in slack.updates[-1]["text"]
    assert "보내지 않은 1명: c@school.kr" in slack.updates[-1]["text"]
    assert [(args[1], args[3]) for args in recorded] == [("a@school.kr", None)]
    assert slack.posts == []


def test_받는_사람이_많아도_카드는_한_장이고_전체_명단은_파일로_올린다(monkeypatch):
    _fake_db(monkeypatch, ops_mail, thread=None, latest=None)
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
    assert slack.uploads[0]["content"].splitlines() == recipients
    assert "12명" in answer
