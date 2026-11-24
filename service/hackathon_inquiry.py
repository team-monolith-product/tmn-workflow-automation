"""
해커톤 문의 접수 글을 읽고 분류·정리하는 순수 로직

팀모노리스 로봇이 #t_제품_해커톤_문의 에 올리는 「해커톤 문의 새 제출」 글을 파싱해
노션 리드 속성, 견적 초안, 스레드 정리 글을 만듭니다. 슬랙·노션 호출은
app/hackathon_inquiry.py 가 하고, 여기는 입력 문자열만 다룹니다.

폼 글은 이런 모양입니다.

    해커톤 문의 새 제출
    *제출 시각*
    2026-11-10 11:44
    *기관·학교명*
    사단법인 ...
    ...
    *문의 내용*
    ㅇ 장소 확정 ... (전체 내용은 댓글에서 확인)
    어드민 링크: <https://admin.codle.io/form_submissions/137/show>

긴 항목은 원글에서 잘리고, 같은 봇이 스레드 댓글로 "문의 내용\\n<전문>" 을 이어 답니다.
"""

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

CHANNEL_ID = "C0B0BR3CY4T"  # #t_제품_해커톤_문의
SLACK_WORKSPACE_DOMAIN = "monolith-keb2010.slack.com"

OWNER_USER_ID = "U052HDXN3EG"  # 이주현
BACKUP_USER_ID = "U074S4Y9PUP"  # 김세윤 (이주현 휴가 중)
OWNER_NOTION_ID = "7cbfca8c-61e8-409e-b486-5a1b8160ea27"  # 이주현
USER_NAMES = {OWNER_USER_ID: "이주현", BACKUP_USER_ID: "김세윤"}

LEAD_DATA_SOURCE_ID = "f36426c5-8176-4b61-805f-539f7cff033b"  # 🗂 리드 파이프라인
CONSULT_TEMPLATE_ID = "3ea1cc82-0da6-805e-baa9-d7234ee46dc6"  # 상담 페이지

FORM_TITLES = ("해커톤 문의 새 제출", "해커톤 신청 새 제출", "해커톤 상담 신청 새 제출")

# 봇이 단 정리 글의 첫 줄. 같은 봇 계정이 폼 글도 올리므로 이 표지로 우리 글을 가린다.
SUMMARY_MARKER = "*[해커톤 문의]*"

BUSINESS_START = time(10, 0)
BUSINESS_END = time(19, 0)

# 폼마다 같은 항목의 이름이 조금씩 다르다. 실측한 이름을 모두 둔다.
ORG_LABELS = ("기관·학교명", "기관·회사명", "대학 · 기관명", "대학·기관명", "기관명")
CONTACT_LABELS = ("담당 선생님", "담당자명", "담당자")
PHONE_LABELS = ("연락처",)
EMAIL_LABELS = ("이메일",)
TARGET_LABELS = ("대상",)
SCALE_LABELS = ("규모", "예상 규모")
SCHEDULE_LABELS = ("희망 시기",)
BODY_LABELS = ("문의 내용", "기타 문의")
SOURCE_LABELS = ("유입 경로",)
SUBMITTED_LABELS = ("제출 시각",)

TRUNCATED_RE = re.compile(r"…\s*\((?:너무 길어 )?[^()]*확인[^()]*\)\s*$")
# 블록 원문은 맨 주소, 슬랙이 만든 text 는 <주소> 꼴이다.
ADMIN_LINK_RE = re.compile(r"^어드민 링크:\s*<?(https?://[^>|\s]+)(?:\|[^>]*)?>?\s*$")
LABEL_RE = re.compile(r"^\*([^*]+)\*$")
SLACK_LINK_RE = re.compile(
    r"<(?:tel:|mailto:)?[^>|]*\|([^>]*)>|<(?:tel:|mailto:)([^>]*)>"
)
# 3000자를 넘는 항목은 "문의 내용 (1/2)" 처럼 나눠 단다
CONTINUATION_LABEL_RE = re.compile(r"^(.+?)(?:\s*\(\d+/\d+\))?$")

VACATION_KEYWORDS = ("휴가",)
VACATION_EMOJIS = (":palm_tree:",)

NOTION_LINK_RE = re.compile(r"^노션: <([^>|]+)\|", re.MULTILINE)
ORG_LINE_RE = re.compile(r"^기관: (.+?)(?: \([^()]*\))?$", re.MULTILINE)

