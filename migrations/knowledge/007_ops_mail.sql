-- ops@team-mono.com 으로 주고받은 메일.
--
-- 한 행이 메일 한 통이다. 받은 메일(in)과 보낸 메일(out)을 한 표에 둔다 --
-- 답장할 메일을 고를 때 방향과 상관없이 "이 상대와 마지막으로 오간 메일" 을 찾는다.
--
-- 메일 원본(첨부 포함)은 S3 에 있다. 여기에는 슬랙과 답장에 필요한 머리와
-- 본문 텍스트만 둔다.

CREATE TABLE ops_mail_message (
    id              bigserial PRIMARY KEY,
    direction       text        NOT NULL,
    -- RFC 5322 Message-ID. SES 로 보낸 메일은 SES 가 이 값을 바꿔 쓰므로
    -- 우리가 만든 값이다. 상대가 회신하면 References 에 SES 쪽 값이 실린다.
    -- 받은 메일에 없으면 지어 넣으므로 중복을 거르는 키로 쓰지 않는다.
    message_id      text        NOT NULL,
    -- 받은 메일은 S3 객체 키의 끝, 보낸 메일은 SES 응답의 MessageId.
    -- SNS 가 같은 알림을 두 번 보내도 한 번만 남는다.
    ses_message_id  text        NOT NULL UNIQUE,
    -- 상대방 주소. 소문자.
    counterpart     text        NOT NULL,
    from_address    text        NOT NULL,
    to_addresses    text[]      NOT NULL,
    subject         text        NOT NULL,
    body_text       text        NOT NULL,
    -- 답장의 References 헤더를 이어 쓰려고 받은 값을 그대로 둔다.
    references_ids  text[]      NOT NULL DEFAULT '{}',
    s3_key          text,
    channel_id      text        NOT NULL,
    -- 보낸 메일의 [보내기] 를 누른 사람의 Slack 사용자 ID.
    sent_by         text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ops_mail_message_direction_check CHECK (direction IN ('in', 'out'))
);

CREATE INDEX ops_mail_message_counterpart ON ops_mail_message (channel_id, counterpart, created_at DESC);

-- S3 에 들어온 받은 메일 원본 하나의 처리 상태. 메일 기록(ops_mail_message)과 따로
-- 둔다 -- 원본을 읽기도 전에 자리를 잡아야 하고, 보낸 메일에는 처리할 것이 없다.
--
-- 처리할 쪽(웹훅, 정리 작업)이 원본을 읽기 전에 행을 넣어 자리를 잡는다(claimed_at).
-- 슬랙에 다 올리면 메일 기록을 넣으면서 processed_at 을 채운다. 중간에 실패하면
-- 자리를 잡은 지 시간이 지난 뒤 attempts 가 남은 쪽이 다시 맡는다.
CREATE TABLE ops_mail_inbox (
    ses_message_id  text        PRIMARY KEY,
    s3_key          text        NOT NULL,
    attempts        int         NOT NULL,
    claimed_at      timestamptz NOT NULL,
    processed_at    timestamptz
);

CREATE INDEX ops_mail_inbox_s3_key ON ops_mail_inbox (s3_key);

-- 상대방 하나에 슬랙 스레드 하나. 같은 상대와 주고받은 메일은 방향과 상관없이
-- 이 스레드에 쌓인다. 새 메일로 먼저 보낸 상대는 회신이 와서 스레드가 열리기
-- 전까지 행이 없다 -- 수백 명에게 보내도 상대마다 스레드를 열지 않는다.
--
-- 같은 상대의 메일 두 통이 동시에 들어오면(웹훅과 정리 작업은 다른 프로세스다)
-- 둘 다 스레드를 열려고 한다. 기본 키가 하나만 남긴다.
CREATE TABLE ops_mail_thread (
    channel_id      text        NOT NULL,
    counterpart     text        NOT NULL,
    thread_ts       text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, counterpart),
    UNIQUE (channel_id, thread_ts)
);
