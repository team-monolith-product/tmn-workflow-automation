-- 운영팀 Slack List 작업의 에이전트 실행 사용량.
--
-- Slack은 사람이 공유할 결과의 원본이고, 이 표는 모델별 작업 효율을 비교하기 위한
-- 분석 원본이다. Slack 메시지 본문을 다시 파싱하지 않으며 메시지 ts도 복제하지 않는다.
--
-- 한 행은 "작업 × 에이전트 실행"이다. 루트와 모든 서브에이전트의 토큰을 합산하고,
-- 대표 루트 모델과 effort만 둔다. 종류별 토큰은 실제 분석 필요가 생길 때 추가한다.
-- 같은 실행을 다시 보고하면 UPSERT하므로 완료 도구 재호출도 토큰을 중복 적재하지 않는다.

CREATE TABLE task_execution_usage (
    list_url                 text        NOT NULL,
    execution_id             text        NOT NULL,
    status                   text        NOT NULL,
    service                  text        NOT NULL,
    model                    text,
    reasoning_effort         text,
    total_tokens             bigint,
    task_started_at          timestamptz NOT NULL,
    task_finished_at         timestamptz NOT NULL,
    collector_version        text,
    collection_status        text        NOT NULL DEFAULT 'unavailable',

    PRIMARY KEY (list_url, execution_id),
    CONSTRAINT task_execution_status_check
        CHECK (status IN ('completed', 'blocked', 'handoff')),
    CONSTRAINT task_execution_collection_status_check
        CHECK (collection_status IN ('complete', 'partial', 'unavailable')),
    CONSTRAINT task_execution_total_check CHECK (total_tokens IS NULL OR total_tokens >= 0)
);

CREATE INDEX task_execution_usage_model
    ON task_execution_usage (service, model, reasoning_effort, task_finished_at DESC);
