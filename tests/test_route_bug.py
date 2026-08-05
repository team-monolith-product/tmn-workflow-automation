"""버그 신고 담당자 선정 로직 테스트"""

from unittest.mock import MagicMock, patch

from app.route_bug import (
    KNOWLEDGE_ACTOR,
    TeamAndPriority,
    extract_team_and_priority_from_report_text,
    search_knowledge,
    select_assignee_email,
    select_candidate_emails,
)

TEAM_TO_EMAILS = {
    "fe": ["shc@team-mono.com", "kbc@team-mono.com"],
    "be": ["ksy@team-mono.com", "peko@team-mono.com"],
    "ie": ["byb@team-mono.com", "rhs@team-mono.com", "pky@team-mono.com"],
}

CODLE_EMAILS = [
    "shc@team-mono.com",
    "kbc@team-mono.com",
    "peko@team-mono.com",
    "yjlee@team-mono.com",
]


class TestSearchKnowledge:
    async def test_logs_queries_as_bot(self):
        """봇이 부른 검색은 사람이 친 검색과 구분되게 기록한다"""
        with patch("app.route_bug.connect"), patch(
            "app.route_bug.search_items", return_value=[]
        ) as mock_search_items:
            await search_knowledge.ainvoke(
                {"query": "배부순서", "channel": "t_개발_백"}
            )

        assert mock_search_items.call_args.kwargs["actor"] == KNOWLEDGE_ACTOR
        assert mock_search_items.call_args.kwargs["tool"] == "route_bug"
        assert mock_search_items.call_args.kwargs["channel"] == "t_개발_백"


class TestExtractTeamAndPriority:
    async def test_returns_structured_response(self):
        """에이전트의 구조화 출력을 직군/우선순위 튜플로 푼다"""
        agent = MagicMock()
        agent.ainvoke = _async_return(
            {"structured_response": TeamAndPriority(team="be", priority="높음")}
        )

        with patch("app.route_bug.ChatOpenAI"), patch(
            "app.route_bug.create_react_agent", return_value=agent
        ):
            team, priority = await extract_team_and_priority_from_report_text("신고")

        assert (team, priority) == ("be", "높음")

    async def test_gives_the_agent_the_knowledge_tool(self):
        """에이전트가 지식베이스 검색 도구를 들고 판단한다"""
        agent = MagicMock()
        agent.ainvoke = _async_return(
            {"structured_response": TeamAndPriority(team="fe", priority="보통")}
        )

        with patch("app.route_bug.ChatOpenAI"), patch(
            "app.route_bug.create_react_agent", return_value=agent
        ) as mock_create_agent:
            await extract_team_and_priority_from_report_text("신고")

        assert mock_create_agent.call_args.args[1] == [search_knowledge]


def _async_return(value):
    """await 하면 주어진 값을 돌려주는 가짜 코루틴 함수"""

    async def _call(*args, **kwargs):
        return value

    return _call


class TestSelectCandidateEmails:
    def test_narrows_to_product_and_team(self):
        """제품과 직군의 교집합으로 후보를 좁힌다"""
        candidate_emails, reason = select_candidate_emails(
            "be", "코들", TEAM_TO_EMAILS, CODLE_EMAILS
        )

        assert candidate_emails == ["peko@team-mono.com"]
        assert "코들 담당 be팀 영역." == reason

    def test_excludes_product_member_without_team(self):
        """제품팀이라도 직군 그룹에 없는 인원은 후보가 아니다"""
        candidate_emails, _ = select_candidate_emails(
            "fe", "코들", TEAM_TO_EMAILS, CODLE_EMAILS
        )

        assert "yjlee@team-mono.com" not in candidate_emails
        assert candidate_emails == ["shc@team-mono.com", "kbc@team-mono.com"]

    def test_falls_back_to_whole_team_when_product_has_none(self):
        """제품팀에 해당 직군 인원이 없으면 직군 전체로 넓힌다"""
        candidate_emails, reason = select_candidate_emails(
            "ie", "코들", TEAM_TO_EMAILS, CODLE_EMAILS
        )

        assert candidate_emails == TEAM_TO_EMAILS["ie"]
        assert "코들팀에 ie 인원이 없어" in reason


class TestSelectAssigneeEmail:
    def test_picks_least_assigned(self):
        """후보 중 최근 담당 건수가 가장 적은 사람을 뽑는다"""
        _, assignee_email = select_assignee_email(
            TEAM_TO_EMAILS["fe"],
            "코들 담당 fe팀 영역.",
            "보통",
            working_emails=[],
            team_to_emails=TEAM_TO_EMAILS,
            email_to_bug_count={"shc@team-mono.com": 3, "kbc@team-mono.com": 1},
        )

        assert assignee_email == "kbc@team-mono.com"

    def test_ignores_working_state_when_not_urgent(self):
        """긴급이 아니면 출근 여부와 무관하게 후보를 유지한다"""
        reason_text, assignee_email = select_assignee_email(
            ["peko@team-mono.com"],
            "코들 담당 be팀 영역.",
            "보통",
            working_emails=["ksy@team-mono.com"],
            team_to_emails=TEAM_TO_EMAILS,
            email_to_bug_count={},
        )

        assert assignee_email == "peko@team-mono.com"
        assert "긴급 아니므로" in reason_text

    def test_prefers_working_candidate_when_urgent(self):
        """긴급이면 후보 중 출근한 사람을 우선한다"""
        _, assignee_email = select_assignee_email(
            TEAM_TO_EMAILS["fe"],
            "코들 담당 fe팀 영역.",
            "긴급",
            working_emails=["kbc@team-mono.com"],
            team_to_emails=TEAM_TO_EMAILS,
            email_to_bug_count={"kbc@team-mono.com": 5},
        )

        assert assignee_email == "kbc@team-mono.com"

    def test_widens_to_other_teams_when_no_candidate_works(self):
        """긴급인데 후보가 모두 결근이면 다른 팀 출근자로 넓힌다"""
        _, assignee_email = select_assignee_email(
            TEAM_TO_EMAILS["fe"],
            "코들 담당 fe팀 영역.",
            "긴급",
            working_emails=["byb@team-mono.com"],
            team_to_emails=TEAM_TO_EMAILS,
            email_to_bug_count={},
        )

        assert assignee_email == "byb@team-mono.com"

    def test_falls_back_to_cto_when_no_candidate(self):
        """후보가 아예 없으면 CTO가 받는다"""
        _, assignee_email = select_assignee_email(
            [],
            "코들 담당 be팀 영역.",
            "보통",
            working_emails=[],
            team_to_emails=TEAM_TO_EMAILS,
            email_to_bug_count={},
        )

        assert assignee_email == "lch@team-mono.com"
