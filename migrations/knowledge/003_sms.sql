-- 문자 발송 이력.
--
-- 뿌리오에는 발송결과 조회 API가 없고, 지금까지 기록은 슬랙 카드 한 줄뿐이었다.
-- 그 카드가 지워지거나 스레드를 못 찾으면 "이 선생님한테 뭘 보냈나"에 답할 방법이
-- 없다. 여기 남기면 기존 query_knowledge 도구가 그대로 읽는다.
--
-- 한 행이 "발송 × 받는 사람"이다. 발송과 수신자를 나누지 않는다 --
-- **읽는 쪽이 사람이 아니라 에이전트다.** query_knowledge 로 임의 SQL 을 짜므로
-- 조인이 하나 있으면 틀릴 자리가 하나 생기고, 조인을 빠뜨린 집계는 에러 없이
-- 그럴듯한 숫자를 낸다. 문안이 행마다 반복되지만 148명 캠페인이 150KB 라 무의미하다.
-- 발송 뒤에 고칠 일이 없는 감사 로그이므로 정규화로 지킬 무결성도 없다.
--
-- 접수(sent_at)와 도달은 다르다. code 1000은 뿌리오가 받았다는 뜻이고, 실제 도달
-- 여부는 웹 발송결과 페이지에만 있다. 그것을 긁어오는 작업이 오면 그때 컬럼을
-- 더한다 -- 지금 비워둘 자리를 미리 만들지 않는다.

CREATE TABLE sms_log (
    id            bigserial PRIMARY KEY,

    -- 발송 단위 --------------------------------------------------------------
    -- 같은 ref_key 가 한 번의 발송이다. message_key 로 묶으면 안 된다 --
    -- 벤더가 빠뜨리면 NULL 이고, NULL 끼리 뭉쳐 서로 다른 발송이 한 건으로 보인다.
    -- ref_key 는 우리가 요청마다 만들어 보내는 값이라 늘 있다.
    ref_key       text        NOT NULL,
    -- 벤더가 code 1000 을 주면서 messageKey 를 빠뜨릴 수 있다. NOT NULL 이면
    -- 그때 이력이 통째로 안 남아 문안과 수신자까지 같이 잃는다.
    message_key   text,
    channel_id    text        NOT NULL,
    -- 채널이 어느 사업인지. 매핑 표를 따로 두면 조인이 생기므로 발송 시점에 박는다.
    -- 매핑이 늘면 UPDATE sms_log SET project=... WHERE channel_id=... 로 소급한다.
    project       text,
    thread_ts     text        NOT NULL,
    -- 발신번호. 회신 전화가 어디로 갈지 이 번호가 정하고, 번호가 둘 이상이 되면
    -- "어느 번호로 나갔나"에 답할 수 있어야 한다.
    sender        text        NOT NULL,
    -- 벤더로 나간 문안. 치환 전 원문이라 [*이름*] 태그가 그대로 있다. 치환 후
    -- 문장을 남기면 재사용할 때 다시 템플릿으로 되돌려야 하고, 그 과정에서
    -- 담당자 이름까지 태그로 바뀌는 사고가 난다.
    content       text        NOT NULL,
    message_type  text        NOT NULL,
    approved_by   text        NOT NULL,
    -- 뿌리오가 접수한 시각. 예약 발송이면 실제로 나가는 것은 나중이다.
    sent_at       timestamptz NOT NULL DEFAULT now(),
    -- 예약 시각. 즉시 발송이면 NULL. 이게 없으면 다음 주에 나갈 문자가
    -- 오늘 나간 것처럼 보인다.
    scheduled_at  timestamptz,

    -- 받는 사람 단위 ----------------------------------------------------------
    phone         text        NOT NULL,
    name          text,
    change_word   jsonb
);

-- "이 번호로 뭘 보냈나" 가 CS 에서 제일 자주 묻는 질문이다.
CREATE INDEX sms_log_phone ON sms_log (phone);
CREATE INDEX sms_log_channel_sent ON sms_log (channel_id, sent_at DESC);
-- 캠페인 단위로 접을 때 쓴다.
CREATE INDEX sms_log_ref ON sms_log (ref_key);
