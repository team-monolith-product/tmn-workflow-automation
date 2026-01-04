"""
분기별 Notion 프로젝트 마이그레이션 스크립트

Usage:
    # DRY RUN (기본값)
    python migrate_quarterly_projects.py --from-quarter 25Y4Q --to-quarter 26Y1Q

    # 실제 실행
    python migrate_quarterly_projects.py --from-quarter 25Y4Q --to-quarter 26Y1Q --execute

    # 롤백
    python migrate_quarterly_projects.py --rollback backup_20260104_123456.json
"""
import os
import json
import argparse
from datetime import datetime
from typing import Any
from dotenv import load_dotenv
from notion_client import Client as NotionClient

# 상수
PROJECT_DATABASE_ID = "9df81e8ee45e4f49aceb402c084b3ac7"  # 프로젝트 데이터베이스 ID
PROJECT_DATA_SOURCE_ID = "1023943f-84d1-4223-a5a6-0c26e22d09f0"  # 프로젝트 데이터 소스 ID
TASK_DATABASE_ID = "a9de18b3877c453a8e163c2ee1ff4137"  # 작업 데이터베이스 ID
TASK_DATA_SOURCE_ID = "3e050c5a-11f3-4a3e-b6d0-498fe06c9d7b"  # 작업 데이터 소스 ID


def find_quarter_projects(
    notion: NotionClient,
    data_source_id: str,
    quarter: str,
    categories: list[str] | None = None,
) -> list[dict]:
    """
    특정 분기의 프로젝트들을 조회

    Args:
        notion: NotionClient
        data_source_id: 프로젝트 데이터 소스 ID
        quarter: 분기 문자열 (예: "25Y4Q")
        categories: 필터링할 카테고리 리스트 (예: ["경험 개선", "기술 개선"])

    Returns:
        프로젝트 페이지 목록
    """
    projects = []
    has_more = True
    start_cursor = None

    # 페이지네이션 처리
    while has_more:
        query_params = {"data_source_id": data_source_id}
        if start_cursor:
            query_params["start_cursor"] = start_cursor

        results = notion.data_sources.query(**query_params)

        for page in results.get("results", []):
            title_prop = page["properties"].get("프로젝트 이름", {}).get("title", [])
            if not title_prop:
                continue

            project_name = title_prop[0]["text"]["content"]

            # 분기 매칭
            if quarter not in project_name:
                continue

            # 카테고리 필터링
            if categories:
                matched = False
                for category in categories:
                    if project_name.startswith(category):
                        matched = True
                        break
                if not matched:
                    continue

            projects.append(page)
            print(f"✓ 찾은 프로젝트: {project_name} (ID: {page['id']})")

        has_more = results.get("has_more", False)
        start_cursor = results.get("next_cursor")

    return projects


def get_tasks_for_projects(
    notion: NotionClient,
    data_source_id: str,
    project_ids: list[str],
) -> list[dict]:
    """
    주어진 프로젝트들에 연결된 모든 작업 조회 (페이지네이션 지원)
    """
    all_tasks = []
    seen_task_ids = set()

    for project_id in project_ids:
        has_more = True
        start_cursor = None

        # 페이지네이션 처리
        while has_more:
            query_params = {
                "data_source_id": data_source_id,
                "filter": {
                    "property": "프로젝트",
                    "relation": {"contains": project_id},
                },
            }
            if start_cursor:
                query_params["start_cursor"] = start_cursor

            results = notion.data_sources.query(**query_params)

            for task in results.get("results", []):
                if task["id"] not in seen_task_ids:
                    all_tasks.append(task)
                    seen_task_ids.add(task["id"])

            has_more = results.get("has_more", False)
            start_cursor = results.get("next_cursor")

    return all_tasks


def backup_task_relations(
    notion: NotionClient,
    data_source_id: str,
    project_ids: list[str],
    backup_file: str,
) -> dict:
    """
    작업-프로젝트 연결관계를 JSON으로 백업

    Returns:
        백업 데이터 딕셔너리
    """
    print("\n" + "=" * 60)
    print("백업 생성 중...")
    print("=" * 60)

    tasks = get_tasks_for_projects(notion, data_source_id, project_ids)

    backup_data = {
        "timestamp": datetime.now().isoformat(),
        "project_ids": project_ids,
        "tasks": []
    }

    for task in tasks:
        title_prop = task["properties"]["제목"]["title"]
        task_title = title_prop[0]["text"]["content"] if title_prop else "제목 없음"

        status_prop = task["properties"]["상태"]["status"]
        status = status_prop["name"] if status_prop else None

        project_relations = task["properties"]["프로젝트"]["relation"]
        project_ids_list = [rel["id"] for rel in project_relations]

        task_data = {
            "id": task["id"],
            "title": task_title,
            "status": status,
            "project_relations": project_ids_list
        }

        backup_data["tasks"].append(task_data)

    # 백업 파일 저장
    with open(backup_file, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, ensure_ascii=False, indent=2)

    print(f"✓ 백업 완료: {backup_file}")
    print(f"  - 작업 수: {len(backup_data['tasks'])}개")

    return backup_data


