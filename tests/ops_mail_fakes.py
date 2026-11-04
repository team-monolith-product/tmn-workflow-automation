"""ops@ 메일 테스트들이 함께 쓰는 가짜 객체와 원문"""

from email.message import EmailMessage

from service.ops_mail import mime

CHANNEL = "C_OPS"


def raw_mail(headers: dict[str, str] | None = None) -> bytes:
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


class FakeSlack:
    token = "xoxb-test"

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.uploads = []
        self.updates = []
        self.deletes = []
        self.ephemerals = []
        FakeSlack.last = self

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


def fake_db(
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

    def finish(row):
        inserted.append(row)

    def claim_thread(channel, counterpart, ts):
        claimed.append((counterpart, ts))
        return claimed_by or ts

    monkeypatch.setattr(module.store, "claim", lambda ses_id, key: attempt)
    monkeypatch.setattr(module.store, "finish", finish)
    monkeypatch.setattr(module.store, "find_thread", lambda channel, cp: thread)
    monkeypatch.setattr(module.store, "reply_target", lambda c, t: target)
    monkeypatch.setattr(module.store, "sent_to", lambda c, cp: list(sent_to or []))
    monkeypatch.setattr(module.store, "claim_thread", claim_thread)
    monkeypatch.setattr(module.store, "claimed", claimed, raising=False)
    return inserted


def parse_headers(headers: str, body: bytes = b"body") -> mime.ParsedMail:
    return mime.parse(headers.replace("\n", "\r\n").encode() + b"\r\n\r\n" + body)
