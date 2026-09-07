-- 채널별 슬랙 작업 리스트
--
-- 채널 하나가 리스트 하나를 가진다. 여기 행이 있느냐로만 그 채널이 작업을
-- 리스트로 관리하는지 정해진다.
--
-- data_source 에 얹지 않는다. 그 표는 지식베이스 수집 대상이라는 뜻이고
-- query_knowledge 가 SQL 로 직접 질의하는 표다. 수집 대상이 아닌 행을 섞으면
-- 질의 결과가 오염된다.
--
-- 열 ID를 저장하는 이유: 슬랙에 리스트의 열을 조회하는 API 가 없다.
-- slackLists.create 응답이 열 ID를 아는 유일한 자리다. todo_mode 리스트는
-- 완료·담당자·마감일이 Col00·Col01·Col02 이고 제목 열만 무작위 ID를 받는다.
--
-- 열 ID 넷을 jsonb 한 칸이 아니라 각각의 열로 둔다. 코드가 넷을 모두
-- 요구하므로 jsonb 가 주는 유연성을 쓸 데가 없고, NOT NULL 이 적재 시점에
-- 계약을 지켜 준다.

CREATE TABLE channel_task_list (
    channel_id           text PRIMARY KEY,
    list_id              text NOT NULL,
    list_url             text NOT NULL,
    name_column_id       text NOT NULL,
    completed_column_id  text NOT NULL,
    assignee_column_id   text NOT NULL,
    due_date_column_id   text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);
