CREATE TABLE analysis_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prediction_date DATE NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL CHECK (lineup_status IN ('projected', 'confirmed')),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (prediction_date, model_version, lineup_status)
);

CREATE TABLE game_predictions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prediction_date DATE NOT NULL,
    game_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL CHECK (lineup_status IN ('projected', 'confirmed')),
    created_at TIMESTAMPTZ NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_probability NUMERIC(6,5) NOT NULL CHECK (away_probability BETWEEN 0 AND 1),
    home_probability NUMERIC(6,5) NOT NULL CHECK (home_probability BETWEEN 0 AND 1),
    predicted_winner TEXT NOT NULL,
    payload JSONB,
    UNIQUE (prediction_date, game_id, model_version, lineup_status)
);

CREATE TABLE game_results (
    game_id TEXT PRIMARY KEY,
    game_date DATE NOT NULL,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_score SMALLINT NOT NULL CHECK (away_score >= 0),
    home_score SMALLINT NOT NULL CHECK (home_score >= 0),
    winner TEXT,
    completed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE value_bet_predictions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prediction_date DATE NOT NULL,
    game_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL CHECK (lineup_status IN ('projected', 'confirmed')),
    created_at TIMESTAMPTZ NOT NULL,
    favorite_team TEXT NOT NULL,
    underdog_team TEXT NOT NULL,
    underdog_odds NUMERIC(8,3) NOT NULL CHECK (underdog_odds > 1),
    model_probability NUMERIC(6,5) NOT NULL CHECK (model_probability BETWEEN 0 AND 1),
    market_probability NUMERIC(6,5) NOT NULL CHECK (market_probability BETWEEN 0 AND 1),
    expected_return NUMERIC(8,5) NOT NULL,
    return_advantage NUMERIC(8,5) NOT NULL,
    bookmaker TEXT NOT NULL,
    bookmaker_count SMALLINT NOT NULL DEFAULT 1 CHECK (bookmaker_count >= 0),
    market_age_minutes NUMERIC(8,2),
    market_quality_passed BOOLEAN NOT NULL DEFAULT TRUE,
    betting_open BOOLEAN NOT NULL DEFAULT TRUE,
    market_updated_at TIMESTAMPTZ,
    commence_at TIMESTAMPTZ,
    recommended BOOLEAN NOT NULL,
    UNIQUE (prediction_date, game_id, model_version, lineup_status)
);

CREATE INDEX idx_game_predictions_date ON game_predictions(prediction_date);
CREATE INDEX idx_game_results_date ON game_results(game_date);
CREATE INDEX idx_value_bets_date ON value_bet_predictions(prediction_date);

CREATE TABLE collector_odds_slots (
    prediction_date DATE NOT NULL,
    slot TEXT NOT NULL CHECK (slot IN ('morning', 'pregame', 'closing')),
    claimed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (prediction_date, slot)
);

CREATE TABLE prediction_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    prediction_date DATE NOT NULL,
    game_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    lineup_status TEXT NOT NULL CHECK (lineup_status IN ('projected', 'confirmed')),
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    UNIQUE (prediction_date, game_id, model_version, lineup_status, created_at)
);
CREATE INDEX idx_prediction_events_game ON prediction_events(prediction_date, game_id, created_at);
