-- 사내 지식베이스 1차 스키마
--
-- 소스(Slack·노션·드라이브)를 가리지 않고 item 한 테이블에 적재한다.
-- 임베딩 컬럼은 모델을 실측으로 정한 뒤 002에서 추가한다. vector(N)의 N은
-- 컬럼 타입이라 나중에 바꾸면 전량 재임베딩이 필요하다.
--
-- 확장 생성은 rds_superuser 권한이 필요하므로 마스터 계정으로 실행한다.

CREATE EXTENSION IF NOT EXISTS pg_bigm;

-- 질의 범위를 좁히는 단위. 컴파일러 팀에게 인프라 런북을 보여주지 않기 위한 것.
-- 1차에서는 행을 만들지 않고 자리만 둔다.
CREATE TABLE project (
    id         bigserial PRIMARY KEY,
    key        text UNIQUE NOT NULL,
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Slack 채널 하나가 데이터소스 하나다. 신선도와 제외 규칙을 채널별로 조율한다.
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

-- item이 project를 직접 가리키지 않는 이유는 하나의 채널을 여러 project가
-- 참조할 수 있어야 하기 때문이다.
CREATE TABLE project_source (
    project_id     bigint NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    data_source_id bigint NOT NULL REFERENCES data_source(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, data_source_id)
);

CREATE TABLE item (
    id                bigserial PRIMARY KEY,
    data_source_id    bigint NOT NULL REFERENCES data_source(id) ON DELETE CASCADE,
    -- source는 data_source에도 있지만 필터에서 매번 조인하지 않으려고 중복 보관한다.
    source            text NOT NULL,
    -- Slack 스레드는 "<channel_id>:<thread_ts>", 버스트는 뒤에 시작 ts를 덧붙인다.
    external_id       text NOT NULL,
    kind              text NOT NULL,
    parent_id         bigint REFERENCES item(id) ON DELETE CASCADE,

    url               text,
    title             text,
    author            text,
    participants      text[],
    reply_count       int NOT NULL DEFAULT 0,
    reaction_count    int NOT NULL DEFAULT 0,
    source_created_at timestamptz NOT NULL,
    source_updated_at timestamptz,

    raw               jsonb NOT NULL,
    -- bigram 검색 대상. 임베딩에는 쓰지 않는다.
    raw_text          text NOT NULL,
    char_len          int GENERATED ALWAYS AS (length(raw_text)) STORED,
    -- token_df 재계산 시점의 스냅샷. index_score를 매번 다시 구하지 않으려고 남긴다.
    max_idf           real,
    index_score       real,

    distilled         jsonb,
    distilled_text    text,

    metadata          jsonb NOT NULL DEFAULT '{}',
    content_hash      text NOT NULL,
    distill_state     text NOT NULL DEFAULT 'pending',
    -- 스레드가 조용해진 뒤로 미룬다. 답글마다 재정제하면 비용이 감당되지 않는다.
    distill_after     timestamptz,
    indexed_at        timestamptz NOT NULL DEFAULT now(),

    UNIQUE (source, external_id),
    CONSTRAINT item_kind_check CHECK (kind IN ('thread', 'burst')),
    CONSTRAINT item_distill_state_check
        CHECK (distill_state IN ('pending', 'skipped', 'done', 'error'))
);

CREATE INDEX item_raw_bigm  ON item USING gin (raw_text gin_bigm_ops);
CREATE INDEX item_recency   ON item (data_source_id, source_created_at DESC);
CREATE INDEX item_meta      ON item USING gin (metadata jsonb_path_ops);
CREATE INDEX item_parent    ON item (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX item_distill_q ON item (distill_after) WHERE distill_state = 'pending';

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
    project_id bigint REFERENCES project(id),
    query      text NOT NULL,
    filters    jsonb,
    result_ids bigint[],
    latency_ms int,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX query_log_recency ON query_log (created_at DESC);
