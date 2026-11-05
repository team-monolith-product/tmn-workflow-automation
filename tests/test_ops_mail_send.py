"""ops@ 메일 초안과 발송 카드 테스트"""

import asyncio
import unicodedata

import pytest

from api import ses
from app import ops_mail
from ops_mail_fakes import CHANNEL, FakeSlack, fake_db
from service.ops_mail import draft as drafts
from service.ops_mail import mime, store


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
    assert [(f.name, f.size) for f in picked] == [("계획안.pdf", 2)]
    assert "견적서.pdf 파일이 없습니다" in str(missing.value)
    assert "계획안.pdf" in str(missing.value)


def test_첨부가_너무_크면_초안을_막는다():
    messages = [{"files": [_slack_file("큰파일.zip", drafts.MAX_ATTACHMENT_BYTES + 1)]}]

    # When : 상한을 넘는 파일을 고르면
    with pytest.raises(drafts.DraftProblem) as result:
        drafts.pick_files(messages, ["큰파일.zip"])

    # Then : 보낼 수 없다고 한다
    assert "보낼 수 없습니다" in str(result.value)


def test_상대_스레드_안에서는_그_상대에게_답장을_만든다():
    target = store.ReplyTarget(
        counterpart="office@abc.kr",
        mail=store.RepliedMail(
            subject="견적 문의",
            message_id="<m2@abc.kr>",
            references_ids=["<m1@abc.kr>"],
        ),
    )

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
        return mime.Attachment(file.name, file.mimetype, b"%PDF")

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
        attachments=[
            drafts.SlackFile(
                name="계획안.pdf", mimetype="application/pdf", size=4, url="u"
            )
        ],
    )
    slack = FakeSlack()

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
    slack = FakeSlack()

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


def test_많은_사람에게_보내다_멈추면_보내지_않은_사람_명단을_파일로_올린다(monkeypatch):
    recipients = [f"u{n}@school.kr" for n in range(15)]

    def send(raw):
        if mime.parse(raw).to_addresses == ["u2@school.kr"]:
            raise TimeoutError
        return {"MessageId": "s"}

    monkeypatch.setattr(ops_mail, "send_mime", send)
    monkeypatch.setattr(ops_mail, "_record", _fake_record([]))
    app = _FakeApp()
    ops_mail.register_ops_mail_handlers(app)
    ops_mail._DRAFTS["d3"] = _new_mail(recipients)
    slack = FakeSlack()

    # When : 열다섯 명 중 세 번째에서 멈추면
    with pytest.raises(TimeoutError):
        _approve(app, slack, "d3")

    # Then : 카드에 못 싣는 남은 열두 명 전체를 초안 스레드에 파일로 올린다
    (upload,) = slack.uploads
    assert upload["thread_ts"] == "ts-chat"
    assert upload["content"].split("\n") == recipients[3:]


def test_받는_사람이_많아도_카드는_한_장이고_전체_명단은_파일로_올린다(monkeypatch):
    fake_db(monkeypatch, ops_mail, thread=None)
    slack = FakeSlack()
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
    fake_db(monkeypatch, ops_mail, thread=None)
    slack = FakeSlack()
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
    slack = FakeSlack()
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


def test_상대_스레드인데_받은_메일_기록이_없으면_새_메일로_넘어가지_않는다():
    # 스레드는 열렸는데 받은 메일 기록이 아직 없다(처리 중이거나 기록에 실패함)
    target = store.ReplyTarget(counterpart="office@abc.kr", mail=None)

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
    slack = FakeSlack()

    # When : 첫 번째에게 보낸 뒤 기록이 실패하면
    with pytest.raises(ConnectionError):
        _approve(app, slack, "d3")

    # Then : 카드가 "보내는 중" 에 멈추지 않고, 보냈지만 기록이 없다는 것과
    #        보내지 않은 사람을 남긴다
    text = slack.updates[-1]["text"]
    assert "a@school.kr 에게는 보냈지만 기록하지 못했습니다" in text
    assert "보내지 않은 1명: b@school.kr" in text


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
    slack = FakeSlack()

    # When : SES 가 보내기 한도로 거절하면
    with pytest.raises(ClientError):
        _approve(app, slack, "d5")

    # Then : 발송 여부를 모른다고 하지 않고 안 나갔다고 남긴다
    text = slack.updates[-1]["text"]
    assert "a@school.kr 에게는 안 나갔습니다 (TooManyRequestsException)" in text
    assert ses.rejected_code(ValueError()) is None


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
    assert [f.url for f in picked] == ["https://files.slack.com/계획안.pdf"]
    assert "링크.pdf 파일이 없습니다" in str(external.value)


def test_슬랙_밖_주소의_파일은_내려받지_않는다():
    # When / Then : 슬랙 파일 서버가 아닌 주소면 봇 토큰을 싣고 가지 않는다
    with pytest.raises(ValueError):
        asyncio.run(
            ops_mail._download(
                "xoxb-secret",
                drafts.SlackFile(
                    name="a.pdf", mimetype="", size=0, url="https://evil.kr/a.pdf"
                ),
            )
        )


