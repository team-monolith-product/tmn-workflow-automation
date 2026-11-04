"""ops@ 메일 원문 읽기·만들기 테스트"""

import datetime
from email.message import EmailMessage

import pytest

from ops_mail_fakes import parse_headers, raw_mail
from service.ops_mail import mime


def test_메일을_읽으면_텍스트_본문과_첨부만_꺼낸다():
    # When : 텍스트·HTML 본문, 첨부, 본문 삽입 로고가 든 메일을 읽으면
    mail = mime.parse(raw_mail())

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


def test_보낸_사람_머리가_특이해도_주소를_읽고_못_읽으면_실패로_올린다():
    # When / Then : 이름에 따옴표 없는 쉼표·대괄호가 있어도 꺾쇠 안 주소를 쓴다
    assert parse_headers("From: Kim, J <a@b.com>").reply_to_address == "a@b.com"
    assert parse_headers("From: John Doe [ACME] <A@B.com>").from_address == "a@b.com"
    # 이름 자리에 남의 주소를 넣어도 진짜 주소(마지막 꺾쇠)를 쓴다
    spoof = parse_headers('From: "<partner@school.kr>" <me@evil.kr>')
    assert spoof.from_address == "me@evil.kr"
    # 주소를 못 읽으면 조용히 넘기지 않고 실패로 올려 포기 알림까지 가게 한다
    with pytest.raises(ValueError):
        parse_headers("From: undisclosed-recipients")
    # From 이 아예 없는 원본(SES 안내)은 빈 주소다
    assert parse_headers("Subject: setup").from_address == ""


def test_DB_와_답장을_깨뜨리는_머리와_본문을_미리_다듬는다():
    # When : NUL 이 든 제목, 줄바꿈이 든 제목, 모르는 문자셋, 모양이 틀린 Message-ID 가 오면
    nul = parse_headers("From: a@b.kr\nSubject: =?utf-8?b?YQBi?=", b"x\x00y")
    newline = parse_headers("From: a@b.kr\nSubject: =?utf-8?b?bGluZTEKbGluZTI=?=")
    charset = parse_headers(
        "From: a@b.kr\nContent-Type: text/plain; charset=x-windows-949",
        "안녕하세요".encode("cp949"),
    )
    odd_id = parse_headers("From: a@b.kr\nMessage-ID: <a@[b>")

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


def test_받은_시각은_맨_위_Received_헤더에서_읽고_Date_는_쓰지_않는다():
    ses = (
        "from mail.abc.kr by inbound-smtp.ap-northeast-2.amazonaws.com with SMTP"
        " id x for ops@in.team-mono.com; Tue, 03 Nov 2026 00:00:05 +0000 (UTC)"
    )
    google = "by mx.google.com with SMTP id y; Mon, 02 Nov 2026 23:59:59 -0800"

    # When : 보낸 쪽 Date 가 18시간 틀렸고 Received 가 여러 개인 메일과,
    #        Received 가 깨졌거나 없는 메일을 읽으면
    skewed = mime.parse(
        b"From: a@b.kr\r\nDate: Tue, 03 Nov 2026 18:00:00 -0000\r\n"
        + f"Received: {ses}\r\nReceived: {google}\r\n".encode()
        + b"Subject: s\r\n\r\nbody"
    )
    broken = mime.parse(raw_mail({"Received": "by somewhere; 어제 아침"}))
    missing = mime.parse(raw_mail({"Date": "Tue, 03 Nov 2026 18:00:00 -0000"}))

    # Then : Date 가 아니라 맨 위(SES) Received 의 시각을 쓰고, 없거나 깨졌으면 None
    assert skewed.received_at == datetime.datetime(
        2026, 11, 3, 0, 0, 5, tzinfo=datetime.timezone.utc
    )
    assert broken.received_at is None
    assert missing.received_at is None
