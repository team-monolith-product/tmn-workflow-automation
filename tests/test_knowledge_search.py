"""지식베이스 어휘 검색 테스트"""

from app.knowledge import render_results
from service.knowledge.search import build_snippet, to_like_pattern


def test_질의는_소문자로_접힌다():
    assert to_like_pattern("Sidekiq") == "%sidekiq%"


def test_와일드카드는_이스케이프한다():
    assert to_like_pattern("50%") == "%50\\%%"
    assert to_like_pattern("sg_09dd") == "%sg\\_09dd%"


def test_역슬래시를_먼저_이스케이프한다():
    # 나중에 하면 %를 이스케이프하며 넣은 역슬래시를 또 이스케이프한다.
    assert to_like_pattern("a\\b") == "%a\\\\b%"


def test_짧은_원문은_통째로_돌려준다():
    assert build_snippet("배포가 실패했습니다", "배포") == "배포가 실패했습니다"


def test_맞은_자리_주변만_잘라낸다():
    raw_text = "머리" * 200 + "핵심어" + "꼬리" * 200
    snippet = build_snippet(raw_text, "핵심어")
    assert "핵심어" in snippet
    assert len(snippet) < len(raw_text)
    assert snippet.startswith("…")
    assert snippet.endswith("…")


def test_대소문자가_달라도_자리를_찾는다():
    assert "Sidekiq" in build_snippet("어제 Sidekiq 큐가 밀렸다", "sidekiq")


def test_줄바꿈은_공백으로_바꾼다():
    assert "\n" not in build_snippet("첫 줄\n둘째 줄", "첫")


def test_결과가_없으면_없다고_말한다():
    assert render_results([]) == "검색 결과가 없습니다."


def test_결과에_출처와_링크가_들어간다():
    rendered = render_results(
        [
            {
                "title": "class-rails 비정상 종료",
                "url": "https://example.slack.com/archives/C1/p1",
                "author": "lch@team-mono.com",
                "channel": "t_개발_백",
                "created_at": "2025-11-11",
                "snippet": "OOM으로 보입니다",
            }
        ]
    )
    assert "[t_개발_백]" in rendered
    assert "https://example.slack.com/archives/C1/p1" in rendered
    assert "lch@team-mono.com · 2025-11-11" in rendered