# 영어 test 는 낱말로만 본다. "AI Contest 조직위원회" 같은 진짜 기관명이 걸리지 않게.
TEST_ORG_RE = re.compile(r"테스트|\btest\b|xss|<script", re.IGNORECASE)


@dataclass
class Submission:
    """폼 제출 한 건"""

    title: str
    fields: dict[str, str]
    admin_url: str | None
    # 원글에서 잘린 항목 이름. 댓글로 전문을 채우면 빠진다.
    truncated: set[str] = field(default_factory=set)

    def get(self, labels: tuple[str, ...]) -> str:
        """여러 이름 중 처음 있는 항목 값을 돌려줍니다. 없으면 빈 문자열."""
        for label in labels:
            if label in self.fields:
                return self.fields[label]
        return ""

    @property
    def org(self) -> str:
        return self.get(ORG_LABELS)

    @property
    def contact(self) -> str:
        return self.get(CONTACT_LABELS)

    @property
    def phone(self) -> str:
        return self.get(PHONE_LABELS)

    @property
    def email(self) -> str:
        return self.get(EMAIL_LABELS)

    @property
    def target(self) -> str:
        return self.get(TARGET_LABELS)

    @property
    def scale(self) -> str:
        return self.get(SCALE_LABELS)

    @property
    def schedule(self) -> str:
        return self.get(SCHEDULE_LABELS)

    @property
    def source(self) -> str:
        return self.get(SOURCE_LABELS)

    @property
    def body(self) -> str:
        """문의 내용과 기타 문의를 이어 붙인 본문"""
        parts = []
        for label in BODY_LABELS:
            if label in self.fields:
                value = self.fields[label]
                parts.append(value if label == "문의 내용" else f"[{label}] {value}")
        return "\n\n".join(parts)

    @property
    def is_dev(self) -> bool:
        """개발 서버에서 온 제출인지"""
        if self.admin_url is None:
            return False
        return urlparse(self.admin_url).hostname == "admin.dev.codle.io"


def unslack(text: str) -> str:
    """슬랙 링크 표기 <tel:010|010> 를 보이는 글자로 바꾸고 HTML 엔티티를 풉니다."""
    text = SLACK_LINK_RE.sub(lambda m: m.group(1) or m.group(2), text)
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def form_text(message: dict) -> str:
    """폼 봇 글을 블록 단위 줄바꿈으로 읽습니다.

    폼 봇(jce-class-rails SlackNotifyJob)은 blocks 만 보내고 text 를 비워 둔다.
    그러면 슬랙이 블록들을 공백으로 이어 text 를 만들어서 「해커톤 문의 새 제출
    *제출 시각*」처럼 제목과 항목이 한 줄에 붙는다. 그래서 블록을 직접 읽는다.

    Args:
        message: 슬랙 메시지

    Returns:
        str: 블록마다 줄을 나눈 글. 블록이 없으면 text 그대로
    """
    if not message.get("blocks"):
        return message.get("text", "")
    parts = []
    for block in message["blocks"]:
        if block["type"] in ("header", "section") and "text" in block:
            parts.append(block["text"]["text"])
        elif block["type"] == "context":
            parts.append(" ".join(e.get("text", "") for e in block["elements"]))
    return "\n".join(parts)


def parse_submission(text: str) -> Submission | None:
    """폼 제출 글을 파싱합니다.

    Args:
        text: 슬랙 메시지 text

    Returns:
        Submission | None: 폼 제출 글이 아니면 None
    """
    lines = text.split("\n")
    title = lines[0].strip()
    if title not in FORM_TITLES:
        return None

    fields: dict[str, str] = {}
    truncated: set[str] = set()
    admin_url = None
    label = None
    buffer: list[str] = []

    def flush():
        if label is None:
            return
        value = "\n".join(buffer).strip()
        if TRUNCATED_RE.search(value):
            truncated.add(label)
            value = TRUNCATED_RE.sub("", value).rstrip()
        fields[label] = unslack(value)

    for line in lines[1:]:
        admin_match = ADMIN_LINK_RE.match(line.strip())
        if admin_match:
            admin_url = admin_match.group(1)
            continue
        label_match = LABEL_RE.match(line.strip())
        if label_match:
            flush()
            label = label_match.group(1).strip()
            buffer = []
            continue
        buffer.append(line)
    flush()

    return Submission(
        title=title, fields=fields, admin_url=admin_url, truncated=truncated
    )


