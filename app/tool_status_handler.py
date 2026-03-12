"""
툴 호출 상태를 슬랙에 표시하는 콜백 핸들러
"""

import asyncio
import os
from langchain_core.callbacks import BaseCallbackHandler


class ToolStatusHandler(BaseCallbackHandler):
    """
    Agent 툴 호출 상태를 Slack에 표시하는 핸들러

    - 하나의 section 메시지를 업데이트하여 모든 툴 호출을 누적 표시
    - section 블록의 expand=False로 긴 내용은 자동으로 접힘
    - asyncio.Lock으로 동시성 문제 해결
    - Lock 범위를 최소화하여 데드락 방지
    - Slack 3000자 제한 대응: 완료된 툴은 카운트만, 실행중/에러는 params 요약
    """

    def __init__(self, say, thread_ts: str, slack_client, channel: str):
        """
        Args:
            say: Slack 메시지 전송 함수
            thread_ts: 스레드 타임스탬프
            slack_client: Slack 클라이언트 (chat_update용)
            channel: 채널 ID
        """
        self.say = say
        self.thread_ts = thread_ts
        self.slack_client = slack_client
        self.channel = channel

        self.status_message_ts = None  # 상태 메시지의 타임스탬프
        self.tool_history = (
            []
        )  # 실행된 툴 이력: {"run_id": str, "name": str, "params": str, "status": str}
        self._lock = None  # 동시성 제어를 위한 lock (lazy init)
        self._message_created_event = (
            None  # 첫 메시지 생성 완료 대기용 Event (lazy init)
        )
        self._creating_message = False  # 첫 메시지 생성 중 플래그
        self.langsmith_run_id = None  # LangSmith trace의 최상위 run_id

    @property
    def lock(self):
        """현재 event loop에서 Lock을 lazy하게 생성"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def message_created_event(self):
        """현재 event loop에서 Event를 lazy하게 생성"""
        if self._message_created_event is None:
            self._message_created_event = asyncio.Event()
        return self._message_created_event

    def _format_status_text(self) -> str:
        """
        툴 실행 상태를 Slack 메시지 형식으로 포맷팅합니다.

        Slack section block의 3000자 제한을 고려하여 params는 50자로 통일

        Returns:
            str: 포맷된 상태 메시지
        """
        MAX_PARAM_LENGTH = 50
        status_lines = []

        # LangSmith 링크 추가 (개발자용)
        if self.langsmith_run_id:
            org_id = os.getenv("LANGSMITH_ORG_ID")
            project_id = os.getenv("LANGSMITH_PROJECT_ID")

            if org_id and project_id:
                # 완전한 LangSmith trace URL 생성
                langsmith_url = (
                    f"https://smith.langchain.com/o/{org_id}/projects/p/{project_id}"
                    f"/r/{self.langsmith_run_id}?trace_id={self.langsmith_run_id}"
                )
                status_lines.append(f"🔍 <{langsmith_url}|LangSmith Trace>")
            else:
                # 환경변수가 없으면 Run ID만 표시
                status_lines.append(f"🔍 LangSmith Run ID: `{self.langsmith_run_id}`")

            status_lines.append("")  # 빈 줄 추가

        # 상태별로 분류
        completed = [t for t in self.tool_history if t["status"] == "completed"]
        running = [t for t in self.tool_history if t["status"] == "running"]
        errors = [t for t in self.tool_history if t["status"] == "error"]

        # 완료된 툴
        for tool_info in completed:
            params = tool_info["params"]
            params_short = (
                params[:MAX_PARAM_LENGTH] + "..."
                if len(params) > MAX_PARAM_LENGTH
                else params
            )
            status_lines.append(f"✅ {tool_info['name']}({params_short})")

        # 에러 툴
        for tool_info in errors:
            params = tool_info["params"]
            params_short = (
                params[:MAX_PARAM_LENGTH] + "..."
                if len(params) > MAX_PARAM_LENGTH
                else params
            )
            status_lines.append(f"❌ {tool_info['name']}({params_short})")

        # 실행 중인 툴
        for tool_info in running:
            params = tool_info["params"]
            params_short = (
                params[:MAX_PARAM_LENGTH] + "..."
                if len(params) > MAX_PARAM_LENGTH
                else params
            )
            status_lines.append(f"⏳ {tool_info['name']}({params_short})")

        return "\n".join(status_lines)

    async def on_chain_start(
        self,
        _serialized,
        _inputs,
        **kwargs,
    ):
        """
        Agent chain이 시작될 때 호출됩니다.
        최상위 run_id를 캡처하여 LangSmith 링크 생성에 사용합니다.

        Args:
            _serialized: Chain 정보 (사용하지 않음)
            _inputs: 입력 데이터 (사용하지 않음)
            **kwargs: run_id 등의 추가 정보
        """
        # 최초 1회만 run_id 저장 (agent executor의 최상위 run_id)
        if self.langsmith_run_id is None:
            self.langsmith_run_id = kwargs.get("run_id")

    async def on_tool_start(
        self,
        serialized,
        input_str,
        **kwargs,
    ):
        tool_name = serialized["name"]
        run_id = kwargs.get("run_id")  # 툴 실행의 고유 ID

        # Lock 안에서 tool_history 수정 및 역할 결정
        async with self.lock:
            self.tool_history.append(
                {
                    "run_id": run_id,
                    "name": tool_name,
                    "params": input_str,
                    "status": "running",
                }
            )

            status_text = self._format_status_text()

            if self.status_message_ts is not None:
                # 이미 메시지가 존재 → 업데이트
                role = "update"
            elif self._creating_message:
                # 다른 코루틴이 메시지 생성 중 → 대기 후 업데이트
                role = "wait_then_update"
            else:
                # 첫 번째 코루틴 → 메시지 생성 담당
                self._creating_message = True
                role = "create"

        # Lock 밖에서 Slack API 호출
        if role == "create":
            response = await self.say(
                {
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": status_text},
                            "expand": False,
                        }
                    ]
                },
                thread_ts=self.thread_ts,
            )
            async with self.lock:
                self.status_message_ts = response.get("ts")
            # 대기 중인 코루틴들에게 메시지 생성 완료를 알림
            self.message_created_event.set()
        else:
            if role == "wait_then_update":
                # 첫 메시지 생성이 완료될 때까지 대기
                await self.message_created_event.wait()
                # 대기 후 최신 상태로 다시 포맷팅
                async with self.lock:
                    status_text = self._format_status_text()

            # 기존 메시지 업데이트
            await self.slack_client.chat_update(
                channel=self.channel,
                ts=self.status_message_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": status_text},
                        "expand": False,
                    }
                ],
            )

    async def on_tool_end(
        self,
        _output,
        **kwargs,
    ):
        run_id = kwargs.get("run_id")  # 완료된 툴의 고유 ID

        # Lock 안에서는 tool_history만 수정
        async with self.lock:
            # run_id로 정확한 툴을 찾아 완료 상태로 변경
            for tool_info in self.tool_history:
                if tool_info["run_id"] == run_id:
                    tool_info["status"] = "completed"
                    break

            status_text = self._format_status_text()
            message_ts = self.status_message_ts

        # Lock 밖에서 Slack API 호출
        if message_ts:
            await self.slack_client.chat_update(
                channel=self.channel,
                ts=message_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": status_text},
                        "expand": False,
                    }
                ],
            )

    async def on_tool_error(
        self,
        _error,
        **kwargs,
    ):
        """
        툴 실행 중 에러 발생 시 호출됩니다.

        Args:
            _error: 발생한 에러 (사용하지 않음)
            **kwargs: run_id 등의 추가 정보
        """
        run_id = kwargs.get("run_id")  # 에러가 발생한 툴의 고유 ID

        # Lock 안에서는 tool_history만 수정
        async with self.lock:
            # run_id로 정확한 툴을 찾아 에러 상태로 변경
            for tool_info in self.tool_history:
                if tool_info["run_id"] == run_id:
                    tool_info["status"] = "error"
                    break

            status_text = self._format_status_text()
            message_ts = self.status_message_ts

        # Lock 밖에서 Slack API 호출
        if message_ts:
            await self.slack_client.chat_update(
                channel=self.channel,
                ts=message_ts,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": status_text},
                        "expand": False,
                    }
                ],
            )
