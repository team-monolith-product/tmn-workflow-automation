"""
GitHub Organization 관리 스크립트 모음

이 패키지는 GitHub Organization의 리포지토리를 일괄 관리하는 스크립트를 제공합니다.

스크립트 목록:
- add_code_owners.py: 모든 리포지토리에 CODEOWNERS 파일 추가
- add_ruleset.py: 모든 리포지토리에 Branch Protection Ruleset 적용
- add_team.py: 모든 리포지토리에 특정 팀 추가
- auto_delete_head_branches.py: PR 병합 시 head 브랜치 자동 삭제 설정

사용 전 필수 환경변수:
- GITHUB_TOKEN: GitHub Personal Access Token
- GITHUB_ORG_NAME: 대상 Organization 이름

모든 스크립트는 --dry-run 옵션을 지원합니다.
"""
