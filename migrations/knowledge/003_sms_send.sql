-- 문자 발송 기록. 한 사람에게 한 번 보낸 것이 한 행이다.
--
-- 시트가 아니라 DB 인 이유는 중복 차단이다. 구글 시트에는 조건부 쓰기가 없어
-- "빈칸일 때만 쓴다"를 원자적으로 못 한다. 진입점이 CLI·슬랙·MCP·스케줄러로
-- 늘어나면 겹치는 순간 같은 사람에게 두 번 나간다. 여기서는 부분 UNIQUE 인덱스
-- 하나가 그걸 막는다.
--
-- 조회는 사람이 이 테이블을 열어보는 게 아니라 봇에게 묻거나 Redash 로 본다.
CREATE TABLE sms_send (
    id           bigserial PRIMARY KEY,
    -- 공식 문자의 발송 건 식별자. 개인 CS 는 NULL 이다 — 같은 사람에게 여러 번
    -- 보내는 게 정상이라 중복 차단 대상이 아니다.
    campaign     text,
    -- 숫자만 남긴 수신번호. 표기가 섞이면 같은 사람을 못 알아본다.
    phone        text NOT NULL,
    -- 발송중: 접수 여부를 모른다(타임아웃·5xx). 사람이 뿌리오 웹에서 확인해야
    --         풀린다. 재시도를 막는 상태다.
    -- 발송:   접수됐다. 도달은 별개다.
    -- 실패:   벤더가 거절한 것이 확실하다. 재시도가 열린다.
    status       text NOT NULL CHECK (status IN ('발송중', '발송', '실패')),
    -- 치환 전 원문. 나중에 "그때 뭐라고 보냈더라"에 답하려면 있어야 한다.
    content      text NOT NULL,
    message_key  text,
    channel_id   text,
    requested_by text,
    sent_at      timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- 중복 차단의 전부다. 실패한 건은 빼서 재시도를 연다.
CREATE UNIQUE INDEX sms_send_campaign_phone
    ON sms_send (campaign, phone)
    WHERE campaign IS NOT NULL AND status <> '실패';

-- "이 번호한테 뭐 보냈어?" 가 가장 흔한 조회다.
CREATE INDEX sms_send_phone ON sms_send (phone, created_at DESC);
