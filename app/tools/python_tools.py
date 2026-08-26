"""
차트 시각화 관련 LangChain Tools
"""

import asyncio
import functools
import io
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Callable, Any

from langchain_core.tools import tool
import matplotlib

matplotlib.use("Agg")  # GUI 없는 백엔드 사용
import matplotlib.pyplot as plt
import pandas as pd

from api import athena
from service.sheets.read import read_sheet

# 코드가 돌려주는 글자 수 상한. 실행 결과는 컨텍스트로 들어간다.
STDOUT_LIMIT = 4_000

# pyplot은 전역 figure 상태를 쓰므로 코드 실행을 워커 하나로 직렬화한다.
# 시트 읽기(read_sheet)도 이 코드 안에서 도는 이상 같은 큐를 탄다 -- 봇 넷이
# 이 워커 하나를 나눠 쓰므로, 서로 몇 초씩 막히기 시작하면 시트 전용 풀로 가른다.
_code_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chart-exec")


# 한글 폰트 설정
def setup_korean_font():
    """matplotlib에서 한글을 표시하기 위한 폰트 설정"""
    import matplotlib.font_manager as fm

    # 한글 폰트 우선순위 리스트
    korean_fonts = [
        "NanumGothic",
        "NanumBarunGothic",
        "NanumMyeongjo",
        "Malgun Gothic",
        "Apple SD Gothic Neo",
        "AppleGothic",
        "DejaVu Sans",
    ]

    # 사용 가능한 한글 폰트 찾기
    available_fonts = [f.name for f in fm.fontManager.ttflist]
    selected_font = None

    for font in korean_fonts:
        if font in available_fonts:
            selected_font = font
            break

    # 폰트 설정
    if selected_font:
        plt.rcParams["font.family"] = selected_font

    # 마이너스 기호 깨짐 방지
    plt.rcParams["axes.unicode_minus"] = False


# 폰트 설정 초기화
setup_korean_font()


def _run_code(
    code: str, injected: dict[str, Callable[..., Any]]
) -> tuple[str, io.BytesIO | None, str | None]:
    """
    LLM이 만든 코드를 실행하고 결과를 반환합니다.

    matplotlib 접근이 한 스레드 안에서만 일어나도록 figure 저장까지 여기서 마칩니다.

    Args:
        code: 실행할 파이썬 코드
        injected: 코드에 주입할 함수들. 이름이 그대로 코드 안의 이름이 된다

    Returns:
        tuple: (STDOUT 출력, 차트 PNG 버퍼 또는 None, 실패 시 스택트레이스)
    """
    captured_output = io.StringIO()
    exec_globals = {
        **injected,
        "plt": plt,
        "matplotlib": matplotlib,
        "pd": pd,
        # sys.stdout은 프로세스 전역이라 교체하지 않고 print만 가로챈다
        "print": functools.partial(print, file=captured_output),
        "__builtins__": __builtins__,
    }

    try:
        exec(code, exec_globals)

        fig = plt.gcf()
        if not fig.get_axes():
            return captured_output.getvalue(), None, None

        img_buffer = io.BytesIO()
        plt.savefig(img_buffer, format="png", dpi=150, bbox_inches="tight")
        img_buffer.seek(0)
        return captured_output.getvalue(), img_buffer, None
    except Exception:
        return captured_output.getvalue(), None, traceback.format_exc()
    finally:
        plt.close("all")


