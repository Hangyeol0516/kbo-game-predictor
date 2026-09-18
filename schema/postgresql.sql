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

CREATE INDEX idx_game_predictions_date ON game_predictions(prediction_date);
CREATE INDEX idx_game_results_date ON game_results(game_date);
