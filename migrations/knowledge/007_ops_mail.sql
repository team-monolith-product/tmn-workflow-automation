-- ops@team-mono.com 으로 주고받은 메일.
--
-- 한 행이 메일 한 통이다. 받은 메일(in)과 보낸 메일(out)을 한 표에 둔다 --
-- 슬랙 스레드가 상대방 주소 하나에 하나라서, 스레드를 고를 때도 답장할 메일을
-- 고를 때도 방향과 상관없이 "이 상대와 마지막으로 오간 메일" 을 찾는다.
--
-- 메일 원본(첨부 포함)은 S3 에 있다. 여기에는 슬랙과 답장에 필요한 머리와
-- 본문 텍스트만 둔다.

CREATE TABLE ops_mail_message (
    id              bigserial PRIMARY KEY,
    direction       text        NOT NULL,
    -- RFC 5322 Message-ID. SES 로 보낸 메일은 SES 가 이 값을 바꿔 쓰므로
    -- 우리가 만든 값이다. 상대가 회신하면 References 에 SES 쪽 값이 실린다.
    message_id      text        NOT NULL UNIQUE,
    ses_message_id  text,
    -- 상대방 주소. 소문자. 스레드를 묶는 키다.
    counterpart     text        NOT NULL,
    from_address    text        NOT NULL,
    to_addresses    text[]      NOT NULL,
    subject         text        NOT NULL,
    body_text       text        NOT NULL,
    -- 답장의 References 헤더를 이어 쓰려고 받은 값을 그대로 둔다.
    references_ids  text[]      NOT NULL DEFAULT '{}',
    s3_key          text,
    channel_id      text        NOT NULL,
    thread_ts       text        NOT NULL,
    -- 보낸 메일의 [보내기] 를 누른 사람의 Slack 사용자 ID.
    sent_by         text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ops_mail_message_direction_check CHECK (direction IN ('in', 'out'))
);

CREATE INDEX ops_mail_message_counterpart ON ops_mail_message (counterpart, created_at DESC);
CREATE INDEX ops_mail_message_thread ON ops_mail_message (channel_id, thread_ts, created_at DESC);
