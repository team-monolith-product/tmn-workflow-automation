from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from app import summarize_deployment as report


def application(
    name, status="OutOfSync", revision="main", repository="jce-service-helm"
):
    return {
        "metadata": {"name": name, "namespace": "argocd-global"},
        "spec": {
            "source": {
                "repoURL": f"https://github.com/team-monolith-product/{repository}",
                "targetRevision": revision,
            }
        },
        "status": {"sync": {"status": status}},
    }


@pytest.fixture
def clients(monkeypatch):
    db = SimpleNamespace(
        properties=SimpleNamespace(pr="PR", assignee="Assignee", title="Title")
    )
    pipeline = SimpleNamespace(
        name="제품 본부",
        pipeline_squads=[SimpleNamespace(squad=SimpleNamespace(notion_db=db))],
    )
    monkeypatch.setattr(
        report,
        "load_config",
        lambda: SimpleNamespace(task_alerts=SimpleNamespace(pipelines=[pipeline])),
    )
    notion = Mock()
    slack = Mock()
    slack.chat_postMessage.return_value = {"ts": "1234567890.123456"}
    monkeypatch.setattr(report, "NotionClient", Mock(return_value=notion))
    monkeypatch.setattr(report, "WebClient", Mock(return_value=slack))
    monkeypatch.setattr(report, "get_email_to_user_id", lambda client: {})
    tasks = Mock(return_value=[])
    monkeypatch.setattr(report, "_query_deployment_tasks", tasks)
    apps = Mock(return_value={"items": [application("user-rails-service-apne2-prd")]})
    monkeypatch.setattr(report, "get_applications", apps)
    monkeypatch.setenv("NOTION_TOKEN", "notion-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-test")
    monkeypatch.setenv("ARGOCD_NAMESPACE", "argocd-global")
    monkeypatch.setenv("ARGOCD_BASE_URL", "https://argocd.example.com/")
    return slack, tasks, apps


def test_services_without_a_deployment_task_still_request_sync(clients):
    slack, _, apps = clients

    report.summarize_deployment()

    apps.assert_called_once_with("argocd-global")
    assert slack.chat_postMessage.call_count == 2
    parent, reply = slack.chat_postMessage.call_args_list
    message = parent.kwargs["text"]
    assert "오늘 예정된 배포가 없네요" in message
    assert "추가 배포 검토 대상: 1개 앱" in message
    assert "user-rails-service-apne2-prd" not in message
    assert "thread_ts" not in parent.kwargs
    assert reply.kwargs["channel"] == parent.kwargs["channel"]
    assert reply.kwargs["thread_ts"] == "1234567890.123456"
    assert reply.kwargs["reply_broadcast"] is False
    details = reply.kwargs["text"]
    assert "OutOfSync · 1개 앱" in details
    assert "하루에 한두 개씩은 꼭 배포해서 이 목록을 줄여야 합니다." in details
    assert (
        "변경사항을 검토한 뒤 Argo CD에서 Sync하고 정상 동작을 확인해 주세요."
        in details
    )
    assert (
        "<https://argocd.example.com/applications/argocd-global/user-rails-service-apne2-prd|"
        in details
    )


def test_closed_pr_only_tasks_do_not_hide_sync_requests(clients, monkeypatch):
    slack, tasks, _ = clients
    tasks.return_value = [{"url": "https://notion.example.com/task", "properties": {}}]
    monkeypatch.setattr(report, "get_pr_links", Mock(return_value=[]))

    report.summarize_deployment()

    parent, reply = slack.chat_postMessage.call_args_list
    message = parent.kwargs["text"]
    assert "오늘 예정된 배포가 없네요" in message
    assert "user-rails-service-apne2-prd" in reply.kwargs["text"]


