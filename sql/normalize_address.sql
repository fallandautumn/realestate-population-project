\set ON_ERROR_STOP on

-- =========================================================
-- normalize_address.sql
-- transactions.district_name と address_points.oaza_chome_name の
-- 丁目表記揺れ（漢数字「一〜九」「十」の組み合わせ ⇔ 算用数字）を
-- 吸収するための正規化関数・ビューの追加マイグレーション。
--
-- 全角/半角の数字表記揺れはPython側（unicodedata.normalize("NFKC")）で
-- 既に統一済みのため、ここで対応するのは「漢数字表記 → 算用数字表記」の
-- 意味的な変換のみで良い。
--
-- 既存データ（transactions / address_points / census_mesh）を削除せず、
-- 稼働中のDBに対してそのまま適用できるマイグレーションとして作成。
-- =========================================================

SET search_path TO realestate, public;

-- ---------------------------------------------------------
-- 1. 漢数字1文字（〇/一〜九）を整数へ変換する補助関数
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

COMMENT ON FUNCTION realestate.kanji_digit_to_number(text) IS
  '漢数字1文字（〇/一〜九）を0〜9の整数に変換する。該当しない文字はNULLを返す。';

-- ---------------------------------------------------------
-- 2. 漢数字表記（1〜99の範囲、「十」を含む複合表記に対応）を整数へ変換する関数
--    例: '一'→1, '十'→10, '十五'→15, '二十'→20, '二十三'→23
--    既に算用数字の場合はそのままintにキャストして返す。
-- ---------------------------------------------------------
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

    -- 既に算用数字（半角）であればそのまま変換
    IF kanji_num ~ '^[0-9]+$' THEN
        RETURN kanji_num::int;
    END IF;

    ju_pos := position('十' in kanji_num);

    IF ju_pos = 0 THEN
        -- 「十」を含まない1桁の漢数字（〇/一〜九）
        RETURN realestate.kanji_digit_to_number(kanji_num);
    END IF;

    before_ju := substring(kanji_num from 1 for ju_pos - 1);
    after_ju  := substring(kanji_num from ju_pos + 1);

    IF before_ju = '' THEN
        tens := 1;  -- 「十」単独 = 10の位が1
    ELSE
        tens := realestate.kanji_digit_to_number(before_ju);
    END IF;

    IF after_ju = '' THEN
        ones := 0;
    ELSE
        ones := realestate.kanji_digit_to_number(after_ju);
    END IF;

    IF tens IS NULL OR ones IS NULL THEN
        RETURN NULL;  -- 想定外の文字を含む場合は変換不能としてNULL
    END IF;

    RETURN tens * 10 + ones;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION realestate.kanji_number_to_int(text) IS
  '漢数字（1〜99、「十」を含む複合表記）または算用数字の文字列を整数に変換する。';

-- ---------------------------------------------------------
-- 3. 地区名・町丁目名の末尾「◯丁目」を「(算用数字)丁目」の正準形へ正規化する関数
--    「丁目」を含まない地区名（例: 三崎町、坂本町）はそのまま返す。
-- ---------------------------------------------------------
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
        RETURN raw_name;  -- 丁目を含まない地区名はそのまま返す
    END IF;

    base_name  := m[1];
    chome_part := m[2];
    chome_num  := realestate.kanji_number_to_int(chome_part);

    IF chome_num IS NULL THEN
        RETURN raw_name;  -- 変換できない場合は安全側として元の文字列を返す
    END IF;

    RETURN base_name || chome_num::text || '丁目';
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION realestate.normalize_address_name(text) IS
  '地区名・町丁目名の末尾の丁目番号（漢数字/算用数字）を算用数字の正準形に正規化する。
   district_name(transactions)とoaza_chome_name(address_points)の表記揺れ吸収用。';

-- ---------------------------------------------------------
-- 4. 正規化済み町丁目名を付与したaddress_pointsのビュー（デバッグ・再利用用）
-- ---------------------------------------------------------
CREATE OR REPLACE VIEW realestate.v_address_points_normalized AS
SELECT
    ap.*,
    realestate.normalize_address_name(ap.oaza_chome_name) AS oaza_chome_name_normalized
FROM realestate.address_points ap;

-- ---------------------------------------------------------
-- 5. v_transactions_with_mesh を正規化後の名称でJOINするよう再定義
--    （既存ビューをCREATE OR REPLACEで置き換えるためデータの再投入は不要）
-- ---------------------------------------------------------
CREATE OR REPLACE VIEW realestate.v_transactions_with_mesh AS
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
    ap.geom                              AS point_geom,
    m2020.mesh_code,
    m2020.population                     AS population_2020,
    m2015.population                     AS population_2015,
    CASE
        WHEN m2015.population IS NOT NULL AND m2015.population > 0
             AND NOT m2015.is_suppressed AND NOT m2020.is_suppressed
        THEN (m2020.population - m2015.population)::NUMERIC / m2015.population
        ELSE NULL
    END                                    AS population_change_rate_15_20,
    ap.oaza_chome_name                     AS matched_oaza_chome_name
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
