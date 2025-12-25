"""
Integration 테스트 - 실제 API와 통신
"""

import pytest
from dotenv import load_dotenv
from api import redash, athena

# 환경 변수 로드
load_dotenv()


class TestRedashIntegration:
    """Redash API Integration 테스트"""

    def test_list_dashboards(self):
        """대시보드 목록 조회 integration 테스트"""
        try:
            result = redash.list_dashboards()
            print(f"\n대시보드 목록 조회 성공!")
            print(f"결과 키: {result.keys()}")

            if "results" in result:
                print(f"대시보드 개수: {len(result['results'])}")
                if result["results"]:
                    first_dashboard = result["results"][0]
                    print(f"첫 번째 대시보드: {first_dashboard.get('name', 'N/A')}")

            assert "results" in result
            print("✓ 대시보드 목록 조회 성공")
        except Exception as e:
            print(f"✗ 대시보드 목록 조회 실패: {str(e)}")
            raise

    def test_search_queries(self):
        """쿼리 검색 integration 테스트"""
        try:
            # 빈 검색어로 전체 쿼리 조회
            result = redash.search_queries(query="", page=1, page_size=5)
            print(f"\n쿼리 검색 성공!")
            print(f"결과 키: {result.keys()}")

            if "results" in result:
                print(f"쿼리 개수: {len(result['results'])}")
                if result["results"]:
                    first_query = result["results"][0]
                    print(f"첫 번째 쿼리 ID: {first_query.get('id', 'N/A')}")
                    print(f"첫 번째 쿼리 이름: {first_query.get('name', 'N/A')}")

            assert "results" in result
            print("✓ 쿼리 검색 성공")
        except Exception as e:
            print(f"✗ 쿼리 검색 실패: {str(e)}")
            raise


class TestAthenaIntegration:
    """Athena API Integration 테스트"""

    def test_get_client(self):
        """Athena 클라이언트 생성 integration 테스트"""
        try:
            client = athena.get_athena_client()
            print(f"\nAthena 클라이언트 생성 성공!")
            print(f"클라이언트 타입: {type(client)}")
            print(f"서비스 이름: {client._service_model.service_name}")

            assert client is not None
            assert client._service_model.service_name == "athena"
            print("✓ Athena 클라이언트 생성 성공")
        except Exception as e:
            print(f"✗ Athena 클라이언트 생성 실패: {str(e)}")
            raise

    @pytest.mark.skip(reason="실제 쿼리 실행은 비용이 발생할 수 있으므로 기본적으로 스킵")
    def test_execute_simple_query(self):
        """간단한 쿼리 실행 integration 테스트 (스킵됨)"""
        try:
            # 매우 간단한 쿼리로 테스트
            query = "SELECT 1 as test_value"
            database = "default"  # 기본 데이터베이스 사용

            print(f"\n쿼리 실행 시작: {query}")
            result = athena.execute_and_wait(query, database, max_wait_seconds=30)

            print(f"쿼리 실행 완료!")
            print(f"결과 키: {result.keys()}")

            assert "ResultSet" in result
            print("✓ 쿼리 실행 성공")
        except Exception as e:
            print(f"✗ 쿼리 실행 실패: {str(e)}")
            raise


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
