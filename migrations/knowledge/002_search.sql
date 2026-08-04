-- 어휘 검색 인덱스를 소문자 표현식으로 옮긴다.
--
-- pg_bigm은 LIKE만 인덱스로 처리한다. ILIKE는 연산자가 달라 순차 스캔으로
-- 떨어진다. 그래서 대소문자를 무시하려면 질의와 대상을 같이 접어야 하고,
-- 접은 값에 인덱스가 있어야 한다.
--
-- 실측 근거(item 14,843행): "sidekiq" 56건, "Sidekiq" 38건, 대소문자를
-- 무시하면 77건. 한글은 영향이 없지만 식별자·서비스명은 표기가 갈린다.
--
-- 001의 raw_text 인덱스는 지운다. 검색이 전부 lower()를 거치면 그쪽은 아무도
-- 읽지 않으면서 27MB를 차지하고 적재마다 갱신 비용만 낸다.

CREATE INDEX item_raw_lower_bigm ON item USING gin (lower(raw_text) gin_bigm_ops);

DROP INDEX item_raw_bigm;
