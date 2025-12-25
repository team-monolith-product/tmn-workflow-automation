"""
차트 도구 테스트
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.tools.chart_tools import get_execute_python_with_chart_tool


@pytest.mark.asyncio
async def test_execute_python_with_chart_success_with_chart():
    """
    차트를 생성하는 파이썬 코드가 성공적으로 실행되고 슬랙에 업로드되는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.files_upload_v2 = AsyncMock()
    mock_say = AsyncMock()

    # 도구 생성
    tool = get_execute_python_with_chart_tool(
        say=mock_say,
        thread_ts="1234567890.123456",
        slack_client=mock_slack_client,
        channel="C123456",
    )

    # 테스트용 파이썬 코드 (matplotlib 차트 생성)
    test_code = """
import matplotlib.pyplot as plt

x = [1, 2, 3, 4, 5]
y = [2, 4, 6, 8, 10]

plt.figure(figsize=(8, 6))
plt.plot(x, y, marker='o')
plt.xlabel('X axis')
plt.ylabel('Y axis')
plt.title('Test Chart')
print("Chart created successfully!")
"""

    # 도구 실행
    result = await tool.ainvoke({"code": test_code})

    # 검증
    assert "✅ 코드 실행 성공: 차트를 슬랙에 업로드했습니다." in result
    assert "Chart created successfully!" in result

    # 슬랙 업로드가 호출되었는지 확인
    mock_slack_client.files_upload_v2.assert_called_once()
    call_kwargs = mock_slack_client.files_upload_v2.call_args.kwargs
    assert call_kwargs["channel"] == "C123456"
    assert call_kwargs["thread_ts"] == "1234567890.123456"
    assert call_kwargs["filename"] == "chart.png"


@pytest.mark.asyncio
async def test_execute_python_with_chart_success_without_chart():
    """
    차트를 생성하지 않는 파이썬 코드가 성공적으로 실행되는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.files_upload_v2 = AsyncMock()
    mock_say = AsyncMock()

    # 도구 생성
    tool = get_execute_python_with_chart_tool(
        say=mock_say,
        thread_ts="1234567890.123456",
        slack_client=mock_slack_client,
        channel="C123456",
    )

    # 테스트용 파이썬 코드 (차트 없음)
    test_code = """
x = 10
y = 20
result = x + y
print(f"Result: {result}")
"""

    # 도구 실행
    result = await tool.ainvoke({"code": test_code})

    # 검증
    assert "✅ 코드 실행 성공" in result
    assert "Result: 30" in result

    # 슬랙 업로드가 호출되지 않았는지 확인
    mock_slack_client.files_upload_v2.assert_not_called()


@pytest.mark.asyncio
async def test_execute_python_with_chart_failure():
    """
    파이썬 코드 실행 실패 시 스택트레이스를 반환하는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_say = AsyncMock()

    # 도구 생성
    tool = get_execute_python_with_chart_tool(
        say=mock_say,
        thread_ts="1234567890.123456",
        slack_client=mock_slack_client,
        channel="C123456",
    )

    # 테스트용 파이썬 코드 (에러 발생)
    test_code = """
x = 10
y = 0
result = x / y  # Division by zero
"""

    # 도구 실행
    result = await tool.ainvoke({"code": test_code})

    # 검증
    assert "❌ 코드 실행 실패:" in result
    assert "ZeroDivisionError" in result
    assert "division by zero" in result.lower()


@pytest.mark.asyncio
async def test_execute_python_with_chart_with_athena_mock():
    """
    athena 함수를 사용하는 코드가 성공적으로 실행되는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.files_upload_v2 = AsyncMock()
    mock_say = AsyncMock()

    # Mock athena.execute_and_wait
    mock_athena_result = {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": "date"}, {"VarCharValue": "count"}]},
                {"Data": [{"VarCharValue": "2024-01-01"}, {"VarCharValue": "100"}]},
                {"Data": [{"VarCharValue": "2024-01-02"}, {"VarCharValue": "150"}]},
                {"Data": [{"VarCharValue": "2024-01-03"}, {"VarCharValue": "200"}]},
            ]
        }
    }

    with patch("api.athena.execute_and_wait", return_value=mock_athena_result):
        # 도구 생성
        tool = get_execute_python_with_chart_tool(
            say=mock_say,
            thread_ts="1234567890.123456",
            slack_client=mock_slack_client,
            channel="C123456",
        )

        # 테스트용 파이썬 코드 (athena 사용)
        test_code = """
import matplotlib.pyplot as plt

# Athena에서 데이터 가져오기
results = execute_athena_query(
    "SELECT date, count FROM daily_stats ORDER BY date",
    database="test_db"
)

# 결과에서 데이터 추출
rows = results["ResultSet"]["Rows"]
headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]
data_rows = [[col.get("VarCharValue", "") for col in row["Data"]] for row in rows[1:]]

# 차트 그리기
dates = [row[0] for row in data_rows]
counts = [int(row[1]) for row in data_rows]

plt.figure(figsize=(10, 6))
plt.plot(dates, counts, marker='o')
plt.xlabel('Date')
plt.ylabel('Count')
plt.title('Daily Stats')
plt.xticks(rotation=45)
plt.tight_layout()

print(f"Processed {len(data_rows)} rows")
"""

        # 도구 실행
        result = await tool.ainvoke({"code": test_code})

        # 검증
        assert "✅ 코드 실행 성공: 차트를 슬랙에 업로드했습니다." in result
        assert "Processed 3 rows" in result

        # 슬랙 업로드가 호출되었는지 확인
        mock_slack_client.files_upload_v2.assert_called_once()
