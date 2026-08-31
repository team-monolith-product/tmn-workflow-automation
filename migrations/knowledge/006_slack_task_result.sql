-- Slack List 작업마다 종료 결과 메시지를 하나만 유지한다.
--
-- Slack 메시지 게시와 우리 DB 기록은 한 트랜잭션으로 묶을 수 없다. 게시 응답의
-- ts를 여기에 남겨 두면 이후 호출은 새 댓글 대신 chat.update를 쓸 수 있다.
-- client_msg_id는 게시 직후 DB 저장 전에 프로세스가 끊긴 경우에도 같은 게시
-- 요청임을 Slack이 식별할 수 있게 한다.

CREATE TABLE slack_task_result_message (
    list_id        text        NOT NULL,
    record_id      text        NOT NULL,
    client_msg_id  text        NOT NULL UNIQUE,
    channel_id     text        NOT NULL,
    message_ts     text        NOT NULL,
    permalink      text,

    PRIMARY KEY (list_id, record_id),
    UNIQUE (channel_id, message_ts)
);
