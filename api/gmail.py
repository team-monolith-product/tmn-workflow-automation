"""Gmail REST API transport를 제공합니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GmailApiError(RuntimeError):
    """응답 본문을 노출하지 않는 Gmail API 오류입니다."""

    def __init__(self, operation: str, status_code: int):
        super().__init__(f"Gmail API {operation} failed with status {status_code}")
        self.operation = operation
        self.status_code = status_code


class GmailHistoryExpired(GmailApiError):
    """저장한 history ID를 Gmail에서 더는 조회할 수 없습니다."""


class GmailMessageNotFound(GmailApiError):
    """변경 내역에 있던 메일을 Gmail에서 더는 조회할 수 없습니다."""


@dataclass(frozen=True)
class GmailWatch:
    history_id: str
    expiration: str


@dataclass(frozen=True)
class GmailHistory:
    message_ids: list[str]
    history_id: str


class GmailClient:
    """Gmail 원본 응답만 다루는 동기 REST client입니다."""

    def __init__(
        self,
        *,
        mailbox_email: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        timeout_seconds: int = 15,
    ) -> None:
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=GOOGLE_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=[GMAIL_READONLY_SCOPE],
        )
        self._mailbox = quote(mailbox_email, safe="")
        self._session = AuthorizedSession(credentials)
        self._timeout_seconds = timeout_seconds

    def close(self) -> None:
        self._session.close()

    def watch(self, topic_name: str) -> GmailWatch:
        payload = self._request_json(
            "POST",
            f"{GMAIL_API_ROOT}/users/{self._mailbox}/watch",
            operation="watch",
            json={
                "topicName": topic_name,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "INCLUDE",
            },
        )
        return GmailWatch(
            history_id=str(payload["historyId"]),
            expiration=str(payload["expiration"]),
        )

    def list_history(self, start_history_id: str) -> GmailHistory:
        message_ids: list[str] = []
        seen_message_ids: set[str] = set()
        page_token: str | None = None
        current_history_id = start_history_id

        while True:
            params: dict[str, str | int] = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
                "labelId": "INBOX",
                "maxResults": 500,
            }
            if page_token:
                params["pageToken"] = page_token

            payload = self._request_json(
                "GET",
                f"{GMAIL_API_ROOT}/users/{self._mailbox}/history",
                operation="list_history",
                params=params,
                history_request=True,
            )
            current_history_id = str(payload.get("historyId", current_history_id))

            for history in payload.get("history", []):
                for added in history.get("messagesAdded", []):
                    message_id = str(added.get("message", {}).get("id", ""))
                    if message_id and message_id not in seen_message_ids:
                        seen_message_ids.add(message_id)
                        message_ids.append(message_id)

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return GmailHistory(message_ids=message_ids, history_id=current_history_id)

    def list_recent_messages(self, after_epoch_seconds: int) -> list[str]:
        message_ids: list[str] = []
        page_token: str | None = None

        while True:
            params: dict[str, str | int] = {
                "q": f"in:inbox after:{after_epoch_seconds}",
                "maxResults": 100,
                "includeSpamTrash": "false",
            }
            if page_token:
                params["pageToken"] = page_token

            payload = self._request_json(
                "GET",
                f"{GMAIL_API_ROOT}/users/{self._mailbox}/messages",
                operation="list_recent_messages",
                params=params,
            )
            message_ids.extend(
                str(message["id"])
                for message in payload.get("messages", [])
                if message.get("id")
            )

            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        return message_ids

    def get_raw_message(self, message_id: str) -> str:
        payload = self._request_json(
            "GET",
            f"{GMAIL_API_ROOT}/users/{self._mailbox}/messages/{quote(message_id, safe='')}",
            operation="get_message",
            params={"format": "raw"},
        )
        return str(payload["raw"])

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        history_request: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = self._session.request(
            method,
            url,
            timeout=self._timeout_seconds,
            **kwargs,
        )
        if response.status_code >= 400:
            if history_request and response.status_code == 404:
                error_type = GmailHistoryExpired
            elif operation == "get_message" and response.status_code == 404:
                error_type = GmailMessageNotFound
            else:
                error_type = GmailApiError
            raise error_type(operation, response.status_code)
        return response.json()