def get_execute_python_tool(
    thread_ts: str | None = None,
    slack_client: Any | None = None,
    channel: str | None = None,
):
    """
    파이썬 코드를 실행하는 도구를 반환합니다. 차트를 그리면 슬랙으로 전송합니다.

    Args:
        thread_ts: Slack 스레드 타임스탬프
        slack_client: Slack WebClient 인스턴스
        channel: Slack 채널 ID

    Returns:
        execute_python tool
    """

    @tool
    async def execute_python(
        code: Annotated[
            str,
            "실행할 파이썬 코드. read_sheet · execute_athena_query · pd · plt 를 쓸 수 있음",
        ],
    ) -> str:
        """
        파이썬 코드를 실행합니다. 표를 다루는 일은 전부 이 도구로 하십시오.

        구글 시트와 Athena 를 읽어 pandas 로 집계·대조·명단 추출을 하고, 차트를
        그리면 슬랙에 자동으로 올라갑니다. 두 명단을 맞춰 보거나 인원을 세는 일을
        눈으로 하지 마십시오 -- 수십 행만 넘어가도 틀리고, 틀린 것이 티가 안 납니다.

        **사용 가능한 라이브러리 및 함수**:
        코드 컨텍스트 내에서 다음을 사용할 수 있습니다:
        - `pd` (pandas): 데이터 분석 및 조작을 위한 pandas 라이브러리
        - `plt` (matplotlib.pyplot): 차트 시각화를 위한 matplotlib
        - `execute_athena_query(query: str, database: str)`: Athena SQL을 실행하고 결과를 반환합니다.
          결과는 dict 형태이며, "ResultSet" 키에 쿼리 결과가 포함됩니다.
        - `read_sheet(sheet, columns=None, tab=None)`: 구글 시트를 읽어 행 목록(dict)을
          반환합니다. sheet 에는 시트 링크나 **시트 이름 일부**를 넣습니다.
          tab 에는 탭 이름이나 gid 를 넣습니다. 생략하면 첫 번째 탭입니다.
          **여기서는 전량을 읽고 자르지 않습니다** — 수백 행 통계는 이 도구로 내십시오.
          이름으로 여러 시트가 걸리면 후보를 담은 ValueError 가 납니다. 그때는
          임의로 고르지 말고 사람에게 어느 것인지 물어보십시오. 열 이름이
          모호할 때도 같습니다 — 예외 메시지가 대안을 알려주니 그대로 따르십시오.
          `APIError [429]` 는 읽기 쿼터(분당 60회)가 찬 것입니다. 코드를 고쳐도
          소용없으니 사람에게 잠시 뒤 다시 부르라고 알리십시오.

        **행 수 주의**: 시트의 데이터 행을 **그대로** 돌려줍니다. 통째로 빈 행만
        버리므로, 아래쪽에 한 칸씩 끌어내려진 행이 섞여 있으면 그것도 들어옵니다.
        명단을 뽑을 때는 직접 거르십시오: `df = df[df["성함"].str.strip() != ""]`.
        여기서 대신 걸러 주면 몇 명이 빠졌는지가 안 보입니다.

        **통계를 낼 때 주의**:
        - print 로 **집계 결과만** 내보내십시오. DataFrame 전체를 print 하면
          답을 쓸 자리가 남지 않습니다.
        - 전화번호는 시트마다 하이픈 유무가 다릅니다. 대조·중복 제거 전에
          숫자만 남기십시오: `df["전화"].str.replace(r"\\D", "", regex=True)`

        **주의사항**:
        - 호출할 때마다 빈 상태에서 시작합니다. 앞선 호출에서 만든 변수는 남지
          않으니, 필요한 데이터는 매번 다시 읽으십시오. 다만 같은 시트를 여러 번
          읽으면 그만큼 쿼터를 쓰므로 한 번에 끝내는 코드를 쓰는 편이 낫습니다.
        - matplotlib을 사용할 때는 plt.savefig()를 호출하지 마세요. 자동으로 처리됩니다.
        - plt.show()도 호출하지 마세요.
        - 차트를 그린 후 plt.figure()나 plt.gcf()로 현재 figure에 접근할 수 있습니다.

        **예시**:
        ```python
        # Athena에서 데이터를 가져와서 차트 그리기
        results = execute_athena_query(
            "SELECT date, count FROM daily_stats ORDER BY date",
            database="analytics_db"
        )

        # 결과를 pandas DataFrame으로 변환
        rows = results["ResultSet"]["Rows"]
        headers = [col.get("VarCharValue", "") for col in rows[0]["Data"]]
        data_rows = [[col.get("VarCharValue", "") for col in row["Data"]] for row in rows[1:]]

        df = pd.DataFrame(data_rows, columns=headers)
        df["count"] = df["count"].astype(int)

        # pandas를 활용한 차트 그리기
        plt.figure(figsize=(10, 6))
        plt.plot(df["date"], df["count"])
        plt.xlabel('Date')
        plt.ylabel('Count')
        plt.title('Daily Stats')
        plt.xticks(rotation=45)
        plt.tight_layout()
        ```

        Args:
            code: 실행할 파이썬 코드

        Returns:
            str: 실행 결과 (STDOUT 출력 + 성공/실패 메시지)
        """
        loop = asyncio.get_running_loop()

        def execute_athena_query(
            query: str, database: str, max_wait_seconds: int = 300
        ) -> dict:
            """코드가 호출하는 동기 진입점. 쿼리 자체는 메인 루프에서 실행됩니다."""
            return asyncio.run_coroutine_threadsafe(
                athena.execute_and_wait(query, database, max_wait_seconds), loop
            ).result()

        # read_sheet 는 루프에 기대지 않으므로(gspread 가 동기다) 매 호출 만들 필요가 없다.
        # execute_athena_query 만 이번 호출의 루프를 붙잡아야 해서 여기서 만든다.
        injected = {
            "execute_athena_query": execute_athena_query,
            "read_sheet": read_sheet,
        }
        stdout_output, img_buffer, error_traceback = await loop.run_in_executor(
            _code_executor, functools.partial(_run_code, code, injected)
        )
        # DataFrame 을 통째로 print 하면 컨텍스트가 통째로 날아간다.
        if len(stdout_output) > STDOUT_LIMIT:
            stdout_output = (
                stdout_output[:STDOUT_LIMIT]
                + f"\n… {len(stdout_output)}자 중 {STDOUT_LIMIT}자에서 잘렸습니다."
                " 표 전체가 아니라 집계 결과만 print 하십시오."
            )

        if error_traceback:
            error_message = f"❌ 코드 실행 실패:\n\n{error_traceback}"
            if stdout_output:
                error_message += f"\n\nSTDOUT:\n{stdout_output}"
            return error_message

        if img_buffer is None:
            result_message = "✅ 코드 실행 성공"
        elif slack_client and channel and thread_ts:
            await slack_client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=img_buffer,
                filename="chart.png",
                title="차트 시각화 결과",
            )
            result_message = "✅ 코드 실행 성공: 차트를 슬랙에 업로드했습니다."
        else:
            result_message = "✅ 코드 실행 성공: 차트가 생성되었으나 슬랙 업로드에 필요한 정보가 없습니다."

        if stdout_output:
            return f"{result_message}\n\nSTDOUT:\n{stdout_output}"
        return result_message

    return execute_python
