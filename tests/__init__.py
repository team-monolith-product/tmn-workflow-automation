"""테스트 패키지.

pytest 9 의 importlib 임포트 모드에서는 sys.path 에 tests/ 가 실리지 않습니다.
이 파일이 없으면 tests 가 정식 패키지가 아니라, 다른 테스트 모듈이 먼저
임포트되는 순간 sys.modules["tests"] 가 __path__ 없는 더미로 굳어
`from tests.<모듈> import ...` 가 수집 단계에서 죽습니다.
"""
