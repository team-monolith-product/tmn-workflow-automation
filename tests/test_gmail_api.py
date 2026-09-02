from unittest.mock import MagicMock, patch

import pytest

from api.gmail import GmailClient, GmailHistoryExpired, GmailMessageNotFound


def response(status_code, payload):
    result = MagicMock()
    result.status_code = status_code
    result.json.return_value = payload
    return result


def client_with_session(session):
    with patch("api.gmail.AuthorizedSession", return_value=session):
        return GmailClient(
            mailbox_email="funzero@team-mono.com",
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
        )


def test_watch_limits_notifications_to_inbox():
    session = MagicMock()
    session.request.return_value = response(
        200, {"historyId": "100", "expiration": "999"}
    )
    gmail = client_with_session(session)

    watch = gmail.watch("projects/test/topics/recovery-mail")

    assert watch.history_id == "100"
    request = session.request.call_args
    assert request.args[:2] == (
        "POST",
        "https://gmail.googleapis.com/gmail/v1/users/funzero%40team-mono.com/watch",
    )
    assert request.kwargs["json"] == {
        "topicName": "projects/test/topics/recovery-mail",
        "labelIds": ["INBOX"],
        "labelFilterBehavior": "INCLUDE",
    }


def test_list_history_collects_unique_message_ids_across_pages():
    session = MagicMock()
    session.request.side_effect = [
        response(
            200,
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "m1"}}]},
                    {"messagesAdded": [{"message": {"id": "m2"}}]},
                ],
                "historyId": "110",
                "nextPageToken": "next",
            },
        ),
        response(
            200,
            {
                "history": [
                    {"messagesAdded": [{"message": {"id": "m2"}}]},
                    {"messagesAdded": [{"message": {"id": "m3"}}]},
                ],
                "historyId": "120",
            },
        ),
    ]
    gmail = client_with_session(session)

    history = gmail.list_history("100")

    assert history.message_ids == ["m1", "m2", "m3"]
    assert history.history_id == "120"
    assert session.request.call_args_list[1].kwargs["params"]["pageToken"] == "next"


def test_list_history_distinguishes_expired_cursor():
    session = MagicMock()
    session.request.return_value = response(404, {"error": "not found"})
    gmail = client_with_session(session)

    with pytest.raises(GmailHistoryExpired) as exc_info:
        gmail.list_history("expired")

    assert exc_info.value.status_code == 404
    assert "not found" not in str(exc_info.value)


def test_get_message_distinguishes_deleted_message():
    session = MagicMock()
    session.request.return_value = response(404, {"error": "not found"})
    gmail = client_with_session(session)

    with pytest.raises(GmailMessageNotFound):
        gmail.get_raw_message("deleted")
