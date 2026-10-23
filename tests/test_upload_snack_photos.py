"""간식증빙사진 드라이브 업로드 테스트"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from scripts import upload_snack_photos as script
from scripts.upload_snack_photos import KST

CHANNELS = [
    {"id": "c1", "type": 4, "name": "공지_팀모노리스"},
    {"id": "c2", "type": 4, "name": "공지_스마트인재개발원"},
    {"id": "f1", "type": 15, "name": "대구-영남중-공지", "parent_id": "c1"},
    {"id": "f2", "type": 15, "name": "거창대성고-공지", "parent_id": "c2"},
    {"id": "f3", "type": 15, "name": "자유게시판", "parent_id": "c1"},
    {"id": "t1", "type": 0, "name": "서울고-공지", "parent_id": "c1"},
]


def test_우리_카테고리의_공지_포럼만_학교로_본다():
    assert script.find_school_forums(CHANNELS) == {"f1": "대구-영남중"}


def test_간식_이름에서_구분자와_군더더기를_뺀다():
    assert script.clean_snack_name(" 햄버거.\n") == "햄버거"
    assert script.clean_snack_name("'컵_라면'") == "컵라면"
    assert script.clean_snack_name("") == "간식"


def test_같은_이름이_있으면_번호를_붙인다():
    posted_at = datetime(2026, 10, 28, 14, 0, tzinfo=KST)
    taken = {"서울고_10월28일_햄버거_AI동행프로그램.jpg"}

    name = script.make_file_name("서울고", posted_at, "햄버거", ".jpg", taken)

    assert name == "서울고_10월28일_햄버거_AI동행프로그램_2.jpg"


def _message(message_id, attachments, timestamp="2026-09-21T08:00:00+00:00"):
    return {"id": message_id, "timestamp": timestamp, "attachments": attachments}


def _image(attachment_id, filename="a.JPG"):
    return {
        "id": attachment_id,
        "filename": filename,
        "content_type": "image/jpeg",
        "url": f"https://cdn/{attachment_id}",
    }


def test_새_이미지만_올리고_이미_올린_첨부는_건너뛴다(monkeypatch):
    monkeypatch.setenv("SNACK_PHOTO_DRIVE_FOLDER_ID", "folder")
    threads = {
        "threads": [
            {
                "id": "th1",
                "parent_id": "f1",
                "name": "9.21(월) 17:00 - 🍪 간식증빙사진",
            },
            {"id": "th2", "parent_id": "f1", "name": "9.21(월) 17:00 - 📋 출석부사진"},
        ]
    }
    messages = [
        _message("m2", [_image("new")]),
        _message(
            "m1", [_image("old"), {**_image("doc"), "content_type": "application/pdf"}]
        ),
    ]
    existing = [
        {"id": "d1", "name": "x.jpg", "appProperties": {"discord_attachment_id": "old"}}
    ]

    with (
        patch.object(script, "get_guild_channels", return_value=CHANNELS),
        patch.object(script, "get_active_threads", return_value=threads),
        patch.object(
            script, "get_channel_messages", return_value=messages
        ) as get_messages,
        patch.object(script, "list_folder_files", return_value=existing),
        patch.object(script, "get_attachment", return_value=b"img"),
        patch.object(script, "ask_snack_name", return_value="피자"),
        patch.object(script, "create_file") as create_file,
        patch.object(script, "OpenAI", MagicMock()),
    ):
        script.main()

    # 출석부 스레드는 보지 않는다.
    get_messages.assert_called_once_with("th1", limit=100, before=None)
    create_file.assert_called_once_with(
        "folder",
        "대구-영남중_9월21일_피자_AI동행프로그램.jpg",
        b"img",
        "image/jpeg",
        {"discord_attachment_id": "new"},
    )


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
