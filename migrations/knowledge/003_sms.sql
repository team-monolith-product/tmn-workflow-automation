-- 문자 발송 이력.
--
-- 진입점이 슬랙 봇과 MCP 둘이라 발송 상태를 공유해야 한다. 지금까지는 로컬
-- jsonl 한 파일이 그 역할을 했는데, 그 파일을 못 보는 진입점이 생기는 순간
-- 같은 사람에게 문자가 두 번 간다.
--
-- 중복 차단을 애플리케이션이 아니라 UNIQUE 제약에 맡긴다. "로그를 읽어보고
-- 없으면 보낸다"는 검사-후-실행이라, 두 진입점이 동시에 돌면 둘 다 통과한다.
-- 제약이면 나중에 INSERT 하는 쪽이 반드시 진다.
--
-- 그래서 발송 순서가 INSERT -> 벤더 호출 -> UPDATE 다. 보내고 기록하면
-- 그 사이가 그대로 경합 구간이 된다.
CREATE TABLE sms_send (
    id            bigserial PRIMARY KEY,
    -- 문안 종류가 아니라 "이 발송 건"이다. 같은 문안을 다시 보내야 하면
    -- discord-resend 처럼 다른 값을 쓴다. 재발송을 실수와 구분하기 위해서다.
    campaign      text NOT NULL,
    phone         text NOT NULL,
    name          text,

    -- SMS(90byte) / LMS. 치환값이 들어간 뒤 길이가 정해지므로 수신자마다
    -- 다를 수 있다. 판정은 발송 단위로 하되 값은 행마다 남긴다.
    message_type  text NOT NULL,
    -- 실제로 나간 본문. 문안을 고친 뒤 재발송했는지 사후에 구분하는 유일한 근거다.
    content_hash  text NOT NULL,

    -- 벤더가 접수 시 발급. 예약 취소 키를 겸한다.
    message_key   text,
    -- 접수 결과(1000 = 성공). "접수됐다"이지 "도달했다"가 아니다.
    accepted_code text,
    -- 도달 결과. 뿌리오 v1 에는 조회 API 가 없어 항상 NULL 이고,
    -- 비즈뿌리오로 옮기면 폴링이 채운다.
    result_code   text,
    result_at     timestamptz,

    -- 감사. 도구 인자가 아니라 인증에서 받은 값을 넣는다.
    requested_by  text NOT NULL,
    entrypoint    text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),

    UNIQUE (campaign, phone),
    CONSTRAINT sms_send_entrypoint_check
        CHECK (entrypoint IN ('slack', 'mcp', 'script'))
);

-- "이 캠페인 어디까지 갔나"가 가장 잦은 질의다.
CREATE INDEX sms_send_campaign ON sms_send (campaign, created_at DESC);
-- 도달 결과를 아직 못 받은 건. 폴링이 이 인덱스로 대상을 좁힌다.
CREATE INDEX sms_send_pending_result ON sms_send (created_at)
    WHERE accepted_code = '1000' AND result_code IS NULL;
