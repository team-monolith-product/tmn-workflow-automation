"""발송 기록 테스트용 가짜 로그.

sms_send 의 중복 차단(부분 UNIQUE 인덱스)을 흉내냅니다. campaign 이 있고
`failed_at` 이 비어 있는 행이 있으면 같은 번호를 다시 넣지 못합니다.
campaign 이 None(개인 CS)이면 언제나 들어갑니다.
"""

import datetime
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
        entries: list[dict[str, Any]],
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
            and row["failed_at"] is None
        }
        won = {}
        for entry in entries:
            phone = self.digits(entry["to"])
            if campaign is not None and phone in taken:
                continue
            self.rows.append(
                {
                    "id": self._next,
                    "campaign": campaign,
                    "phone": phone,
                    "content": content,
                    "variables": {
                        key: value
                        for key, value in entry.items()
                        if key != "to" and value
                    },
                    "channel_id": channel_id,
                    "requested_by": requested_by,
                    "message_key": None,
                    "claimed_at": datetime.datetime.now(),
                    "sent_at": None,
                    "scheduled_for": None,
                    "failed_at": None,
                    "confirmed_at": None,
                }
            )
            won[phone] = self._next
            self._next += 1
            taken.add(phone)
        return won

    def _rows_by_id(self, ids):
        return [row for row in self.rows if row["id"] in ids]

    def mark_sent(self, ids, *, message_key=None, scheduled_for=None) -> None:
        for row in self._rows_by_id(ids):
            row["sent_at"] = datetime.datetime.now()
            row["scheduled_for"] = scheduled_for
            row["message_key"] = message_key

    def mark_failed(self, ids) -> None:
        for row in self._rows_by_id(ids):
            row["failed_at"] = datetime.datetime.now()

    def history(self, phone: str, limit: int = 20) -> list[dict[str, Any]]:
        want = self.digits(phone)
        return [row for row in reversed(self.rows) if row["phone"] == want][:limit]

    def pending(self, campaign: str) -> list[dict[str, Any]]:
        return [
            row
            for row in self.rows
            if row["campaign"] == campaign
            and row["sent_at"] is None
            and row["failed_at"] is None
        ]

    def stages(self, phone: str) -> list[str]:
        """그 번호 행들이 어느 단계인지. 테스트 편의용."""
        want = self.digits(phone)
        out = []
        for row in self.rows:
            if row["phone"] != want:
                continue
            if row["failed_at"]:
                out.append("실패")
            elif row["sent_at"]:
                out.append("발송")
            else:
                out.append("모름")
        return out
