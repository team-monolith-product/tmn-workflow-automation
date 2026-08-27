-- Slack List 작업에 요청 맥락과 작업 기록 스레드를 따로 연결한다.
--
-- 기존 thread_column_id는 작업을 만든 원래 Slack 대화를 가리키므로 이름만
-- source_thread_column_id로 바꾼다. work_thread_column_id는 기존 List에서
-- 사용자가 message 타입의 "작업 기록" 열을 추가한 뒤 첫 작업 시작 때 채운다.
--
-- MCP는 List URL만 받기 때문에 list_id로 연결 채널을 역조회한다. List 하나를
-- 여러 채널 등록이 공유하면 어느 채널에 작업 스레드를 만들지 결정할 수 없으므로
-- unique index로 그 상태를 막는다.

ALTER TABLE channel_task_list
    RENAME COLUMN thread_column_id TO source_thread_column_id;

ALTER TABLE channel_task_list
    ADD COLUMN work_thread_column_id text;

CREATE UNIQUE INDEX channel_task_list_list_id_unique
    ON channel_task_list (list_id);
