-- 문자 발송 이력.
--
-- 뿌리오에는 발송결과 조회 API가 없고, 지금까지 기록은 슬랙 카드 한 줄뿐이었다.
-- 그 카드가 지워지거나 스레드를 못 찾으면 "이 선생님한테 뭘 보냈나"에 답할 방법이
-- 없다. 여기 남기면 기존 query_knowledge 도구가 그대로 읽는다.
--
-- 접수(sent_at)와 도달은 다르다. code 1000은 뿌리오가 받았다는 뜻이고, 실제 도달
-- 여부는 웹 발송결과 페이지에만 있다. 그것을 긁어오는 작업이 오면 그때 컬럼을
-- 더한다 -- 지금 비워둘 자리를 미리 만들지 않는다.

-- 사업 하나가 채널 여러 개를 쓴다(본채널 + _cs). 채널 이름으로는 묶을 수 없다 --
-- 실측 12개 중 구분자가 '-' 와 '_' 로 갈리고 사업명이 없는 것도 있다.
CREATE TABLE sms_channel (
    channel_id  text PRIMARY KEY,
    project     text NOT NULL
);

CREATE TABLE sms_send (
    id            bigserial PRIMARY KEY,
    channel_id    text        NOT NULL,
    -- 같은 스레드의 발송이 한 캠페인이다. 누락자 재발송은 같은 스레드에서 하므로
    -- 캠페인을 따로 식별할 컬럼이 필요 없다.
    thread_ts     text        NOT NULL,
    -- 벤더로 나간 문안. 치환 전 원문이라 [*이름*] 태그가 그대로 있다. 치환 후
    -- 문장을 남기면 재사용할 때 다시 템플릿으로 되돌려야 하고, 그 과정에서
    -- 담당자 이름까지 태그로 바뀌는 사고가 난다.
    content       text        NOT NULL,
    message_type  text        NOT NULL,
    -- 벤더가 code 1000 을 주면서 messageKey 를 빠뜨릴 수 있다. NOT NULL 이면
    -- 그때 이력이 통째로 안 남아 문안과 수신자까지 같이 잃는다.
    message_key   text,
    approved_by   text        NOT NULL,
    sent_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX sms_send_channel_sent ON sms_send (channel_id, sent_at DESC);

CREATE TABLE sms_recipient (
    id           bigserial PRIMARY KEY,
    send_id      bigint NOT NULL REFERENCES sms_send (id) ON DELETE CASCADE,
    phone        text   NOT NULL,
    name         text,
    change_word  jsonb
);

-- "이 번호로 뭘 보냈나" 가 CS 에서 제일 자주 묻는 질문이다.
CREATE INDEX sms_recipient_phone ON sms_recipient (phone);
CREATE INDEX sms_recipient_send ON sms_recipient (send_id);

INSERT INTO sms_channel (channel_id, project) VALUES
    ('C0AP8CG1Y6N', '26기업연계정보교원연수'),
    ('C0BRF9XJ40N', '26기업연계정보교원연수');
