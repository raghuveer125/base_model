-- Trading_Plug&Play — Postgres schema (Phase 1)
-- 2-day retention enforced by scripts/cli.py retention command.

CREATE TABLE IF NOT EXISTS index_ticks (
    id            BIGSERIAL PRIMARY KEY,
    index         TEXT        NOT NULL,
    ltp           DOUBLE PRECISION NOT NULL,
    ts_exchange   BIGINT      NOT NULL,
    ts_received   BIGINT      NOT NULL,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_index_ticks_index_ts ON index_ticks (index, ts DESC);
CREATE INDEX IF NOT EXISTS ix_index_ticks_ts       ON index_ticks (ts);

CREATE TABLE IF NOT EXISTS option_chain_data (
    id            BIGSERIAL PRIMARY KEY,
    index         TEXT        NOT NULL,
    strike        INTEGER     NOT NULL,
    option_type   CHAR(2)     NOT NULL CHECK (option_type IN ('CE','PE')),
    expiry        DATE        NOT NULL,
    ltp           DOUBLE PRECISION NOT NULL,
    oi            BIGINT      NOT NULL DEFAULT 0,
    oi_change     BIGINT      NOT NULL DEFAULT 0,
    iv            DOUBLE PRECISION,
    ts_exchange   BIGINT      NOT NULL,
    ts_received   BIGINT      NOT NULL,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_option_chain_lookup ON option_chain_data (index, expiry, strike, option_type, ts DESC);
CREATE INDEX IF NOT EXISTS ix_option_chain_ts     ON option_chain_data (ts);

CREATE TABLE IF NOT EXISTS index_candles (
    id            BIGSERIAL PRIMARY KEY,
    index         TEXT        NOT NULL,
    timeframe     TEXT        NOT NULL CHECK (timeframe IN ('1m','5m','15m')),
    open_ts       TIMESTAMPTZ NOT NULL,
    close_ts      TIMESTAMPTZ NOT NULL,
    open          DOUBLE PRECISION NOT NULL,
    high          DOUBLE PRECISION NOT NULL,
    low           DOUBLE PRECISION NOT NULL,
    close         DOUBLE PRECISION NOT NULL,
    volume        BIGINT      NOT NULL DEFAULT 0,
    tick_count    INTEGER     NOT NULL DEFAULT 0,
    UNIQUE (index, timeframe, open_ts)
);
CREATE INDEX IF NOT EXISTS ix_index_candles_lookup ON index_candles (index, timeframe, open_ts DESC);
CREATE INDEX IF NOT EXISTS ix_index_candles_ts     ON index_candles (open_ts);
