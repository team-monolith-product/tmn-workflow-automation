"""
차트 도구 테스트
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.tools.python_tools import get_execute_python_tool


@pytest.mark.asyncio
async def test_execute_python_success_with_chart():
    """
    차트를 생성하는 파이썬 코드가 성공적으로 실행되고 슬랙에 업로드되는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.files_upload_v2 = AsyncMock()

    # 도구 생성
    tool = get_execute_python_tool(
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
async def test_execute_python_success_without_chart():
    """
    차트를 생성하지 않는 파이썬 코드가 성공적으로 실행되는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.files_upload_v2 = AsyncMock()

    # 도구 생성
    tool = get_execute_python_tool(
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
async def test_execute_python_failure():
    """
    파이썬 코드 실행 실패 시 스택트레이스를 반환하는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()

    # 도구 생성
    tool = get_execute_python_tool(
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
async def test_execute_python_with_athena_mock():
    """
    athena 함수를 사용하는 코드가 성공적으로 실행되는지 테스트
    """
    # Mock Slack client
    mock_slack_client = AsyncMock()
    mock_slack_client.files_upload_v2 = AsyncMock()

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

    with patch("api.athena.execute_and_wait", new_callable=AsyncMock) as mock_execute:
        mock_execute.return_value = mock_athena_result

        # 도구 생성
        tool = get_execute_python_tool(
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


@pytest.mark.asyncio
async def test_코드가_정의한_함수가_상위_이름을_본다():
    # exec 에 locals 를 따로 주면 함수 본문이 상위 import 를 못 찾는다.
    # 하이픈 지우는 함수를 apply 로 넘기는 일이 흔해서 바로 터진다.
    tool = get_execute_python_tool()

    result = await tool.ainvoke(
        {
            "code": (
                "import re\n"
                "def 숫자만(값):\n"
                "    return re.sub(r'\\D', '', 값)\n"
                "print(숫자만('010-1111-2222'))\n"
            )
        }
    )

    assert "01011112222" in result
    assert "NameError" not in result


def test_draft_sms_를_주입해야_설명에_나온다():
    # 없는 함수를 알려 주면 코드가 부르고 NameError 로 끝난다.
    열린도구 = get_execute_python_tool(draft_sms=AsyncMock())
    닫힌도구 = get_execute_python_tool()

    assert "draft_sms" in 열린도구.description
    assert "draft_sms" not in 닫힌도구.description


def _슬랙에_올리는_도구():
    slack_client = AsyncMock()
    slack_client.files_upload_v2 = AsyncMock()
    tool = get_execute_python_tool(
        thread_ts="1234567890.123456",
        slack_client=slack_client,
        channel="C123456",
    )
    return tool, slack_client


@pytest.mark.asyncio
async def test_upload_file_은_만든_파일을_스레드에_첨부한다():
    tool, slack_client = _슬랙에_올리는_도구()

    result = await tool.ainvoke(
        {
            "code": (
                "import io, zipfile\n"
                "buf = io.BytesIO()\n"
                "with zipfile.ZipFile(buf, 'w') as zf:\n"
                "    zf.writestr('팀A.csv', 'a,b\\n1,2\\n')\n"
                "upload_file('teams.zip', buf.getvalue(), title='팀별 대화')\n"
                "upload_file('summary.csv', '팀,건수\\nA,1\\n')\n"
            )
        }
    )

    assert "teams.zip, summary.csv" in result
    slack_client.files_upload_v2.assert_called_once()
    call_kwargs = slack_client.files_upload_v2.call_args.kwargs
    assert call_kwargs["channel"] == "C123456"
    assert call_kwargs["thread_ts"] == "1234567890.123456"
    uploads = call_kwargs["file_uploads"]
    assert [u["filename"] for u in uploads] == ["teams.zip", "summary.csv"]
    assert uploads[0]["title"] == "팀별 대화"
    assert uploads[0]["file"].startswith(b"PK")
    assert uploads[1]["file"] == "팀,건수\nA,1\n".encode("utf-8")


@pytest.mark.asyncio
async def test_upload_file_은_경로만_주면_디스크에서_읽는다(tmp_path):
    path = tmp_path / "result.csv"
    path.write_bytes(b"x,y\n")
    tool, slack_client = _슬랙에_올리는_도구()

    await tool.ainvoke({"code": f"upload_file({str(path)!r})"})

    uploads = slack_client.files_upload_v2.call_args.kwargs["file_uploads"]
    assert uploads[0]["filename"] == "result.csv"
    assert uploads[0]["file"] == b"x,y\n"


@pytest.mark.asyncio
async def test_코드가_실패하면_파일을_올리지_않는다():
    # 고쳐 부를 때마다 같은 파일이 쌓이지 않게, 끝까지 성공한 실행만 올린다.
    tool, slack_client = _슬랙에_올리는_도구()

    result = await tool.ainvoke(
        {"code": "upload_file('a.csv', 'x')\nraise RuntimeError('boom')\n"}
    )

    assert "❌ 코드 실행 실패" in result
    slack_client.files_upload_v2.assert_not_called()


@pytest.mark.asyncio
async def test_upload_file_은_개수_상한을_넘기면_실패한다():
    tool, slack_client = _슬랙에_올리는_도구()

    result = await tool.ainvoke(
        {"code": "for i in range(11):\n    upload_file(f'{i}.csv', 'x')\n"}
    )

    assert "ZIP 으로 묶으십시오" in result
    slack_client.files_upload_v2.assert_not_called()


def test_슬랙_정보가_있어야_upload_file_이_설명에_나온다():
    열린도구, _ = _슬랙에_올리는_도구()
    닫힌도구 = get_execute_python_tool()

    assert "upload_file" in 열린도구.description
    assert "upload_file" not in 닫힌도구.description
