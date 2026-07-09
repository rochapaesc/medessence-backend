CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE COLLATION IF NOT EXISTS case_insensitive (
    provider = icu,
    locale = 'und-u-ks-level2',
    deterministic = false
);