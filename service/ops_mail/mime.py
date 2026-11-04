"""
메일 원문(MIME)을 읽고 만듭니다.
"""

import io
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.generator import BytesGenerator
from email.header import decode_header, make_header
from email.message import EmailMessage
from datetime import datetime, timezone
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

from bs4 import BeautifulSoup

MESSAGE_ID = re.compile(r"<[^<>\s]+>")
ANGLE_ADDRESS = re.compile(r"<\s*([^<>\s@]+@[^<>\s@]+)\s*>")
BARE_ADDRESS = re.compile(r"[^\s<>,;:\"'()\[\]]+@[^\s<>,;:\"'()\[\]]+")


@dataclass(frozen=True)
class Attachment:
    """첨부파일 하나."""

    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class ParsedMail:
    """받은 메일에서 슬랙과 답장에 쓰는 것만 꺼낸 값."""

    message_id: str
    from_header: str
    from_address: str
    reply_to_address: str
    to_addresses: list[str]
    subject: str
    references_ids: list[str]
    body_text: str
    attachments: list[Attachment]
    # SES 수신 규칙이 검사 결과를 X-SES-*-Verdict 헤더로 원문에 붙인다.
    spam: bool
    virus: bool
    auto_reply: bool
    # Date 헤더. 없거나 깨졌으면 None.
    sent_at: datetime | None


def _raw(message: EmailMessage, name: str) -> str:
    """헤더를 파이썬 헤더 파서를 거치지 않고 받은 그대로 꺼냅니다.

    파서는 모양이 틀린 값(Message-ID: <a@[b> 등)에서 AttributeError 로 터지고,
    'John Doe [ACME] <a@b.com>' 같은 From 에서는 주소를 버린다.
    """
    for key, value in message.raw_items():
        if key.lower() == name.lower():
            return " ".join(_unescape(str(value)).split())
    return ""


