"""발송 기록 테스트용 가짜 로그.

sms_send 의 중복 차단(부분 UNIQUE 인덱스)을 흉내냅니다. campaign 이 있고
상태가 '실패'가 아닌 행이 있으면 같은 번호를 다시 넣지 못합니다. campaign 이
None(개인 CS)이면 언제나 들어갑니다.
"""

from typing import Any


class FakeLog:
    """service.sms.log 를 대신합니다."""

    def __init__(self):
        self.rows: list[dict[str, Any]] = []
        self._next = 1

    def digits(self, phone: str) -> str:
        return "".join(char for char in phone if char.isdigit())

    def claim(
        self,
        campaign: str | None,
        phones: list[str],
        *,
        content: str,
        channel_id: str | None = None,
        requested_by: str | None = None,
    ) -> dict[str, int]:
        taken = {
            row["phone"]
            for row in self.rows
            if row["campaign"] is not None
            and row["campaign"] == campaign
            and row["status"] != "실패"
        }
        won = {}
        for phone in phones:
            phone = self.digits(phone)
            if campaign is not None and phone in taken:
                continue
            row = {
                "id": self._next,
                "campaign": campaign,
                "phone": phone,
                "status": "발송중",
                "content": content,
                "channel_id": channel_id,
                "requested_by": requested_by,
                "message_key": None,
                "sent_at": None,
            }
            self.rows.append(row)
            won[phone] = self._next
            self._next += 1
            taken.add(phone)
        return won

    def mark(self, ids, status, *, message_key=None, sent_at=None) -> None:
        for row in self.rows:
            if row["id"] in ids:
                row["status"] = status
                row["message_key"] = message_key or row["message_key"]
                row["sent_at"] = sent_at

    def history(self, phone: str, limit: int = 20) -> list[dict[str, Any]]:
        want = self.digits(phone)
        return [row for row in reversed(self.rows) if row["phone"] == want][:limit]

    def pending(self, campaign: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if row["campaign"] == campaign and row["status"] == "발송중"
        ]

    def status_of(self, phone: str) -> list[str]:
        """그 번호 행들의 상태. 테스트 편의용."""
        want = self.digits(phone)
        return [row["status"] for row in self.rows if row["phone"] == want]