def apply_continuations(submission: Submission, reply_texts: list[str]) -> None:
    """폼 봇이 스레드 댓글로 이어 단 전문으로 잘린 항목을 채웁니다.

    댓글 첫 줄이 항목 이름이고 나머지가 전문입니다. 한 항목이 여러 댓글로
    나뉘면 순서대로 이어 붙입니다.

    Args:
        submission: parse_submission 결과. 제자리에서 고칩니다.
        reply_texts: 폼 봇이 단 댓글 text, 오래된 것부터
    """
    collected: dict[str, list[str]] = {}
    for text in reply_texts:
        first, _, rest = text.partition("\n")
        name = CONTINUATION_LABEL_RE.match(first.strip()).group(1).strip()
        if name in submission.truncated:
            collected.setdefault(name, []).append(rest)
    for name, parts in collected.items():
        submission.fields[name] = unslack("".join(parts).strip())
        submission.truncated.discard(name)


def phone_digits(raw: str) -> str:
    """연락처에서 숫자만 남깁니다."""
    return re.sub(r"\D", "", raw)


def format_phone(raw: str) -> str:
    """연락처를 010-1234-5678 꼴로 맞춥니다. 형식이 틀리면 원문을 돌려줍니다."""
    if not is_valid_phone(raw):
        return raw
    digits = phone_digits(raw)
    if digits.startswith("02"):
        return f"02-{digits[2:-4]}-{digits[-4:]}"
    return f"{digits[:3]}-{digits[3:-4]}-{digits[-4:]}"


def is_valid_phone(raw: str) -> bool:
    """국내 전화번호 꼴인지 봅니다. 010-12345-6789 처럼 자릿수가 어긋나면 False."""
    return (
        re.fullmatch(r"0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}", raw.strip().rstrip("."))
        is not None
    )


def is_dummy_phone(raw: str) -> bool:
    """010-0000-0000, 111111111 처럼 한 숫자만 반복한 연락처인지"""
    digits = phone_digits(raw)
    if not digits:
        return False
    tail = digits[3:] if digits.startswith("010") else digits
    return len(set(tail)) <= 1


def is_test_submission(submission: Submission) -> bool:
    """테스트·더미 제출인지 판정합니다."""
    if submission.is_dev:
        return True
    if TEST_ORG_RE.search(submission.org):
        return True
    if is_dummy_phone(submission.phone):
        return True
    return "ENT-" in submission.source


def customer_type(submission: Submission) -> str | None:
    """노션 고객유형을 정합니다.

    폼의 "대상"을 따르되, 기관명에 대학이 들어가면 대학으로 봅니다. 대학 담당자도
    학교 랜딩에서 "학교"를 고르는 일이 많기 때문입니다(실제 대학 문의 다수).
    """
    if "대학" in submission.org:
        return "대학·사업단"
    target = submission.target
    if "대학" in target:
        return "대학·사업단"
    if "학교" in target:
        return "고교·K12"
    if "기업" in target:
        return "기업"
    if "기관" in target or "지자체" in target:
        return "지자체·공공"
    return None


def parse_headcount(scale: str) -> int | None:
    """규모에서 인원 수를 읽습니다. 숫자가 하나뿐이고 팀 단위가 아닐 때만."""
    if "팀" in scale:
        return None
    numbers = re.findall(r"\d+", scale)
    if len(numbers) != 1:
        return None
    return int(numbers[0])