def _to_text(data: bytes) -> str:
    """문자셋을 모르는 바이트를 글자로 읽습니다. UTF-8, CP949 순으로 해 봅니다."""
    for encoding in ("utf-8", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _unescape(value: str) -> str:
    """인코딩하지 않은 8비트 헤더를 글자로 읽습니다.

    날 EUC-KR·UTF-8 로 쓴 한글 헤더를 파이썬은 서로게이트 문자로 들고 있다. 그대로
    두면 슬랙에는 깨져 올라가고, DB 에 넣다 실패해 같은 메일이 세 번 올라간다.
    """
    if not any("\udc80" <= char <= "\udcff" for char in value):
        return value
    return _to_text(value.encode("utf-8", "surrogateescape"))


def _decoded(value: str) -> str:
    """=?utf-8?b?...?= 같은 인코딩을 풀어 사람이 읽는 글자로 만듭니다."""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _text(value: str) -> str:
    """DB 에 넣을 수 없는 NUL 문자를 뺍니다. 넣다 실패하면 같은 메일이 슬랙에 세 번 올라간다."""
    return value.replace("\x00", "")


def _line(value: str) -> str:
    """한 줄로 만듭니다. 받은 제목에 줄바꿈이 있으면 그 제목으로 답장을 만들 수 없다."""
    return " ".join(_text(value).split())


def _address(value: str) -> str:
    """헤더 값에서 소문자 주소 하나를 꺼냅니다. 없으면 빈 문자열입니다.

    parseaddr 는 'Kim, J <a@b.com>' 처럼 이름에 따옴표 없는 쉼표가 있으면 빈 값을,
    'John Doe [ACME] <a@b.com>' 이면 'john doe' 를 돌려준다. 그래서 꺾쇠 안의 주소를
    직접 찾는다. 이름 자리에 '"<partner@x>" <me@y>' 처럼 남의 주소를 넣을 수
    있으니 마지막 꺾쇠를 쓴다.
    """
    angled = ANGLE_ADDRESS.findall(value)
    if angled:
        return angled[-1].lower()
    bare = BARE_ADDRESS.search(value)
    return bare.group(0).lower() if bare else ""


def _decode(part: EmailMessage) -> str:
    """텍스트 파트를 글자로 읽습니다.

    문자셋이 없거나(옛 그룹웨어가 EUC-KR 본문을 charset 없이 보낸다 -- 그러면
    get_content() 가 ASCII 로 읽어 전부 �가 된다), 파이썬이 모르는 이름이면
    (x-windows-949 -- LookupError) 바이트를 직접 읽는다.
    """
    if part.get_content_charset() is not None:
        try:
            return part.get_content()
        except LookupError:
            pass
    return _to_text(part.get_payload(decode=True) or b"")


def _body_text(message: EmailMessage) -> str:
    """본문 텍스트를 꺼냅니다. 텍스트가 없고 HTML 만 있으면 태그를 걷어냅니다.

    본문과 같은 형식의 텍스트 파트가 여럿이면 이어 붙입니다. 애플 메일은 본문
    중간에 사진을 넣으면 "글, 사진, 글" 세 파트로 보내서, 첫 파트만 읽으면 사진
    뒤에 쓴 글이 사라진다.
    """
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    parts = [
        part
        for part in _leaves(message)
        if part.get_content_type() == body.get_content_type()
        and part.get_content_disposition() != "attachment"
    ]
    content = "\n\n".join(_decode(part) for part in parts)
    if body.get_content_type() == "text/html":
        return BeautifulSoup(content, "html.parser").get_text("\n").strip()
    return content.strip()


def _as_received(message: EmailMessage) -> bytes:
    """받은 메일 일부를 받은 바이트 그대로 다시 씁니다.

    한글 헤더를 인코딩하지 않고 날 EUC-KR·UTF-8 로 보내는 메일 서버가 있다.
    as_bytes() 는 이런 헤더의 문자셋을 몰라 =?unknown-8bit?...?= 로 바꿔 써서,
    메일 프로그램에서 보낸 사람과 제목이 깨진다. compat32 는 헤더를 받은 대로 둔다.
    """
    buffer = io.BytesIO()
    BytesGenerator(
        buffer, mangle_from_=False, policy=policy.compat32.clone(linesep="\r\n")
    ).flatten(message)
    return buffer.getvalue()


def _leaves(part: EmailMessage):
    """multipart 를 펼쳐 맨 끝 파트들을 돌려줍니다. 전달한 메일(message/*) 속은 펼치지 않습니다."""
    if part.get_content_maintype() == "multipart":
        for child in part.iter_parts():
            yield from _leaves(child)
    else:
        yield part


def _attachments(message: EmailMessage) -> list[Attachment]:
    """첨부파일을 꺼냅니다.

    맨 위 multipart/mixed 만 보지 않고 모든 파트를 봅니다. 애플 메일이 본문 중간에
    넣은 첨부는 multipart/alternative 안쪽에 있고, 스캐너가 보낸 메일은 메일 전체가
    파일 하나다. 본문과, 첨부로 표시되지 않은 텍스트(본문의 다른 형식)와,
    본문에 박힌 이미지(서명 로고 등, Content-ID 가 있음)는 뺍니다.

    "첨부로 전달" 한 메일(message/rfc822)은 디코딩할 본문이 따로 없어 그 메일
    원문을 .eml 로 붙입니다. 빈 첨부는 뺍니다 -- 슬랙이 0바이트 파일을 받지 않아
    올리기가 매번 실패한다.
    """
    body = message.get_body(preferencelist=("plain", "html"))
    attachments = []
    for part in _leaves(message):
        if part is body:
            continue
        if part.get_content_disposition() != "attachment" and (
            part["Content-ID"] or part.get_content_maintype() == "text"
        ):
            continue
        if part.get_content_maintype() == "message":
            data = b"".join(_as_received(inner) for inner in part.iter_parts())
            default_name = f"attachment-{len(attachments) + 1}.eml"
        else:
            data = part.get_payload(decode=True) or b""
            default_name = f"attachment-{len(attachments) + 1}"
        if not data:
            continue
        attachments.append(
            Attachment(
                filename=part.get_filename() or default_name,
                content_type=part.get_content_type(),
                data=data,
            )
        )
    return attachments


def _is_auto_reply(message: EmailMessage) -> bool:
    """부재중 같은 자동응답인지 봅니다.

    RFC 3834 의 Auto-Submitted: auto-replied 와, 그걸 안 따르는 메일 서버들이 쓰는
    관용 헤더를 봅니다. 메일링 리스트·대량 발송(Precedence: list/bulk,
    Auto-Submitted: auto-generated)은 자동응답으로 치지 않는다 -- 치면 처음 오는
    상대의 그런 메일은 스레드를 못 열어 슬랙에 아예 안 올라간다. 구글 그룹을 거쳐
    오는 메일에도 Precedence: list 가 붙는다.
    """
    auto_submitted = _raw(message, "Auto-Submitted").lower()
    precedence = _raw(message, "Precedence").lower()
    return (
        auto_submitted.startswith("auto-replied")
        or precedence == "auto_reply"
        or bool(_raw(message, "X-Autoreply"))
        or bool(_raw(message, "X-Autorespond"))
    )


def parse(raw: bytes) -> ParsedMail:
    """메일 원문을 읽습니다.

    Args:
        raw: S3 에 저장된 MIME 원문

    Returns:
        ParsedMail: 읽은 값
    """
    message = message_from_bytes(raw, policy=policy.default)
    from_raw = _raw(message, "From")
    from_header = _line(_decoded(from_raw))
    from_address = _address(from_raw)
    # From 이 있는데 주소를 못 읽으면 조용히 넘기지 않는다. 실패로 처리해야 횟수를
    # 다 쓴 뒤 채널에 알린다. From 이 아예 없는 원본(SES 안내 등)은 그대로 둔다.
    if from_raw and not from_address:
        raise ValueError(f"보낸 사람 주소를 읽지 못했습니다: {from_header[:200]}")
    references = MESSAGE_ID.findall(
        f"{_raw(message, 'References')} {_raw(message, 'In-Reply-To')}"
    )
    # Message-ID 는 답장의 In-Reply-To 로 다시 나간다. 모양이 틀리면 헤더를 만들다
    # 실패하므로 <...> 하나만 받는다.
    message_ids = MESSAGE_ID.findall(_raw(message, "Message-ID"))
    return ParsedMail(
        message_id=message_ids[0] if message_ids else make_msgid(),
        from_header=from_header,
        from_address=from_address,
        reply_to_address=_address(_raw(message, "Reply-To")) or from_address,
        to_addresses=[
            address.lower()
            for _, address in getaddresses(
                [_text(_raw(message, "To")), _text(_raw(message, "Cc"))]
            )
            if address
        ],
        subject=_line(_decoded(_raw(message, "Subject"))),
        references_ids=list(dict.fromkeys(references)),
        body_text=_text(_body_text(message)),
        attachments=_attachments(message),
        spam=str(message["X-SES-Spam-Verdict"] or "") == "FAIL",
        virus=str(message["X-SES-Virus-Verdict"] or "") == "FAIL",
        auto_reply=_is_auto_reply(message),
        sent_at=_sent_at(_raw(message, "Date")),
    )


def _sent_at(header: str) -> datetime | None:
    """Date 헤더를 시각으로 읽습니다. 시간대가 없으면 UTC 로 봅니다."""
    try:
        sent_at = parsedate_to_datetime(header)
    except (TypeError, ValueError, IndexError):
        return None
    if sent_at.tzinfo is None:
        return sent_at.replace(tzinfo=timezone.utc)
    return sent_at


def reply_subject(subject: str) -> str:
    """답장 제목. 이미 Re: 로 시작하면 그대로 둡니다."""
    if re.match(r"^\s*re\s*:", subject, re.IGNORECASE):
        return subject
    return f"Re: {subject}"


def build(
    *,
    from_header: str,
    to_address: str,
    subject: str,
    body: str,
    references_ids: list[str],
    attachments: list[Attachment],
) -> tuple[str, bytes]:
    """보낼 메일 원문을 만듭니다.

    Args:
        from_header: 보내는 사람. 예: "팀모노리스 <ops@team-mono.com>"
        to_address: 받는 사람 주소
        subject: 제목
        body: 본문 텍스트
        references_ids: 이어지는 메일들의 Message-ID. 마지막 것이 답장 대상입니다.
            새 메일이면 비웁니다.
        attachments: 붙일 파일. 없으면 비웁니다.

    Returns:
        tuple[str, bytes]: (Message-ID, MIME 원문)
    """
    message = EmailMessage()
    message_id = make_msgid(domain=_address(from_header).split("@")[-1])
    message["Message-ID"] = message_id
    message["Date"] = formatdate(localtime=True)
    message["From"] = from_header
    message["To"] = to_address
    message["Subject"] = subject
    if references_ids:
        message["In-Reply-To"] = references_ids[-1]
        message["References"] = " ".join(references_ids)
    message.set_content(body)
    for attachment in attachments:
        # 슬랙 파일의 mimetype 이 비어 오기도 한다.
        maintype, _, subtype = attachment.content_type.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    # 머리 한 줄을 998자까지 둔다. 기본(78자)이면 아웃룩·SES 의 긴 Message-ID 를
    # 접지 못해 In-Reply-To·References 를 =?utf-8?q?...?= 로 바꿔 써서, 받는 쪽이
    # 답장을 원래 대화에 붙이지 못한다. 본문 인코딩은 set_content 가 이미 정했다.
    return message_id, message.as_bytes(
        policy=message.policy.clone(max_line_length=998)
    )
