-- 로봇에게 예약해 둔 작업.
--
-- 한 행이 예약 하나다. 슬랙에서 "내일 9시에 X 해줘", "매주 수요일 16시에 X 해줘"
-- 라고 하면 에이전트가 행을 넣고, 1분마다 도는 스케줄러 작업이 시각이 된 행을
-- 꺼내 예약한 스레드에서 그 요청(prompt)을 다시 에이전트에게 맡긴다.
--
-- 상태: pending → running → done | failed. pending 일 때만 canceled 로 바꿀 수 있다.
-- 반복 예약(cron)은 실행이 끝나면 다음 시각으로 run_at 을 옮기고 pending 으로
-- 돌아온다 -- 실패해도 다음 회차는 돈다. until 을 넘으면 done 이 된다.
--
-- running 에서 프로세스가 죽으면 그대로 남는다 -- 다시 돌리지 않는다. 에이전트가
-- 채널 공지나 문자·메일 초안처럼 밖에 흔적을 남기므로 두 번 도는 쪽이 더 나쁘다.

CREATE TABLE scheduled_task (
    id          bigserial   PRIMARY KEY,
    -- 실행 기록을 남길 곳. 예약을 건 스레드다. 회차마다 이 스레드에서 돈다.
    channel_id  text        NOT NULL,
    thread_ts   text        NOT NULL,
    -- 예약한 사람의 Slack 사용자 ID. 실행할 때 이 사람이 물은 것으로 돌린다.
    created_by  text        NOT NULL,
    -- 시각이 되면 에이전트에게 줄 요청. 그때 스레드를 처음 보는 사람이 읽어도
    -- 알 수 있게 완결된 문장으로 넣는다.
    prompt      text        NOT NULL,
    -- 다음 실행 시각.
    run_at      timestamptz NOT NULL,
    -- 반복 주기. KST 기준 5필드 크론식('0 16 * * wed'). 한 번만이면 NULL.
    cron        text,
    -- 반복을 멈출 시각. 이 시각 뒤로는 돌지 않는다. 끝없이 돌면 NULL.
    until       timestamptz,
    status      text        NOT NULL DEFAULT 'pending',
    started_at  timestamptz,
    finished_at timestamptz,
    -- 마지막으로 실패한 이유. 반복 예약은 다음 회차가 성공하면 지운다.
    last_error  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT scheduled_task_status_check
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'canceled'))
);

-- 폴러가 시각이 된 대기 행만 훑는다.
CREATE INDEX scheduled_task_due ON scheduled_task (run_at) WHERE status = 'pending';
