ALTER TABLE revenue_transactions
    ADD COLUMN synced_at timestamptz NOT NULL DEFAULT now();
