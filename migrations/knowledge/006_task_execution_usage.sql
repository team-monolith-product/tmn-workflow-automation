-- 운영팀 Slack List 작업의 에이전트 실행 사용량.
--
-- Slack은 사람이 공유할 결과의 원본이고, 이 표는 모델별 작업 효율을 비교하기 위한
-- 분석 원본이다. Slack 메시지 본문을 다시 파싱하지 않으며 메시지 ts도 복제하지 않는다.
--
-- 한 행은 "작업 × 에이전트 실행"이다. 모델·effort·루트/서브에이전트별 세부값은
-- usage_by_model JSON 배열에 두고, 자주 비교할 대표 모델과 합계는 일반 열에 둔다.
-- 같은 실행을 다시 보고하면 UPSERT하므로 완료 도구 재호출도 토큰을 중복 적재하지 않는다.

CREATE TABLE task_execution_usage (
    id                       bigserial PRIMARY KEY,
    list_id                  text        NOT NULL,
    record_id                text        NOT NULL,
    list_url                 text        NOT NULL,
    execution_id             text        NOT NULL,
    actor                    text        NOT NULL,
    status                   text        NOT NULL,
    service                  text        NOT NULL,
    model                    text,
    reasoning_effort         text,
    input_tokens             bigint,
    cached_input_tokens      bigint,
    cache_write_input_tokens bigint,
    output_tokens            bigint,
    reasoning_output_tokens  bigint,
    total_tokens             bigint,
    conversation_turns       integer,
    usage_by_model           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    task_started_at          timestamptz NOT NULL,
    task_finished_at         timestamptz NOT NULL,
    collector_version        text,
    collection_status        text        NOT NULL DEFAULT 'unavailable',
    recorded_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT task_execution_status_check
        CHECK (status IN ('completed', 'blocked', 'handoff')),
    CONSTRAINT task_execution_collection_status_check
        CHECK (collection_status IN ('complete', 'partial', 'unavailable')),
    CONSTRAINT task_execution_identity_unique
        UNIQUE (list_id, record_id, execution_id),
    CONSTRAINT task_execution_usage_by_model_check
        CHECK (jsonb_typeof(usage_by_model) = 'array'),
    CONSTRAINT task_execution_input_check CHECK (input_tokens IS NULL OR input_tokens >= 0),
    CONSTRAINT task_execution_cached_input_check
        CHECK (cached_input_tokens IS NULL OR cached_input_tokens >= 0),
    CONSTRAINT task_execution_cache_write_check
        CHECK (cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0),
    CONSTRAINT task_execution_output_check CHECK (output_tokens IS NULL OR output_tokens >= 0),
    CONSTRAINT task_execution_reasoning_check
        CHECK (reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
    CONSTRAINT task_execution_total_check CHECK (total_tokens IS NULL OR total_tokens >= 0),
    CONSTRAINT task_execution_turns_check
        CHECK (conversation_turns IS NULL OR conversation_turns >= 0)
);

CREATE INDEX task_execution_usage_task
    ON task_execution_usage (list_id, record_id, task_finished_at DESC);
CREATE INDEX task_execution_usage_model
    ON task_execution_usage (service, model, reasoning_effort, task_finished_at DESC);