def test_카드는_본문도_이스케이프해_실제로_나갈_글자를_보여준다():
    draft = drafts.Draft(
        recipients=["a@b.kr"],
        subject="안내",
        body="팀모노리스 <ops@team-mono.com> & <!channel>",
        references_ids=[],
        thread_ts="ts",
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
    slack = FakeSlack()

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
    slack = FakeSlack()

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
    slack = FakeSlack()
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
    slack = FakeSlack()

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
    ses._client.cache_clear()

    # When : 메일을 두 번 보내면
    ses.send_mime(b"raw")
    ses.send_mime(b"raw")
    ses._client.cache_clear()

    # Then : 타임아웃·5xx 에 같은 원문을 다시 보내지 않게 한 번만 시도하고,
    #        클라이언트는 한 번만 만든다
    assert len(configs) == 1
    assert configs[0].retries == {"total_max_attempts": 1}


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
    slack = FakeSlack()

    # When : 같은 카드의 [보내기] 를 두 번 누르면
    _approve(app, slack, "d9")
    _approve(app, slack, "d9")

    # Then : 한 번만 나가고, 두 번째 누른 사람에게는 이미 처리됐다고 알린다
    assert len(sent) == 1
    assert len(slack.ephemerals) == 1


def test_맥에서_올린_한글_파일_이름도_완성형_이름으로_고른다():
    """맥은 한글 파일 이름을 자모가 풀린 NFD 로 올리고, 에이전트는 NFC 로 쓴다"""
    nfd = unicodedata.normalize("NFD", "카카오비즈메시지_가이드.pdf")
    messages = [{"files": [_slack_file(nfd)]}]

    # When : 에이전트가 완성형으로 쓴 이름으로 고르면
    picked = drafts.pick_files(messages, ["카카오비즈메시지_가이드.pdf"])

    # Then : 그 파일을 고르고, 메일에도 완성형 이름으로 붙인다
    assert [f.name for f in picked] == ["카카오비즈메시지_가이드.pdf"]


def test_수정할_때_앞_초안을_에이전트가_읽을_글로_넘긴다():
    """카드가 수정 요청 문구로 바뀌어, 넘기지 않으면 에이전트가 앞 본문을 모른다"""
    draft = drafts.Draft(
        recipients=["chk@team-mono.com"],
        subject="회의 & 일정",
        body="안녕하세요.\n\n회의는 언제쯤 마치실 예정이신가요?",
        references_ids=[],
        thread_ts="1.0",
        attachments=[drafts.SlackFile("가이드.pdf", "application/pdf", 1, "u")],
    )

    previous = ops_mail._revise_previous(draft)

    # 슬랙 이스케이프(&amp;) 없이 그대로 넘겨야 에이전트가 그대로 옮겨 쓴다
    assert previous == (
        "받는 사람(1명): chk@team-mono.com\n"
        "제목: 회의 & 일정\n"
        "첨부: 가이드.pdf\n"
        "본문:\n안녕하세요.\n\n회의는 언제쯤 마치실 예정이신가요?"
    )


def test_카드의_첨부는_슬랙_파일로_걸고_올린_시각을_보여준다():
    """이름만 보이면 같은 이름이 여럿일 때 실제로 나갈 파일을 확인할 수 없다"""
    file = {
        **_slack_file("계약서|최종.pdf", 1024 * 1024),
        "permalink": "https://monolith.slack.com/files/U1/F1/a.pdf",
        "created": 1762318920,
    }
    messages = [{"text": "이 파일과 함께 보내", "files": [file]}]

    # When : 고른 파일로 카드 글을 만들면
    picked = drafts.pick_files(messages, ["계약서|최종.pdf"])
    draft = drafts.Draft(
        recipients=["a@b.kr"],
        subject="안내",
        body="본문",
        references_ids=[],
        thread_ts="ts",
        attachments=picked,
    )
    summary = ops_mail._summary(draft)

    # Then : 이름을 그 파일로 걸고(| 는 링크를 끊으므로 바꾼다), 올린 시각을 붙인다
    assert (
        "첨부: <https://monolith.slack.com/files/U1/F1/a.pdf|계약서¦최종.pdf>"
        " (1.0MB · <!date^1762318920^{date_short_pretty} {time}|올린 시각> 올림)"
    ) in summary


def test_링크가_없는_첨부는_이름만_보여준다():
    draft = drafts.Draft(
        recipients=["a@b.kr"],
        subject="안내",
        body="본문",
        references_ids=[],
        thread_ts="ts",
        attachments=[drafts.SlackFile("a<b>.pdf", "application/pdf", 0, "u")],
    )

    # When / Then : 링크·시각이 없으면 이스케이프한 이름과 크기만 보인다
    assert "첨부: a&lt;b&gt;.pdf (0.0MB)" in ops_mail._summary(draft)


def test_초안_카드는_이렇게_보내려고_한다고_말한다():
    blocks = ops_mail._blocks("d1", _new_mail(["a@school.kr"]))

    # Then : 머리말이 확인을 구하는 말투이고, 보내기·수정·취소 버튼이 그대로 있다
    assert blocks[0]["text"]["text"].startswith("*이렇게 보내려고 합니다* — 새 메일")
    assert [e["action_id"] for e in blocks[1]["elements"]] == [
        ops_mail.APPROVE,
        ops_mail.REVISE,
        ops_mail.CANCEL,
    ]


def test_수정할_때_받는_사람은_줄이지_않고_모두_넘긴다():
    """카드처럼 \"외 N명\" 으로 줄이면 에이전트가 보이는 10명만으로 다시 쓴다"""
    recipients = [f"t{i}@school.kr" for i in range(ops_mail.PREVIEW_RECIPIENTS + 5)]

    previous = ops_mail._revise_previous(_new_mail(recipients))

    assert f"받는 사람({len(recipients)}명): " + ", ".join(recipients) in previous
    assert "외" not in previous


def test_외부_파일과_지운_파일은_붙일_후보가_아니다():
    """대화 기록의 [첨부 파일] 과 같은 기준이라, 보인 파일은 늘 고를 수 있다"""
    from service.slack import is_slack_hosted_file

    assert is_slack_hosted_file(_slack_file("a.pdf"))
    assert not is_slack_hosted_file({**_slack_file("a.pdf"), "is_external": True})
    assert not is_slack_hosted_file({"id": "F1", "mode": "tombstone"})
    assert not is_slack_hosted_file({"name": "a.pdf"})
