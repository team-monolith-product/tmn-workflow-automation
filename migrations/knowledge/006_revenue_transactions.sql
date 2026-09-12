-- 재생성 가능한 매출장 캐시. 배치가 전체를 원자적으로 교체한다.
CREATE TABLE revenue_transactions (
    year integer NOT NULL,
    issued_on date,
    status text NOT NULL CHECK (status IN ('issued', 'planned')),
    customer text NOT NULL,
    item text NOT NULL,
    quantity numeric,
    unit_price numeric,
    amount numeric NOT NULL,
    tax numeric,
    total numeric,
    category text NOT NULL,
    subcategory text,
    note text,
    school_level text,
    edu_office text,
    budget_sources text[],
    terms text[],
    program_name text,
    program_client text,
    program_budget numeric,
    program_our_revenue numeric,
    program_stage text,
    CHECK ((status = 'issued' AND issued_on IS NOT NULL)
        OR (status = 'planned' AND issued_on IS NULL))
);

COMMENT ON TABLE revenue_transactions IS
'매출장 일간 동기화 캐시. 한 행은 매출장 거래 한 행. 금액은 공급가액 amount로 집계한다.';
COMMENT ON COLUMN revenue_transactions.year IS '발행일 연도와 다를 수 있는 매출장 귀속연도';
COMMENT ON COLUMN revenue_transactions.budget_sources IS '고객의 CRM 딜에서 모은 복수 예산출처. 개별 거래의 확정 예산이 아님';
COMMENT ON COLUMN revenue_transactions.terms IS '고객의 CRM 딜에서 모은 사용학기';
COMMENT ON COLUMN revenue_transactions.program_budget IS '사업 전체 예산. 여러 거래에 반복되므로 합산 금지';
COMMENT ON COLUMN revenue_transactions.program_our_revenue IS 'CRM의 사업 전체 우리 몫. 매출장 거래 매출과 별개이며 합산 금지';