def test_task_and_repository_notice_remain_alongside_sync_requests(
    clients, monkeypatch
):
    slack, tasks, _ = clients
    tasks.return_value = [
        {
            "url": "https://notion.example.com/task",
            "properties": {"Title": {"title": [{"plain_text": "수정 배포"}]}},
        }
    ]
    monkeypatch.setattr(
        report,
        "get_pr_links",
        Mock(
            return_value=[
                {
                    "url": "https://github.com/team-monolith-product/example/pull/1",
                    "merged": True,
                }
            ]
        ),
    )

    report.summarize_deployment(caller_slack_user_id="U123")

    parent, reply = slack.chat_postMessage.call_args_list
    message = parent.kwargs["text"]
    assert "by <@U123>" in message
    assert "수정 배포" in message
    assert "example#1>✅" in message
    assert "• example" in message
    assert "user-rails-service-apne2-prd" not in message
    assert "user-rails-service-apne2-prd" in reply.kwargs["text"]


def test_only_out_of_sync_production_services_are_listed_once_in_name_order():
    multisource = application("b-jupyter", repository="jce-jupyter-hub-helm.git")
    multisource["spec"]["sources"] = [multisource["spec"].pop("source")]
    summary, message = report.format_sync_requests(
        [
            multisource,
            application("a-user"),
            application("a-user"),
            application("synced", status="Synced"),
            application("unknown", status="Unknown"),
            application("development", revision="develop"),
            application("infrastructure", repository="tmn-infra-helm"),
        ],
        "https://argocd.example.com",
    )

    assert "추가 배포 검토 대상: 2개 앱" in summary
    assert "OutOfSync · 2개 앱" in message
    assert message.index("|a-user>") < message.index("|b-jupyter>")
    assert message.count("|a-user>") == 1
    for name in ["synced", "unknown", "development", "infrastructure"]:
        assert f"|{name}>" not in message


@pytest.mark.parametrize("failure", [requests.HTTPError("403"), KeyError("items")])
def test_lookup_failure_is_reported_instead_of_claiming_no_pending_services(
    clients, failure
):
    slack, _, apps = clients
    apps.side_effect = failure

    report.summarize_deployment()

    slack.chat_postMessage.assert_called_once()
    message = slack.chat_postMessage.call_args.kwargs["text"]
    assert "Argo CD 상태 조회에 실패했습니다" in message
    assert "OutOfSync · 0개" not in message


def test_no_pending_services_preserves_empty_deployment_notice(clients):
    slack, _, apps = clients
    apps.return_value = {"items": [application("user", status="Synced")]}

    report.summarize_deployment()

    slack.chat_postMessage.assert_called_once()
    message = slack.chat_postMessage.call_args.kwargs["text"]
    assert "오늘 예정된 배포가 없네요" in message
    assert "추가 배포 검토 대상" not in message


def test_many_pending_apps_only_add_a_count_to_the_channel_message(clients):
    slack, _, apps = clients
    apps.return_value = {"items": [application(f"service-{i:02}") for i in range(19)]}

    report.summarize_deployment()

    parent, reply = slack.chat_postMessage.call_args_list
    assert "추가 배포 검토 대상: 19개 앱" in parent.kwargs["text"]
    assert "argocd.example.com/applications/" not in parent.kwargs["text"]
    assert reply.kwargs["text"].count("argocd.example.com/applications/") == 19


def test_failed_parent_message_does_not_send_an_orphaned_reply(clients):
    slack, _, _ = clients
    slack.chat_postMessage.side_effect = RuntimeError("Slack send failed")

    with pytest.raises(RuntimeError, match="Slack send failed"):
        report.summarize_deployment()

    slack.chat_postMessage.assert_called_once()


def test_failed_thread_reply_is_not_silenced(clients):
    slack, _, _ = clients
    slack.chat_postMessage.side_effect = [
        {"ts": "1234567890.123456"},
        RuntimeError("Slack reply failed"),
    ]

    with pytest.raises(RuntimeError, match="Slack reply failed"):
        report.summarize_deployment()

    assert slack.chat_postMessage.call_count == 2
