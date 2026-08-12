"""pytest 설정 파일"""

import os
import sys

import pytest
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 테스트 환경에서 필요한 환경 변수 기본값 설정
# (모듈 수준에서 초기화되는 외부 서비스 클라이언트용)
os.environ.setdefault("TAVILY_API_KEY", "test-dummy-key")


@pytest.fixture(autouse=True)
def _sample_template(request, tmp_path, monkeypatch):
    """테스트용 문안 파일.

    실제 사업 문안을 레포에 두지 않습니다. 특정 사업 1회성 공지가 코드베이스에
    남으면 썩고, 실제로 그 문안 안의 담당자 번호가 도달 확인 파서를 오작동시킨
    적이 있습니다.

    문자 테스트에만 겁니다. 레포 전체에 걸면 SMS 계층의 import 하나가 무관한
    테스트 25개 파일을 빨갛게 만들고, 원인 파일에 없던 실패가 전역에서 뜹니다.
    """
    if "sms" not in request.node.nodeid and "send_sms" not in request.node.nodeid:
        return

    from service.sms import templates

    directory = tmp_path / "sms"
    directory.mkdir()
    (directory / "discord.txt").write_text(
        "[*이름*]선생님께 안내드립니다.\n- 기수 : [*1*]\n- 참여 링크 : [*2*]",
        encoding="utf-8",
    )
    monkeypatch.setattr(templates, "TEMPLATE_DIR", directory)
