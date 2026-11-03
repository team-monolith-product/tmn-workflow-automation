"""
메일 원문(MIME)을 읽고 만듭니다.
"""

import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr

from bs4 import BeautifulSoup

MESSAGE_ID = re.compile(r"<[^<>\s]+>")

# 사람이 쓰지 않은 메일을 알리는 헤더. 부재중 자동응답, 메일링 리스트 등.
# RFC 3834 의 Auto-Submitted 와, 그걸 안 따르는 메일 서버들이 쓰는 관용 헤더다.
AUTO_PRECEDENCE = {"auto_reply", "bulk", "junk", "list"}


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


def _address(header: str | None) -> str:
    """'이름 <a@b.com>' 에서 소문자 주소만 꺼냅니다."""
    return parseaddr(header or "")[1].lower()


def _body_text(message: EmailMessage) -> str:
    """본문 텍스트를 꺼냅니다. 텍스트가 없고 HTML 만 있으면 태그를 걷어냅니다."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        return BeautifulSoup(content, "html.parser").get_text("\n").strip()
    return content.strip()


def _attachments(message: EmailMessage) -> list[Attachment]:
    """첨부파일을 꺼냅니다.

    본문에 박힌 이미지(서명 로고 등)는 뺍니다. 이런 이미지는 Content-ID 가
    있고 attachment 로 표시되지 않습니다.
    """
    attachments = []
    for index, part in enumerate(message.iter_attachments(), start=1):
        if part["Content-ID"] and part.get_content_disposition() != "attachment":
            continue
        attachments.append(
            Attachment(
                filename=part.get_filename() or f"attachment-{index}",
                content_type=part.get_content_type(),
                data=part.get_payload(decode=True) or b"",
            )
        )
    return attachments


def _is_auto_reply(message: EmailMessage) -> bool:
    """자동응답이나 대량 발송처럼 사람이 쓰지 않은 메일인지 봅니다."""
    auto_submitted = str(message["Auto-Submitted"] or "no").strip().lower()
    precedence = str(message["Precedence"] or "").strip().lower()
    return (
        auto_submitted != "no"
        or precedence in AUTO_PRECEDENCE
        or message["X-Autoreply"] is not None
        or message["X-Autorespond"] is not None
    )


def parse(raw: bytes) -> ParsedMail:
    """메일 원문을 읽습니다.

    Args:
        raw: S3 에 저장된 MIME 원문

    Returns:
        ParsedMail: 읽은 값
    """
    message = message_from_bytes(raw, policy=policy.default)
    from_address = _address(message["From"])
    references = MESSAGE_ID.findall(
        f"{message['References'] or ''} {message['In-Reply-To'] or ''}"
    )
    return ParsedMail(
        message_id=str(message["Message-ID"] or make_msgid()),
        from_header=str(message["From"] or ""),
        from_address=from_address,
        reply_to_address=_address(message["Reply-To"]) or from_address,
        to_addresses=[
            address.lower()
            for _, address in getaddresses(
                [str(message["To"] or ""), str(message["Cc"] or "")]
            )
            if address
        ],
        subject=str(message["Subject"] or ""),
        references_ids=list(dict.fromkeys(references)),
        body_text=_body_text(message),
        attachments=_attachments(message),
        spam=str(message["X-SES-Spam-Verdict"] or "") == "FAIL",
        virus=str(message["X-SES-Virus-Verdict"] or "") == "FAIL",
        auto_reply=_is_auto_reply(message),
    )


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
        maintype, subtype = attachment.content_type.split("/", 1)
        message.add_attachment(
            attachment.data,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return message_id, message.as_bytes()
