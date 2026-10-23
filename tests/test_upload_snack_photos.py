"""간식증빙사진 드라이브 업로드 테스트"""

from datetime import datetime
from unittest.mock import call, patch

import pytest
import requests

from scripts import upload_snack_photos as script
from scripts.upload_snack_photos import KST

CHANNELS = [
    {"id": "c1", "type": 4, "name": "공지_팀모노리스"},
    {"id": "c2", "type": 4, "name": "공지_스마트인재개발원"},
    {"id": "c3", "type": 4, "name": "공지_팀모노리스2"},
    {"id": "f1", "type": 15, "name": "대구-영남중-공지", "parent_id": "c1"},
    {"id": "f2", "type": 15, "name": "거창대성고-공지", "parent_id": "c2"},
    {"id": "f3", "type": 15, "name": "자유게시판", "parent_id": "c1"},
    {"id": "f4", "type": 15, "name": "장성중1반-공지", "parent_id": "c3"},
    {"id": "t1", "type": 0, "name": "서울고-공지", "parent_id": "c1"},
]


def test_우리_카테고리의_공지_포럼만_학교로_본다():
    assert script.find_school_forums(CHANNELS) == {
        "f1": "대구-영남중",
        "f4": "장성중1반",
    }


def test_같은_이름이_있으면_번호를_붙인다():
    posted_at = datetime(2026, 10, 28, 14, 0, tzinfo=KST)
    taken = {"서울고_10월28일_AI동행프로그램.jpg"}

    name = script.make_file_name("서울고", posted_at, ".jpg", taken)

    assert name == "서울고_10월28일_AI동행프로그램_2.jpg"


def _message(message_id, attachments, reactions=()):
    return {
        "id": message_id,
        "timestamp": "2026-09-21T08:00:00+00:00",
        "attachments": attachments,
        "reactions": list(reactions),
    }


BOT_CHECK = {"emoji": {"name": "✅"}, "me": True}
HUMAN_CHECK = {"emoji": {"name": "✅"}, "me": False}


def _image(attachment_id, filename="a.JPG"):
    return {
        "id": attachment_id,
        "filename": filename,
        "content_type": "image/jpeg",
        "url": f"https://cdn/{attachment_id}",
    }


THREADS = {
    "threads": [
        {"id": "th1", "parent_id": "f1", "name": "9.21(월) 17:00 - 🍪 간식증빙사진"},
        {"id": "th2", "parent_id": "f1", "name": "9.21(월) 17:00 - 📋 출석부사진"},
    ]
}


def _run_main(messages, existing=()):
    """외부 API 를 막고 main 을 돌린 뒤 (create_file, create_reaction) mock 을 돌려준다."""
    with (
        patch.object(script, "get_guild_channels", return_value=CHANNELS),
        patch.object(script, "get_active_threads", return_value=THREADS),
        patch.object(script, "get_channel_messages", side_effect=messages),
        patch.object(script, "list_folder_files", return_value=list(existing)),
        patch.object(script, "get_attachment", return_value=b"img"),
        patch.object(script, "create_file") as create_file,
        patch.object(script, "create_reaction") as create_reaction,
        patch.object(script.time, "sleep"),
    ):
        script.main()
    return create_file, create_reaction


@pytest.fixture(autouse=True)
def folder(monkeypatch):
    monkeypatch.setenv("DISCORD_SNACK_PHOTO_DRIVE_FOLDER_ID", "folder")


def test_간식증빙_글의_새_이미지를_올리고_체크를_단다():
    messages = [
        [
            _message("m3", []),
            _message("m2", [{**_image("doc"), "content_type": "application/pdf"}]),
            _message("m1", [_image("new")]),
        ]
    ]

    create_file, create_reaction = _run_main(messages)

    # 출석부 글은 보지 않는다(get_channel_messages 가 한 번만 불려 side_effect 가 남지 않음).
    create_file.assert_called_once_with(
        "folder",
        "대구-영남중_9월21일_AI동행프로그램.jpg",
        b"img",
        "image/jpeg",
        {"discord_attachment_id": "new"},
    )
    # 이미지가 없는 메시지에는 체크를 달지 않는다.
    create_reaction.assert_called_once_with("th1", "m1", "✅")


def test_봇이_체크한_메시지는_드라이브에_없어도_다시_올리지_않는다():
    # 드라이브에서 파일을 지우거나 옮겨도 다시 올라가면 안 된다.
    messages = [[_message("m1", [_image("a")], reactions=[BOT_CHECK])]]

    create_file, create_reaction = _run_main(messages)

    create_file.assert_not_called()
    create_reaction.assert_not_called()


def test_사람이_단_체크는_올린_표시로_보지_않는다():
    messages = [[_message("m1", [_image("a")], reactions=[HUMAN_CHECK])]]

    create_file, _ = _run_main(messages)

    create_file.assert_called_once()


def test_체크_전에_멈췄던_메시지는_남은_사진만_올리고_체크한다():
    messages = [[_message("m1", [_image("done"), _image("left")])]]
    existing = [
        {
            "id": "d1",
            "name": "x.jpg",
            "appProperties": {"discord_attachment_id": "done"},
        }
    ]

    create_file, create_reaction = _run_main(messages, existing)

    create_file.assert_called_once()
    assert create_file.call_args.args[4] == {"discord_attachment_id": "left"}
    create_reaction.assert_called_once_with("th1", "m1", "✅")


def test_한_학교가_실패해도_다른_학교는_올리고_마지막에_실패를_알린다():
    threads = {
        "threads": [
            {"id": "th1", "parent_id": "f1", "name": "간식증빙사진"},
            {"id": "th4", "parent_id": "f4", "name": "간식증빙사진"},
        ]
    }
    messages = [requests.HTTPError("403"), [_message("m1", [_image("a")])]]

    with pytest.raises(RuntimeError, match="실패 1건"):
        with (
            patch.object(script, "get_guild_channels", return_value=CHANNELS),
            patch.object(script, "get_active_threads", return_value=threads),
            patch.object(script, "get_channel_messages", side_effect=messages),
            patch.object(script, "list_folder_files", return_value=[]),
            patch.object(script, "get_attachment", return_value=b"img"),
            patch.object(script, "create_file") as create_file,
            patch.object(script, "create_reaction") as create_reaction,
            patch.object(script.time, "sleep"),
        ):
            script.main()

    assert create_file.call_args.args[1] == "장성중1반_9월21일_AI동행프로그램.jpg"
    assert create_reaction.call_args == call("th4", "m1", "✅")


def test_보관된_스레드는_끝까지_넘겨_가며_모은다():
    pages = [
        {
            "threads": [{"id": "a", "thread_metadata": {"archive_timestamp": "T1"}}],
            "has_more": True,
        },
        {
            "threads": [{"id": "b", "thread_metadata": {"archive_timestamp": "T2"}}],
            "has_more": False,
        },
    ]
    with patch.object(
        script, "get_public_archived_threads", side_effect=pages
    ) as get_archived:
        threads = script.list_archived_threads("f1")

    assert [t["id"] for t in threads] == ["a", "b"]
    assert get_archived.call_args_list[1].kwargs == {"before": "T1"}
