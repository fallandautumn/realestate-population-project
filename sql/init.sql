-- =========================================================
-- init.sql
-- PostGIS拡張の有効化とプロジェクト用スキーマ・テーブル定義
-- 対象: 神奈川県 戸建住宅取引価格 × 国勢調査メッシュ 空間結合分析
--       (横須賀市・三浦市 = 人口減少地域 / 川崎市多摩区・宮前区 = 相対的に人口が安定・増加している地域)
--
-- v2: 不動産情報ライブラリのダウンロードCSV（API経由ではなく手動DL）に合わせて
--     transactions_raw → transactions に改称し、坪単価・最寄駅情報・築年数など
--     ダウンロードCSV固有の列を追加。
-- =========================================================

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;

CREATE SCHEMA IF NOT EXISTS realestate;
SET search_path TO realestate, public;

-- ---------------------------------------------------------
-- 1. 不動産取引価格情報（国交省 不動産情報ライブラリ ダウンロードCSV由来）
--    位置情報を持たないため、後段で address_points とJOINし
--    geom（町丁目代表点）を付与する
-- ---------------------------------------------------------
DROP VIEW IF EXISTS realestate.v_transactions_with_mesh;
DROP TABLE IF EXISTS realestate.transactions_raw;

CREATE TABLE IF NOT EXISTS realestate.transactions (
    transaction_id        BIGSERIAL PRIMARY KEY,
    trade_type             TEXT,
    price_category           TEXT,
    region                     TEXT,
    municipality_code           CHAR(5)        NOT NULL,
    prefecture_name               TEXT,
    municipality_name               TEXT           NOT NULL,
    district_name                     TEXT,
    nearest_station_name                TEXT,
    nearest_station_minutes               SMALLINT,
    trade_price_yen                         BIGINT,
    price_per_tsubo                           BIGINT,
    area_sqm                                    NUMERIC(10, 2),
    unit_price_per_sqm                            BIGINT,
    land_shape                                      TEXT,
    frontage_m                                        NUMERIC(6, 2),
    total_floor_area_sqm                                NUMERIC(10, 2),
    building_year                                         SMALLINT,
    building_age_years                                      SMALLINT,
    building_structure                                        TEXT,
    use_purpose                                                 TEXT,
    future_use_purpose                                            TEXT,
    front_road_direction                                            TEXT,
    front_road_classification                                         TEXT,
    front_road_breadth_m                                                NUMERIC(6, 2),
    city_planning_zone                                                    TEXT,
    building_coverage_ratio                                                 SMALLINT,
    floor_area_ratio                                                          SMALLINT,
    transaction_period                                                          TEXT           NOT NULL,
    transaction_year                                                              SMALLINT      NOT NULL,
    transaction_quarter                                                             SMALLINT      NOT NULL,
    remarks                                                                           TEXT,
    source_file                                                                         TEXT,
    source_loaded_at                                                                      TIMESTAMPTZ    NOT NULL DEFAULT now()
);

COMMENT ON TABLE realestate.transactions IS
  '不動産情報ライブラリのダウンロードCSV（横須賀市・三浦市・川崎市多摩区・宮前区）から取り込んだ取引データ。trade_type=''宅地(土地と建物)''で戸建に絞り込み済み。位置情報なし。';

CREATE INDEX IF NOT EXISTS idx_transactions_muni
    ON realestate.transactions (municipality_code);
CREATE INDEX IF NOT EXISTS idx_transactions_district
    ON realestate.transactions (municipality_code, district_name);
CREATE INDEX IF NOT EXISTS idx_transactions_year
    ON realestate.transactions (transaction_year, transaction_quarter);
CREATE INDEX IF NOT EXISTS idx_transactions_type
    ON realestate.transactions (trade_type);

