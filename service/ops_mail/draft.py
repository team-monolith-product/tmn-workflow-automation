"""
보낼 메일 초안을 만들고 검사합니다. 슬랙 카드와 버튼은 app/ops_mail.py 가 맡습니다.
"""

import unicodedata
from dataclasses import dataclass, field
from email.utils import getaddresses

from service.ops_mail import mime
from service.ops_mail.store import ReplyTarget

# 첨부 크기 합의 상한. SES v2 는 한 통에 40MB 까지인데 base64 인코딩 뒤
# 크기로 잰다. 인코딩하면 4/3 배가 되니 원본으로는 30MB 남짓이고, 본문과
# 헤더 자리를 남겨 28MB 로 둔다.
MAX_ATTACHMENT_BYTES = 28 * 1024 * 1024

# 한 번에 보내는 받는 사람 수의 상한. 명단을 잘못 넣어 수천 명에게 나가는 것을 막는다.
MAX_RECIPIENTS = 500


class DraftProblem(ValueError):
    """초안을 만들 수 없는 이유. 에이전트에게 고칠 것으로 돌려줍니다."""


@dataclass(frozen=True)
class SlackFile:
    """스레드에 올라온 슬랙 파일 하나. [보내기] 를 누를 때 내려받아 붙입니다."""

    name: str
    mimetype: str
    size: int
    url: str


@dataclass(frozen=True)
class Draft:
    """[보내기] 를 기다리는 메일.

    Attributes:
        recipients: 받는 사람. 한 명씩 따로 보냅니다
        subject: 제목
        body: 본문
        references_ids: 답장이 거는 메일들의 Message-ID. 새 메일이면 비어 있습니다
        thread_ts: 초안을 부른 스레드. 카드가 여기 올라가고 [수정] 도 여기로 돌아옵니다
        attachments: 붙일 슬랙 파일
    """

    recipients: list[str]
    subject: str
    body: str
    references_ids: list[str]
    thread_ts: str
    attachments: list[SlackFile] = field(default_factory=list)

    @property
    def is_reply(self) -> bool:
        """상대방 스레드에서 부른 답장이면 True. 답장만 받은 메일에 겁니다."""
        return bool(self.references_ids)


def make(
    *,
    thread_ts: str,
    target: ReplyTarget | None,
    body: str,
    to: list[str],
    subject: str,
) -> Draft:
    """초안을 만듭니다. 상대방 스레드 안이면 답장, 밖이면 새 메일입니다.

    Args:
        thread_ts: 도구를 부른 스레드
        target: store.reply_target. 상대방 스레드가 아니면 None
        body: 본문
        to: 받는 사람 주소들. 답장이면 비워도 됩니다
        subject: 제목. 답장이면 비워도 됩니다

    Returns:
        Draft: 첨부 없는 초안

    Raises:
        DraftProblem: 보낼 수 없는 초안
    """
    if "\r" in subject or "\n" in subject:
        raise DraftProblem("제목에 줄바꿈을 넣을 수 없습니다.")
    recipients = list(dict.fromkeys(_address(entry) for entry in to if entry.strip()))

    # 상대방 스레드에서는 늘 그 상대에게만 보낸다. 메일 본문으로 받는 사람을
    # 바꾸게 하지 못하도록, 답장할 메일을 못 찾아도 새 메일로 넘어가지 않는다.
    if target:
        counterpart = target.counterpart
        if recipients and recipients != [counterpart]:
            raise DraftProblem(
                f"이 스레드는 {counterpart} 와의 메일 스레드라 받는 사람을"
                " 바꿀 수 없습니다. 다른 사람에게는 채널에서 새 메일로 보내주세요."
            )
        if target.mail is None:
            raise DraftProblem(
                f"{counterpart} 에게서 받은 메일이 아직 기록되지 않아 답장을 만들 수"
                " 없습니다. 잠시 뒤 다시 해 주세요."
            )
        return Draft(
            recipients=[counterpart],
            subject=mime.reply_subject(target.mail.subject),
            body=body,
            # 마지막 것이 In-Reply-To 가 된다.
            references_ids=[*target.mail.references_ids, target.mail.message_id],
            thread_ts=thread_ts,
        )

    if not recipients or not subject:
        raise DraftProblem("새 메일은 받는 사람(to)과 제목(subject)이 필요합니다.")
    if len(recipients) > MAX_RECIPIENTS:
        raise DraftProblem(
            f"받는 사람이 {len(recipients)}명입니다. 한 번에 {MAX_RECIPIENTS}명까지라"
            " 나눠서 올려주세요."
        )
    return Draft(
        recipients=recipients,
        subject=subject,
        body=body,
        references_ids=[],
        thread_ts=thread_ts,
    )