def restore_task_relations(notion: NotionClient, backup_file: str):
    """
    백업 파일로부터 작업-프로젝트 연결관계 복원
    """
    print("\n" + "=" * 60)
    print(f"롤백 시작: {backup_file}")
    print("=" * 60)

    # 백업 파일 로드
    with open(backup_file, "r", encoding="utf-8") as f:
        backup_data = json.load(f)

    print(f"백업 시간: {backup_data['timestamp']}")
    print(f"복원할 작업 수: {len(backup_data['tasks'])}개\n")

    # 각 작업의 프로젝트 관계 복원
    for idx, task_data in enumerate(backup_data["tasks"], 1):
        task_id = task_data["id"]
        task_title = task_data["title"]
        original_relations = [{"id": pid} for pid in task_data["project_relations"]]

        print(f"[{idx}/{len(backup_data['tasks'])}] 복원 중: {task_title}")

        try:
            notion.pages.update(
                page_id=task_id,
                properties={
                    "프로젝트": {"relation": original_relations}
                }
            )
            print(f"  ✓ 복원 완료")
        except Exception as e:
            print(f"  ✗ 오류: {e}")

    print("\n" + "=" * 60)
    print("롤백 완료!")
    print("=" * 60)


def duplicate_project(
    notion: NotionClient,
    original_project: dict,
    new_quarter: str,
    project_database_id: str,
    dry_run: bool = False,
) -> dict | None:
    """
    프로젝트를 복제하여 새 분기 프로젝트 생성
    """
    original_props = original_project["properties"]
    original_title = original_props["프로젝트 이름"]["title"][0]["text"]["content"]

    # 새 프로젝트 이름 생성
    category = original_title.rsplit(" ", 1)[0]
    new_title = f"{category} {new_quarter}"

    if dry_run:
        print(f"  [DRY RUN] 프로젝트 생성: {new_title}")
        return None

    # 새 프로젝트 생성
    new_properties = {
        "프로젝트 이름": {"title": [{"text": {"content": new_title}}]},
        "상태": {"status": {"name": "진행 중"}},
    }

    # 페이지 생성 시에는 database_id 사용 (data_source_id 아님!)
    response = notion.pages.create(
        parent={"database_id": project_database_id},
        properties=new_properties,
    )

    print(f"  ✓ 프로젝트 생성 완료: {new_title} (ID: {response['id']})")
    return response


def calculate_task_relation_updates(
    tasks: list[dict],
    old_project_id: str,
    new_project_id: str,
) -> list[dict]:
    """
    작업 상태에 따라 프로젝트 관계 업데이트 계획 생성

    작업이 구 프로젝트에만 연결되어 있다고 가정하고,
    상태에 따라 최종 프로젝트 관계를 결정합니다.

    Returns:
        업데이트할 작업 목록 [{"task_id": ..., "new_relations": [...], "reason": ...}]
    """
    updates = []

    for task in tasks:
        title_prop = task["properties"]["제목"]["title"]
        task_title = title_prop[0]["text"]["content"] if title_prop else "제목 없음"

        status_prop = task["properties"]["상태"]["status"]
        status = status_prop["name"] if status_prop else None

        # 현재 프로젝트 관계
        current_relations = task["properties"]["프로젝트"]["relation"]
        current_project_ids = [rel["id"] for rel in current_relations]

        # 이 작업이 구 프로젝트에 연결되어 있지 않으면 스킵
        if old_project_id not in current_project_ids:
            continue

        # 구/신 프로젝트 제외한 다른 프로젝트 관계 유지
        other_relations = [rel for rel in current_relations
                          if rel["id"] not in [old_project_id, new_project_id]]

        new_relations = []
        reason = ""

        # 상태별 최종 프로젝트 관계 결정
        if status in ["중단", "완료"]:
            # 구 프로젝트만 유지
            new_relations = other_relations + [{"id": old_project_id}]
            reason = f"구 프로젝트만 유지 (상태: {status})"

        elif status in ["진행", "리뷰"]:
            # 양쪽 프로젝트 모두 유지
            new_relations = other_relations + [{"id": old_project_id}, {"id": new_project_id}]
            reason = f"양쪽 프로젝트 유지 (상태: {status})"

        elif status == "대기" or status is None:
            # 신 프로젝트만 유지
            new_relations = other_relations + [{"id": new_project_id}]
            if status is None:
                reason = "신 프로젝트만 유지 (상태 없음 -> 대기로 취급)"
            else:
                reason = f"신 프로젝트만 유지 (상태: {status})"

        else:
            # 알 수 없는 상태 - 안전하게 양쪽 유지
            print(f"  경고: 알 수 없는 상태 '{status}' - 작업: {task_title} (양쪽 프로젝트 유지)")
            new_relations = other_relations + [{"id": old_project_id}, {"id": new_project_id}]
            reason = f"양쪽 프로젝트 유지 (알 수 없는 상태: {status})"

        updates.append({
            "task_id": task["id"],
            "task_title": task_title,
            "new_relations": new_relations,
            "reason": reason,
        })

    return updates


