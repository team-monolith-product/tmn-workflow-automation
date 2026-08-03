-- 사내 지식베이스 1차 스키마
--
-- 소스(Slack·노션·드라이브)를 가리지 않고 item 한 테이블에 적재한다.
--
-- 1차에 없는 것과 이유:
--   embedding      모델을 실측으로 정한 뒤 002. vector(N)의 N은 컬럼 타입이라
--                  나중에 바꾸면 전량 재임베딩이다.
--   kind/parent_id burst가 002에 들어올 때 같이 만든다. 지금은 item이 항상
--                  스레드 하나라 값이 하나뿐인 컬럼이 된다.
--   project        질의 범위 좁히기. item이 data_source_id만 물고 있어서
--                  나중에 테이블을 추가해도 item 재분류가 필요 없다.
--
-- 확장 생성은 rds_superuser 권한이 필요하므로 마스터 계정으로 실행한다.

CREATE EXTENSION IF NOT EXISTS pg_bigm;

-- Slack 채널 하나가 데이터소스 하나다. 수집 주기와 제외 규칙을 채널별로 준다.
CREATE TABLE data_source (
    id             bigserial PRIMARY KEY,
    source         text NOT NULL,
    external_id    text NOT NULL,
    name           text NOT NULL,
    enabled        boolean NOT NULL DEFAULT true,
    config         jsonb NOT NULL DEFAULT '{}',
    joined_at      timestamptz,
    backfilled_at  timestamptz,
    cursor         text,
    last_synced_at timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);

-- 소스 무관 단일 인덱스 테이블.
--
-- 컬럼과 metadata를 가르는 기준은 소스가 아니라 역할이다. 검색 엔진이 소스와
-- 무관하게 모든 행에서 읽는 것만 컬럼이고, 나머지는 metadata에 둔다. Slack의
-- participants·reply_count·reaction_count는 index_score의 입력일 뿐이고
-- 검색 경로는 산출된 점수만 보므로 metadata에 있다.
CREATE TABLE item (
    id                bigserial PRIMARY KEY,
    data_source_id    bigint NOT NULL REFERENCES data_source(id) ON DELETE CASCADE,
    -- Slack 스레드는 "<channel_id>:<thread_ts>"
    external_id       text NOT NULL,

    url               text,
    title             text,
    author            text,
    source_created_at timestamptz NOT NULL,
    source_updated_at timestamptz,

    raw               jsonb NOT NULL,
    -- 어휘 검색 대상. 임베딩에는 쓰지 않는다. 정제가 날려버리는 고유 문자열
    -- (에러 코드, 리소스 ID, 사람 이름)이 여기에만 남는다.
    raw_text          text NOT NULL,
    char_len          int GENERATED ALWAYS AS (length(raw_text)) STORED,
    -- token_df 재계산 시점의 스냅샷. index_score를 매번 다시 구하지 않으려고 남긴다.
    max_idf           real,
    index_score       real,

    -- LLM이 스레드 하나를 재작성한 결과. 스레드당 하나다.
    -- question, summary, resolution, systems, code_refs
    distilled         jsonb,
    -- 위 다섯 항목을 이어붙인 정규화 문서. 002에서 임베딩 입력이 되고 1차에도
    -- bigram으로 검색한다. question은 "엔지니어가 실제로 검색할 법한 한 줄"로
    -- 뽑으므로 사용자 질의와 같은 어휘 공간에 놓인다.
    --
    -- 구조를 distilled에 따로 보존하는 이유는 조합을 바꿀 때 LLM을 다시 돌리지
    -- 않고 다시 렌더하기만 하면 되기 때문이다. 정제가 파이프라인에서 제일 비싸다.
    distilled_text    text,

    metadata          jsonb NOT NULL DEFAULT '{}',
    content_hash      text NOT NULL,
    distill_state     text NOT NULL DEFAULT 'pending',
    -- 스레드가 조용해진 뒤로 미룬다. 답글마다 재정제하면 비용이 감당되지 않는다.
    distill_after     timestamptz,
    indexed_at        timestamptz NOT NULL DEFAULT now(),

    UNIQUE (data_source_id, external_id),
    CONSTRAINT item_distill_state_check
        CHECK (distill_state IN ('pending', 'skipped', 'done', 'error'))
);

CREATE INDEX item_raw_bigm   ON item USING gin (raw_text gin_bigm_ops);
CREATE INDEX item_distilled_bigm ON item USING gin (distilled_text gin_bigm_ops)
    WHERE distilled_text IS NOT NULL;
CREATE INDEX item_recency    ON item (data_source_id, source_created_at DESC);
CREATE INDEX item_meta       ON item USING gin (metadata jsonb_path_ops);
CREATE INDEX item_distill_q  ON item (distill_after) WHERE distill_state = 'pending';

-- index_score의 IDF 항과 검색 3번째 신호가 같은 표를 본다.
-- tokenizer를 바꾸면 df가 전부 무의미해지므로 이름을 같이 남긴다.
CREATE TABLE token_df (
    token      text PRIMARY KEY,
    df         bigint NOT NULL,
    tokenizer  text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 무엇이 검색되지 않는지는 이 표로만 알 수 있다.
CREATE TABLE query_log (
    id         bigserial PRIMARY KEY,
    actor      text NOT NULL,
    tool       text NOT NULL,
    query      text NOT NULL,
    filters    jsonb,
    result_ids bigint[],
    latency_ms int,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX query_log_recency ON query_log (created_at DESC);