-- ---------------------------------------------------------
-- 2. 位置参照情報（国土数値情報 大字・町丁目レベル位置参照情報 令和7年版）
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS realestate.address_points (
    address_point_id     BIGSERIAL PRIMARY KEY,
    prefecture_code       CHAR(2)   NOT NULL,
    prefecture_name        TEXT,
    municipality_code       CHAR(5)   NOT NULL,
    municipality_name         TEXT,
    oaza_chome_code              TEXT,
    oaza_chome_name                TEXT      NOT NULL,
    longitude                        NUMERIC(10, 7) NOT NULL,
    latitude                           NUMERIC(10, 7) NOT NULL,
    origin_code                          TEXT,
    oaza_class_code                        TEXT,
    geom                                      GEOMETRY(Point, 4326) NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_address_points_key
    ON realestate.address_points (municipality_code, oaza_chome_name);
CREATE INDEX IF NOT EXISTS idx_address_points_geom
    ON realestate.address_points USING GIST (geom);

-- ---------------------------------------------------------
-- 3. 国勢調査 地域メッシュ（2分の1地域メッシュ=約500m）
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS realestate.census_mesh (
    mesh_code                CHAR(9)   NOT NULL,
    census_year                 SMALLINT  NOT NULL,
    population                     INTEGER,
    households                       INTEGER,
    population_aged_65_over            INTEGER,
    is_suppressed                        BOOLEAN   NOT NULL DEFAULT FALSE,
    geom                                    GEOMETRY(Polygon, 4326) NOT NULL,
    PRIMARY KEY (mesh_code, census_year)
);

CREATE INDEX IF NOT EXISTS idx_census_mesh_geom
    ON realestate.census_mesh USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_census_mesh_year
    ON realestate.census_mesh (census_year);

-- ---------------------------------------------------------
-- 4. 住所正規化関数
--    transactions.district_name と address_points.oaza_chome_name は
--    丁目の表記（漢数字「一〜九」「十」の組み合わせ ⇔ 算用数字）が
--    データソース間で一致しないため、意味的に正規化した上でJOINする。
--    全角/半角の表記揺れはPython側(unicodedata.normalize("NFKC"))で
--    ロード時に統一済みのため、ここでは漢数字→算用数字の変換のみ行う。
-- ---------------------------------------------------------
CREATE OR REPLACE FUNCTION realestate.kanji_digit_to_number(kanji_digit text)
RETURNS int AS $$
    SELECT CASE kanji_digit
        WHEN '〇' THEN 0
        WHEN '一' THEN 1
        WHEN '二' THEN 2
        WHEN '三' THEN 3
        WHEN '四' THEN 4
        WHEN '五' THEN 5
        WHEN '六' THEN 6
        WHEN '七' THEN 7
        WHEN '八' THEN 8
        WHEN '九' THEN 9
        ELSE NULL
    END;
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION realestate.kanji_number_to_int(kanji_num text)
RETURNS int AS $$
DECLARE
    ju_pos int;
    before_ju text;
    after_ju text;
    tens int;
    ones int;
BEGIN
    IF kanji_num IS NULL OR kanji_num = '' THEN
        RETURN NULL;
    END IF;

    IF kanji_num ~ '^[0-9]+$' THEN
        RETURN kanji_num::int;
    END IF;

    ju_pos := position('十' in kanji_num);

    IF ju_pos = 0 THEN
        RETURN realestate.kanji_digit_to_number(kanji_num);
    END IF;

    before_ju := substring(kanji_num from 1 for ju_pos - 1);
    after_ju  := substring(kanji_num from ju_pos + 1);

    IF before_ju = '' THEN
        tens := 1;
    ELSE
        tens := realestate.kanji_digit_to_number(before_ju);
    END IF;

    IF after_ju = '' THEN
        ones := 0;
    ELSE
        ones := realestate.kanji_digit_to_number(after_ju);
    END IF;

    IF tens IS NULL OR ones IS NULL THEN
        RETURN NULL;
    END IF;

    RETURN tens * 10 + ones;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION realestate.normalize_address_name(raw_name text)
RETURNS text AS $$
DECLARE
    m text[];
    base_name text;
    chome_part text;
    chome_num int;
BEGIN
    IF raw_name IS NULL THEN
        RETURN NULL;
    END IF;

    m := regexp_match(raw_name, '^(.*?)([0-9〇一二三四五六七八九十]+)丁目$');

    IF m IS NULL THEN
        RETURN raw_name;
    END IF;

    base_name  := m[1];
    chome_part := m[2];
    chome_num  := realestate.kanji_number_to_int(chome_part);

    IF chome_num IS NULL THEN
        RETURN raw_name;
    END IF;

    RETURN base_name || chome_num::text || '丁目';
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE VIEW realestate.v_address_points_normalized AS
SELECT
    ap.*,
    realestate.normalize_address_name(ap.oaza_chome_name) AS oaza_chome_name_normalized
FROM realestate.address_points ap;

-- ---------------------------------------------------------
-- 5. 分析ビュー：取引データにメッシュの人口増減率を付与
--    正規化後の地区名でaddress_pointsとJOINする。
-- ---------------------------------------------------------
DROP VIEW IF EXISTS realestate.v_transactions_with_mesh;
CREATE VIEW realestate.v_transactions_with_mesh AS
SELECT
    t.transaction_id,
    t.municipality_code,
    t.municipality_name,
    t.district_name,
    t.trade_type,
    t.trade_price_yen,
    t.area_sqm,
    t.building_year,
    t.building_age_years,
    t.nearest_station_name,
    t.nearest_station_minutes,
    t.city_planning_zone,
    t.building_coverage_ratio,
    t.floor_area_ratio,
    t.transaction_year,
    t.transaction_quarter,
    ap.oaza_chome_name                   AS matched_oaza_chome_name,
    ap.geom                              AS point_geom,
    m2020.mesh_code,
    m2020.population                     AS population_2020,
    m2015.population                     AS population_2015,
    CASE
        WHEN m2015.population IS NOT NULL AND m2015.population > 0
             AND NOT m2015.is_suppressed AND NOT m2020.is_suppressed
        THEN (m2020.population - m2015.population)::NUMERIC / m2015.population
        ELSE NULL
    END                                    AS population_change_rate_15_20
FROM realestate.transactions t
JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name)
JOIN realestate.census_mesh m2020
    ON m2020.census_year = 2020
   AND ST_Within(ap.geom, m2020.geom)
LEFT JOIN realestate.census_mesh m2015
    ON m2015.census_year = 2015
   AND m2015.mesh_code = m2020.mesh_code
WHERE t.trade_type = '宅地(土地と建物)';
