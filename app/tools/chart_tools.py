"""
차트 시각화 관련 LangChain Tools
"""

import io
import sys
import traceback
from typing import Annotated, Callable, Any

from langchain_core.tools import tool
import matplotlib

matplotlib.use("Agg")  # GUI 없는 백엔드 사용
import matplotlib.pyplot as plt
import pandas as pd

from api import athena


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


def get_execute_python_with_chart_tool(
    say: Callable[[dict[str, Any], str], Any] | None = None,
    thread_ts: str | None = None,
    slack_client: Any | None = None,
    channel: str | None = None,
):
    """
    파이썬 코드를 실행하고 matplotlib 차트를 슬랙으로 전송하는 도구를 반환합니다.

    Args:
        say: Slack 메시지 전송 함수
        thread_ts: Slack 스레드 타임스탬프
        slack_client: Slack WebClient 인스턴스
        channel: Slack 채널 ID

    Returns:
        execute_python_with_chart tool
    """

    @tool
    async def execute_python_with_chart(
        code: Annotated[
            str,
            "실행할 파이썬 코드. matplotlib으로 차트를 그리는 코드를 포함할 수 있음",
        ],
    ) -> str:
        """
        파이썬 코드를 실행하고 matplotlib 차트를 슬랙으로 전송합니다.

        이 도구는 데이터 시각화가 필요할 때 사용합니다.
        코드 내에서 matplotlib을 사용하여 차트를 그리면 자동으로 슬랙에 이미지로 전송됩니다.

        **사용 가능한 라이브러리 및 함수**:
        코드 컨텍스트 내에서 다음을 사용할 수 있습니다:
        - `pd` (pandas): 데이터 분석 및 조작을 위한 pandas 라이브러리
        - `plt` (matplotlib.pyplot): 차트 시각화를 위한 matplotlib
        - `execute_athena_query(query: str, database: str)`: Athena SQL을 실행하고 결과를 반환합니다.
          결과는 dict 형태이며, "ResultSet" 키에 쿼리 결과가 포함됩니다.

        **주의사항**:
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
        # STDOUT 캡처를 위한 StringIO
        captured_output = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_output

        try:
            # 코드 실행을 위한 globals 준비
            # athena.execute_and_wait 함수를 제공
            exec_globals = {
                "execute_athena_query": athena.execute_and_wait,
                "plt": plt,
                "matplotlib": matplotlib,
                "pd": pd,
                "__builtins__": __builtins__,
            }
            exec_locals = {}

            # 코드 실행
            exec(code, exec_globals, exec_locals)

            # STDOUT 복원
            sys.stdout = old_stdout
            stdout_output = captured_output.getvalue()

            # matplotlib figure가 생성되었는지 확인
            fig = plt.gcf()
            if fig.get_axes():
                # figure가 있으면 슬랙에 업로드
                if slack_client and channel and thread_ts:
                    # 이미지를 메모리에 저장
                    img_buffer = io.BytesIO()
                    plt.savefig(img_buffer, format="png", dpi=150, bbox_inches="tight")
                    img_buffer.seek(0)

                    # 슬랙에 이미지 업로드
                    await slack_client.files_upload_v2(
                        channel=channel,
                        thread_ts=thread_ts,
                        file=img_buffer,
                        filename="chart.png",
                        title="차트 시각화 결과",
                    )

                    # figure 닫기
                    plt.close(fig)

                    result_message = "✅ 코드 실행 성공: 차트를 슬랙에 업로드했습니다."
                else:
                    plt.close(fig)
                    result_message = "✅ 코드 실행 성공: 차트가 생성되었으나 슬랙 업로드에 필요한 정보가 없습니다."
            else:
                # figure가 없으면 일반 코드 실행으로 간주
                result_message = "✅ 코드 실행 성공"

            # STDOUT이 있으면 포함
            if stdout_output:
                return f"{result_message}\n\nSTDOUT:\n{stdout_output}"
            else:
                return result_message

        except Exception as e:
            # STDOUT 복원
            sys.stdout = old_stdout
            stdout_output = captured_output.getvalue()

            # 스택트레이스 추출
            tb = traceback.format_exc()

            # 에러 메시지 구성
            error_message = f"❌ 코드 실행 실패:\n\n{tb}"

            # STDOUT이 있으면 포함
            if stdout_output:
                error_message += f"\n\nSTDOUT:\n{stdout_output}"

            return error_message
        finally:
            # 혹시 모를 figure가 남아있으면 닫기
            plt.close("all")

    return execute_python_with_chart


# 기본 tool (backward compatibility를 위해 유지)
execute_python_with_chart = get_execute_python_with_chart_tool()
