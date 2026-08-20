"""pytest 설정 파일"""

import os
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 테스트 환경에서 필요한 환경 변수 기본값 설정
# (모듈 수준에서 초기화되는 외부 서비스 클라이언트용)
os.environ.setdefault("TAVILY_API_KEY", "test-dummy-key")