def _address(entry: str) -> str:
    """받는 사람 항목 하나에서 주소 하나를 꺼냅니다.

    한 항목에 주소를 여럿 넣으면("a@b.kr, c@d.kr") 한 통의 To 에 모두 실려 서로의
    주소가 보이고, 그 행의 상대방이 주소 하나가 아니게 된다. 그래서 정확히 하나만 받습니다.

    Raises:
        DraftProblem: 주소가 하나가 아니거나 주소 모양이 아님
    """
    parsed = [address for _, address in getaddresses([entry]) if address]
    if len(parsed) != 1 or "\n" in entry or "\r" in entry:
        raise DraftProblem(f"받는 사람 하나에 주소 하나만 넣어주세요: {entry}")
    # SES 가 받지 않는 주소를 여기서 막는다. 보낼 때 걸리면 그 뒤 사람들이 다 밀린다.
    address = parsed[0]
    local, _, domain = address.rpartition("@")
    if (
        not local
        or "." not in domain
        or ".." in domain
        or not address.isascii()
        or any(char.isspace() for char in address)
    ):
        raise DraftProblem(f"메일 주소가 아닙니다: {entry}")
    return address.lower()


def megabytes(size: int) -> str:
    return f"{size / 1024 / 1024:.1f}MB"


def pick_files(messages: list[dict], names: list[str]) -> list[SlackFile]:
    """스레드에 올라온 파일 중 붙일 것을 이름으로 고릅니다.

    에이전트가 파일을 지어내지 못하게 스레드에 실제로 있는 파일만 받습니다.
    같은 이름이 여러 번 올라왔으면 나중에 올린 것을 씁니다.

    Args:
        messages: conversations_replies 의 메시지들
        names: 붙일 파일 이름

    Returns:
        list[SlackFile]: 고른 파일

    Raises:
        DraftProblem: 없는 이름이 있거나 너무 큼
    """
    # 맥에서 올린 한글 파일 이름은 자모가 풀린 NFD 로 온다("ㅋㅏㅋㅏ"). 에이전트는 이름을
    # 완성형(NFC)으로 쓰므로 그대로 견주면 늘 "없는 파일" 이 된다. 받는 쪽(윈도·아웃룩)
    # 에서도 첨부 이름이 자모로 풀려 보이므로, 이름은 NFC 로 맞춰 견주고 붙인다.
    files = {}
    for message in messages:
        for file in message.get("files", []):
            # 슬랙에 올라간 파일만 받는다. 지운 파일(tombstone)·가려진 파일은 이름이나
            # 주소가 없고, 외부 파일(구글 드라이브 링크 등)은 주소가 슬랙 밖이라
            # 봇 토큰을 실어 내려받으면 토큰이 새고 로그인 화면이 첨부로 나간다.
            url = file.get("url_private_download")
            if file.get("name") and url and not file.get("is_external"):
                name = unicodedata.normalize("NFC", file["name"])
                files[name] = {**file, "name": name, "url": url}

    names = [unicodedata.normalize("NFC", name) for name in names]
    missing = [name for name in names if name not in files]
    if missing:
        raise DraftProblem(
            f"스레드에 {', '.join(missing)} 파일이 없습니다. "
            f"스레드에 올라온 파일: {', '.join(files) or '없음'}"
        )

    picked = [files[name] for name in dict.fromkeys(names)]
    total = sum(file.get("size", 0) for file in picked)
    if total > MAX_ATTACHMENT_BYTES:
        raise DraftProblem(
            f"첨부가 합쳐서 {megabytes(total)} 라 보낼 수 없습니다. "
            f"{megabytes(MAX_ATTACHMENT_BYTES)} 까지입니다."
        )
    return [
        SlackFile(
            name=file["name"],
            mimetype=file.get("mimetype", ""),
            size=file.get("size", 0),
            url=file["url"],
        )
        for file in picked
    ]
