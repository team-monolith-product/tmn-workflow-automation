"""
툴 호출 상태를 슬랙에 표시하는 콜백 핸들러
"""

import asyncio
from langchain_core.callbacks import BaseCallbackHandler


class ToolStatusHandler(BaseCallbackHandler):
    """
    Agent 툴 호출 상태를 Slack에 표시하는 핸들러

    - 하나의 section 메시지를 업데이트하여 모든 툴 호출을 누적 표시
    - section 블록의 expand=False로 긴 내용은 자동으로 접힘
    - asyncio.Lock으로 동시성 문제 해결
    - Lock 범위를 최소화하여 데드락 방지
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
        self.tool_history = []  # 실행된 툴 이력: {"run_id": str, "name": str, "params": str, "status": str}
        self._lock = None  # 동시성 제어를 위한 lock (lazy init)

    @property
    def lock(self):
        """현재 event loop에서 Lock을 lazy하게 생성"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def on_tool_start(
        self,
        serialized,
        input_str,
        **kwargs,
    ):
        tool_name = serialized["name"]
        run_id = kwargs.get("run_id")  # 툴 실행의 고유 ID

        # Lock 안에서는 tool_history만 수정
        async with self.lock:
            self.tool_history.append({
                "run_id": run_id,
                "name": tool_name,
                "params": input_str,
                "status": "running"
            })

            # 현재 상태 스냅샷 복사
            status_lines = []
            for tool_info in self.tool_history:
                if tool_info["status"] == "completed":
                    status_lines.append(f"✅ {tool_info['name']}({tool_info['params']})")
                elif tool_info["status"] == "running":
                    status_lines.append(f"⏳ {tool_info['name']}({tool_info['params']})")

            status_text = "\n".join(status_lines)
            is_first_message = self.status_message_ts is None

        # Lock 밖에서 Slack API 호출
        if is_first_message:
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
            # say() 응답에서 ts 저장
            async with self.lock:
                if self.status_message_ts is None:  # 다시 체크 (race condition 방지)
                    self.status_message_ts = response.get("ts")
        else:
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
        output,
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

            # 현재 상태 스냅샷 복사
            status_lines = []
            for tool_info in self.tool_history:
                if tool_info["status"] == "completed":
                    status_lines.append(f"✅ {tool_info['name']}({tool_info['params']})")
                elif tool_info["status"] == "running":
                    status_lines.append(f"⏳ {tool_info['name']}({tool_info['params']})")

            status_text = "\n".join(status_lines)
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