def parse_schedule(text: str) -> tuple[date, date | None] | None:
    """희망 시기에서 연·월·일이 모두 있는 날짜를 읽습니다.

    Returns:
        tuple[date, date | None] | None: (시작, 끝). 하루면 끝은 None. 못 읽으면 None
    """
    found = re.findall(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", text)
    # 사람이 적은 글이라 2026.13.40 같은 없는 날짜도 온다. 그런 날짜는 읽지 않는다.
    dates = [
        date(int(y), int(m), int(d))
        for y, m, d in found
        if 1 <= int(m) <= 12 and 1 <= int(d) <= calendar.monthrange(int(y), int(m))[1]
    ]
    if not dates or len(dates) > 2:
        return None
    if len(dates) == 2 and dates[1] > dates[0]:
        return dates[0], dates[1]
    return dates[0], None


def parse_submitted_at(submission: Submission) -> datetime | None:
    """제출 시각을 KST datetime 으로 읽습니다."""
    raw = submission.get(SUBMITTED_LABELS).strip()
    # 형식이 다르면 읽지 않는다. 여기서 터지면 그 뒤 문의까지 접수가 막힌다.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw):
        return None
    return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def is_business_hours(now: datetime, is_business_day: bool) -> bool:
    """업무시간(영업일 10:00~19:00)인지

    Args:
        now: KST 시각
        is_business_day: 그날이 영업일인지. 공휴일 조회가 네트워크라 밖에서 받습니다.
    """
    return is_business_day and BUSINESS_START <= now.time() < BUSINESS_END


def estimate_draft(customer: str | None, headcount: int | None) -> str:
    """견적기준표 v1.0 으로 참고용 견적 초안 한 줄을 만듭니다.

    Args:
        customer: 노션 고객유형
        headcount: 인원 수

    Returns:
        str: 슬랙에 그대로 쓰는 한 줄
    """
    if headcount is None or customer is None:
        return "규모 확인 후 산정"
    if customer == "고교·K12":
        if headcount <= 40:
            return (
                f"고교·K12 운영형 {headcount}명 → 200만 원 "
                "(부가세 포함, 40명 구간 최소 금액)"
            )
        total = 200 + 6 * (headcount - 40)
        return (
            f"고교·K12 운영형 {headcount}명 → {total:,}만 원 "
            f"(부가세 포함, 200만 + 6만×{headcount - 40}명)"
        )
    platform = 1.5 * headcount
    if headcount >= 40:
        total = platform + 50 + 50 + 40
        return (
            f"{customer} 운영형 {headcount}명 → {total:,.1f}만 원 "
            f"(공급가, 부가세 별도: 플랫폼 1.5만×{headcount} + 사전교육 50 + 멘토 2명 50 + 운영 40)"
        ).replace(".0만", "만")
    total = platform + 20
    return (
        f"{customer} 플랫폼형 {headcount}명 → {total:,.1f}만 원 "
        f"(공급가, 부가세 별도: 1.5만×{headcount} + 세팅 20)"
    ).replace(".0만", "만")


def notion_properties(
    submission: Submission,
    owner_notion_id: str,
    slack_permalink: str,
    source: str = "인바운드-폼",
) -> dict:
    """리드 파이프라인 새 페이지의 속성을 만듭니다.

    빈 값은 넣지 않습니다. 읽지 못한 규모·희망일정은 비고에 원문으로 남깁니다.

    Args:
        submission: 폼 제출
        owner_notion_id: 담당 노션 사용자 ID
        slack_permalink: 폼 원글 링크
        source: 노션 유입경로. 사람이 붙인 메일 문의는 인바운드-메일

    Returns:
        dict: pages.create 의 properties
    """
    props: dict = {
        "고객·기관명": {
            "title": [{"text": {"content": submission.org or "(기관명 없음)"}}]
        },
        "진행상태": {"select": {"name": "문의접수"}},
        "담당": {"people": [{"id": owner_notion_id}]},
        "유입경로": {"select": {"name": source}},
    }
    submitted_at = parse_submitted_at(submission)
    if submitted_at:
        props["최초 문의일"] = {"date": {"start": submitted_at.isoformat()}}
    customer = customer_type(submission)
    if customer:
        props["고객유형"] = {"select": {"name": customer}}
    if submission.contact:
        props["기관 담당자"] = {
            "rich_text": [{"text": {"content": submission.contact}}]
        }
    if submission.phone:
        props["연락처"] = {"phone_number": submission.phone}
    if submission.email:
        props["이메일"] = {"email": submission.email}

    notes = []
    headcount = parse_headcount(submission.scale)
    if headcount is not None:
        props["규모(명)"] = {"number": headcount}
    elif submission.scale:
        notes.append(f"규모: {submission.scale}")
    schedule = parse_schedule(submission.schedule)
    if schedule:
        start, end = schedule
        props["희망일정"] = {
            "date": {
                "start": start.isoformat(),
                "end": end.isoformat() if end else None,
            }
        }
    elif submission.schedule:
        notes.append(f"희망 시기: {submission.schedule}")

    if submission.source:
        notes.append(f"폼 유입 경로: {submission.source}")
    if submission.body:
        notes.append(f"문의 내용:\n{submission.body}")
    if submission.truncated:
        notes.append("(문의 전문은 어드민에서 확인)")
    if submission.admin_url:
        notes.append(f"어드민: {submission.admin_url}")
    notes.append(f"슬랙: {slack_permalink}")
    props["비고"] = {"rich_text": rich_text_chunks("\n".join(notes))}
    return props


def rich_text_chunks(text: str) -> list[dict]:
    """노션 rich_text 한 조각 2000자 제한에 맞춰 나눕니다."""
    return [
        {"text": {"content": text[i : i + 2000]}} for i in range(0, len(text), 2000)
    ][:100]


def format_summary(
    submission: Submission,
    *,
    mention_user_id: str | None,
    deferred: bool,
    notion_url: str | None,
    notion_note: str | None,
    estimate: str | None,
    flags: list[str],
) -> str:
    """스레드에 다는 정리 글을 만듭니다.

    Args:
        submission: 폼 제출
        mention_user_id: 지금 멘션할 사람. 업무시간 외면 None
        deferred: 업무시간 외라 멘션을 다음 업무일 10:00으로 미루는지
        notion_url: 노션 리드 페이지 링크
        notion_note: 노션 링크 옆에 붙일 설명 (예: "기존 페이지")
        estimate: 견적 초안 한 줄
        flags: 맨 앞에 굵게 붙일 표시 (예: "[긴급] 재문의")

    Returns:
        str: 슬랙 mrkdwn
    """
    head = " ".join(f"*{flag}*" for flag in flags)
    lines = [SUMMARY_MARKER + (f" {head}" if head else "")]
    if mention_user_id:
        lines.append(
            f"<@{mention_user_id}> 새 {submission.title.split(' 새 ')[0]}입니다."
        )
    elif deferred:
        # 여기서 <@ID> 로 쓰면 바로 알림이 가므로 이름 없이 쓴다
        lines.append("업무시간 외 접수라 다음 업무일 10:00에 담당자를 멘션합니다.")
    lines.append("")

    org = submission.org or "(기관명 없음)"
    customer = customer_type(submission)
    lines.append(f"기관: {org}" + (f" ({customer})" if customer else ""))
    phone = submission.phone
    if phone and not is_valid_phone(phone):
        phone = f"{phone} ⚠️ 연락처 확인 필요, 메일로 연락"
    contact = " / ".join(part for part in (submission.contact, phone) if part)
    if contact:
        lines.append(f"담당: {contact}")
    if submission.email:
        lines.append(f"이메일: {submission.email}")
    scale_line = " · ".join(
        part
        for part in (
            f"규모: {submission.scale}" if submission.scale else "",
            f"희망 시기: {submission.schedule}" if submission.schedule else "",
        )
        if part
    )
    if scale_line:
        lines.append(scale_line)
    if submission.source:
        lines.append(f"유입: {submission.source}")

    if submission.body:
        lines += ["", "*문의 내용*", submission.body]
    if submission.truncated:
        lines.append("_(전문이 슬랙에서 잘렸습니다. 어드민에서 확인해 주세요)_")

    if estimate:
        lines += [
            "",
            "*견적 초안* (참고용, 상담 후 확정)",
            estimate,
            "기준: 해커톤 견적기준표 v1.0",
        ]

    lines.append("")
    if notion_url:
        lines.append(
            f"노션: <{notion_url}|리드 페이지>"
            + (f" ({notion_note})" if notion_note else "")
        )
    if submission.admin_url:
        lines.append(f"어드민: <{submission.admin_url}|제출 원문>")
    if mention_user_id or deferred:
        lines.append(
            "확인하면 이 글에 이모지를 눌러 주세요. 2시간 동안 반응이 없으면 DM 드립니다."
        )
    return "\n".join(lines)


def is_vacation_profile(profile: dict) -> bool:
    """슬랙 프로필 상태 메시지에 휴가 또는 🌴 가 있는지"""
    text = profile.get("status_text", "")
    emoji = profile.get("status_emoji", "")
    return any(k in text for k in VACATION_KEYWORDS) or emoji in VACATION_EMOJIS


def summary_notion_url(summary_text: str) -> str | None:
    """정리 글에서 노션 리드 페이지 링크를 꺼냅니다. 없으면 None."""
    match = NOTION_LINK_RE.search(summary_text)
    return match.group(1) if match else None


def summary_org(summary_text: str) -> str:
    """정리 글의 기관명. 사람이 붙인 문의는 원글이 폼이 아니라 여기서 읽습니다."""
    match = ORG_LINE_RE.search(summary_text)
    return match.group(1) if match else "(기관명 없음)"


def permalink(ts: str) -> str:
    """문의 채널 메시지 링크"""
    return (
        f"https://{SLACK_WORKSPACE_DOMAIN}/archives/{CHANNEL_ID}/p{ts.replace('.', '')}"
    )
