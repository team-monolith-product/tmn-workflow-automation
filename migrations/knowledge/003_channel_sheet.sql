-- 슬랙 채널 하나가 참가자 스프레드시트 하나를 가리킵니다.
--
-- 사업이 여러 개고 채널도 여러 개인데(t_고객_사업운영_*), 참가자 명단과
-- 발송이력은 사업마다 다릅니다. 전역 환경변수 하나로 두면 어느 채널에서
-- 보내든 같은 시트에 쌓여 사업이 섞입니다.
--
-- data_source.config 에 얹지 않는 이유는 register.upsert_source 가 재등록 때
-- config 를 통째로 덮어쓰기 때문입니다. 채널을 지식 수집에 다시 등록하는
-- 순간 이 매핑이 조용히 사라집니다.
CREATE TABLE channel_sheet (
    channel_id     text PRIMARY KEY,
    spreadsheet_id text NOT NULL,
    -- 누가 언제 연결했는지. 잘못 연결됐을 때 물어볼 사람을 찾는 용도다.
    connected_by   text NOT NULL,
    connected_at   timestamptz NOT NULL DEFAULT now()
);
