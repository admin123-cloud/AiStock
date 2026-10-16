-- Additive only: no mutation/materialization or old quantity rewrite.
ALTER TABLE stock.kline_minute_5 ADD COLUMN IF NOT EXISTS volume_exact Nullable(Decimal256(45)) DEFAULT NULL;

CREATE OR REPLACE VIEW stock.kline_minute_5_precise AS
SELECT code, datetime, open, high, low, close,
       coalesce(volume_exact, toDecimal256(volume, 45)) AS volume,
       amount, created_at, id, volume_exact
FROM stock.kline_minute_5 FINAL;
