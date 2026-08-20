-- 문자 발송 기록. 한 사람에게 한 번 보낸 것이 한 행이다.
--
-- 시트가 아니라 DB 인 이유는 중복 차단이다. 구글 시트에는 조건부 쓰기가 없어
-- "빈칸일 때만 쓴다"를 원자적으로 못 한다. 진입점이 CLI·슬랙·MCP·스케줄러로
-- 늘어나면 겹치는 순간 같은 사람에게 두 번 나간다. 여기서는 부분 UNIQUE 인덱스
-- 하나가 그걸 막는다.
--
-- 상태를 status 한 컬럼으로 두지 않는다. 그러면 도달 확인이 붙을 때 '발송'을
-- '도달'로 덮어써야 하고, 언제 보냈는지가 사라진다. 시각을 따로 남기면 상태가
-- 덮이지 않고 쌓인다.
--
--     sent_at IS NULL AND failed_at IS NULL   접수 여부를 모른다(타임아웃·5xx)
--     sent_at IS NOT NULL                     벤더가 접수했다
--     failed_at IS NOT NULL                   벤더가 거절했다. 재시도가 열린다
--     confirmed_at IS NULL                    아직 도달을 확인하지 못했다
--
-- 조회는 사람이 이 테이블을 열어보는 게 아니라 봇에게 묻거나 Redash 로 본다.
CREATE TABLE sms_send (
    id           bigserial PRIMARY KEY,
    -- 공식 문자의 발송 건 식별자. 개인 CS 는 NULL 이다 — 같은 사람에게 여러 번
    -- 보내는 게 정상이라 중복 차단 대상이 아니다.
    campaign     text,
    -- 숫자만 남긴 수신번호. 표기가 섞이면 같은 사람을 못 알아본다.
    phone        text NOT NULL,
    -- 치환 전 원문과 그 사람의 치환값. 둘이 있어야 "그때 이 사람이 받은 문자"를
    -- 그대로 되살릴 수 있다. 원문만 남기면 [*이름*] 자리가 비어 보인다.
    content      text NOT NULL,
    variables    jsonb NOT NULL DEFAULT '{}',
    message_key  text,
    channel_id   text,
    requested_by text,

    -- 자리를 잡은 시각. 벤더를 부르기 전에 넣는다.
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    -- 벤더가 접수한 시각(code 1000).
    sent_at       timestamptz,
    -- 예약이면 실제로 나갈 시각. 즉시 발송이면 NULL.
    scheduled_for timestamptz,
    -- 벤더가 거절한 시각. 안 나간 것이 확실하다.
    failed_at     timestamptz,
    -- 도달을 확인한 시각. 뿌리오 웹 발송결과를 읽어 채운다.
    confirmed_at  timestamptz,

    -- 접수와 거절이 동시에 참일 수는 없다.
    CONSTRAINT sms_send_sent_xor_failed
        CHECK (sent_at IS NULL OR failed_at IS NULL),
    -- 보내지도 않은 것이 도달할 수는 없다.
    CONSTRAINT sms_send_confirm_after_send
        CHECK (confirmed_at IS NULL OR sent_at IS NOT NULL)
);

-- 중복 차단의 전부다. 거절당한 건은 빠져서 재시도가 열린다.
CREATE UNIQUE INDEX sms_send_campaign_phone
    ON sms_send (campaign, phone)
    WHERE campaign IS NOT NULL AND failed_at IS NULL;

-- "이 번호한테 뭐 보냈어?" 가 가장 흔한 조회다.
CREATE INDEX sms_send_phone ON sms_send (phone, claimed_at DESC);

-- 도달 확인이 찾는 것: 보냈는데 아직 확인 못 한 건.
CREATE INDEX sms_send_unconfirmed
    ON sms_send (sent_at)
    WHERE sent_at IS NOT NULL AND confirmed_at IS NULL;