def apply_task_relation_updates(
    notion: NotionClient,
    updates: list[dict],
    dry_run: bool = False,
):
    """
    작업 관계 업데이트 적용
    """
    for idx, update in enumerate(updates, 1):
        task_id = update["task_id"]
        task_title = update["task_title"]
        new_relations = update["new_relations"]
        reason = update["reason"]

        if dry_run:
            print(f"  [{idx}/{len(updates)}] [DRY RUN] {task_title}")
            print(f"      → {reason}")
        else:
            print(f"  [{idx}/{len(updates)}] 업데이트: {task_title}")
            print(f"      → {reason}")

            try:
                notion.pages.update(
                    page_id=task_id,
                    properties={
                        "프로젝트": {"relation": new_relations}
                    }
                )
            except Exception as e:
                print(f"      ✗ 오류: {e}")


def update_project_status(
    notion: NotionClient,
    project_id: str,
    project_title: str,
    status: str,
    dry_run: bool = False,
):
    """
    프로젝트 상태 업데이트
    """
    if dry_run:
        print(f"  [DRY RUN] {project_title} → 상태: {status}")
        return

    notion.pages.update(
        page_id=project_id,
        properties={
            "상태": {"status": {"name": status}},
        },
    )
    print(f"  ✓ {project_title} → 상태: {status}")


def migrate_quarterly_projects(
    notion: NotionClient,
    from_quarter: str,
    to_quarter: str,
    project_data_source_id: str,
    task_data_source_id: str,
    categories: list[str] | None = None,
    dry_run: bool = True,
):
    """
    분기별 프로젝트 마이그레이션 메인 로직
    """
    print("\n" + "=" * 60)
    print(f"분기별 프로젝트 마이그레이션: {from_quarter} → {to_quarter}")
    if dry_run:
        print(">>> DRY RUN MODE - 실제 변경 없음 <<<")
    else:
        print(">>> EXECUTE MODE - 실제 변경 수행 <<<")
    print("=" * 60)

    # Step 1: 구 분기 프로젝트 조회
    print(f"\nStep 1: {from_quarter} 프로젝트 조회...")
    old_projects = find_quarter_projects(
        notion, project_data_source_id, from_quarter, categories
    )

    if not old_projects:
        print(f"✗ 오류: {from_quarter} 프로젝트를 찾을 수 없습니다.")
        return

    print(f"  → 발견: {len(old_projects)}개 프로젝트")

    # Step 2: 백업 생성
    old_project_ids = [p["id"] for p in old_projects]
    backup_filename = f"backup_{from_quarter}_to_{to_quarter}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    if not dry_run:
        backup_task_relations(
            notion, task_data_source_id, old_project_ids, backup_filename
        )
    else:
        print(f"\n[DRY RUN] 백업 파일 생성 스킵: {backup_filename}")

    # Step 3: 새 분기 프로젝트 확인 및 생성
    print(f"\nStep 3: {to_quarter} 프로젝트 확인 및 생성...")

    # 먼저 새 분기 프로젝트가 이미 존재하는지 확인
    existing_new_projects = find_quarter_projects(
        notion, project_data_source_id, to_quarter, categories
    )

    # 기존 프로젝트를 카테고리별로 인덱싱
    existing_projects_by_name = {}
    for proj in existing_new_projects:
        title = proj["properties"]["프로젝트 이름"]["title"][0]["text"]["content"]
        existing_projects_by_name[title] = proj

    new_projects = []
    project_mapping = {}
    created_count = 0
    reused_count = 0

    # database_id 추출 (URL에서 또는 상수에서)
    project_database_id = PROJECT_DATABASE_ID

    for old_project in old_projects:
        old_title = old_project["properties"]["프로젝트 이름"]["title"][0]["text"]["content"]
        category = old_title.rsplit(" ", 1)[0]
        new_title = f"{category} {to_quarter}"

        # 이미 존재하는 프로젝트 확인
        if new_title in existing_projects_by_name:
            existing_project = existing_projects_by_name[new_title]
            print(f"  ✓ 기존 프로젝트 사용: {new_title} (ID: {existing_project['id']})")
            project_mapping[old_project["id"]] = existing_project["id"]
            new_projects.append(existing_project)
            reused_count += 1
        else:
            # 존재하지 않으면 새로 생성
            new_project = duplicate_project(
                notion, old_project, to_quarter, project_database_id, dry_run
            )
            if new_project:
                new_projects.append(new_project)
                project_mapping[old_project["id"]] = new_project["id"]
                created_count += 1
            else:
                # Dry run 모드에서는 가상 ID 사용
                project_mapping[old_project["id"]] = f"dry-run-new-id-{old_project['id']}"
                created_count += 1

    print(f"  → 총 {len(project_mapping)}개 프로젝트 (기존: {reused_count}개, 신규 생성: {created_count}개)")

    # Step 4: 연결된 작업 조회
    print(f"\nStep 4: 프로젝트에 연결된 작업 조회...")
    query_project_ids = list(project_mapping.keys())
    if not dry_run:
        query_project_ids.extend(list(project_mapping.values()))

    tasks = get_tasks_for_projects(notion, task_data_source_id, query_project_ids)
    print(f"  → 발견: {len(tasks)}개 작업")

    # Step 5: 작업 관계 업데이트 계획
    print(f"\nStep 5: 작업 관계 업데이트 계획 생성...")
    all_updates = []
    for old_id, new_id in project_mapping.items():
        updates = calculate_task_relation_updates(tasks, old_id, new_id)
        all_updates.extend(updates)

    print(f"  → 업데이트 대상: {len(all_updates)}개 작업")

    # Step 6: 작업 관계 업데이트 적용
    if all_updates:
        print(f"\nStep 6: 작업 관계 업데이트 적용...")
        apply_task_relation_updates(notion, all_updates, dry_run)

    # Step 7: 구 분기 프로젝트 상태 업데이트
    print(f"\nStep 7: {from_quarter} 프로젝트 상태 업데이트 → '완료'")
    for old_project in old_projects:
        title = old_project["properties"]["프로젝트 이름"]["title"][0]["text"]["content"]
        update_project_status(notion, old_project["id"], title, "완료", dry_run)

    # Step 8: 수동 작업 안내
    print("\n" + "=" * 60)
    print("수동 작업 안내")
    print("=" * 60)
    print("Notion API는 데이터베이스 뷰 설정 변경을 지원하지 않습니다.")
    print(f"구 분기 프로젝트({from_quarter})를 숨기려면 수동으로 처리해야 합니다:")
    print()
    print("1. Notion에서 프로젝트 데이터베이스 열기")
    print("   https://www.notion.so/team-mono/9df81e8ee45e4f49aceb402c084b3ac7")
    print("2. 원하는 뷰 선택")
    print("3. 필터 설정:")
    print("   - '상태' does not equal '완료'")
    print(f"   - 또는 '프로젝트 이름' does not contain '{from_quarter}'")
    print("=" * 60)

    if not dry_run:
        print(f"\n✓ 백업 파일: {backup_filename}")
        print(f"  롤백 명령어: python migrate_quarterly_projects.py --rollback {backup_filename}")

    print("\n마이그레이션 완료!")


def main():
    """메인 함수"""
    parser = argparse.ArgumentParser(
        description="분기별 Notion 프로젝트 마이그레이션 스크립트"
    )

    # 마이그레이션 모드
    parser.add_argument(
        "--from-quarter",
        help="원본 분기 (예: 25Y4Q)",
    )
    parser.add_argument(
        "--to-quarter",
        help="대상 분기 (예: 26Y1Q)",
    )
    parser.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="처리할 프로젝트 카테고리 (예: '경험 개선' '기술 개선')",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제 변경 수행 (기본값: DRY RUN)",
    )

    # 롤백 모드
    parser.add_argument(
        "--rollback",
        help="백업 파일로부터 롤백",
    )

    args = parser.parse_args()

    # 환경 변수 로드
    load_dotenv()

    # Notion 클라이언트 초기화
    notion = NotionClient(
        auth=os.environ.get("NOTION_TOKEN"),
        notion_version="2025-09-03",
    )

    # 롤백 모드
    if args.rollback:
        restore_task_relations(notion, args.rollback)
        return

    # 마이그레이션 모드
    if not args.from_quarter or not args.to_quarter:
        parser.error("--from-quarter와 --to-quarter가 필요합니다")

    migrate_quarterly_projects(
        notion=notion,
        from_quarter=args.from_quarter,
        to_quarter=args.to_quarter,
        project_data_source_id=PROJECT_DATA_SOURCE_ID,
        task_data_source_id=TASK_DATA_SOURCE_ID,
        categories=args.categories,
        dry_run=not args.execute,
    )


if __name__ == "__main__":
    main()
