\set ON_ERROR_STOP on

-- =========================================================
-- oaza_mesh_fallback.sql
-- 【案B】大字レベルの取引（district_nameに丁目を含まない取引）について、
-- 当該大字に属する全丁目代表点(address_points)がそれぞれ属するメッシュの
-- 人口統計値を平均し、メッシュ情報の近似値として付与するフォールバック処理。
--
-- 採用根拠:
--   案A（丁目点の緯度経度を平均した疑似中心点を作り、その1点で空間結合する）は
--   大字の形状次第で疑似中心点が実際のメッシュ外・無関係なメッシュに位置する
--   リスクがある。
--   案Bは実在する丁目点がそれぞれ属する実在のメッシュの人口統計値を直接平均する
--   ため、架空の座標を経由せず、回帰分析の説明変数（人口増減率等）に含まれる
--   測定誤差を小さく抑えられる。
--
-- 丁目レベルで完全一致する取引（既存ロジック）はそのまま優先し、
-- それでも一致しない取引にのみ本フォールバックを適用する。
-- 列構成を大きく変更するため、CREATE OR REPLACE VIEW ではなく
-- DROP VIEW + CREATE VIEW で安全に再定義する
-- （CREATE OR REPLACE VIEWは列の末尾追加しか許可せず、過去に
--   "cannot change name of view column" エラーの原因になったため）。
-- =========================================================

SET search_path TO realestate, public;

DROP VIEW IF EXISTS realestate.v_transactions_with_mesh;

CREATE VIEW realestate.v_transactions_with_mesh AS
WITH exact_match AS (
    -- 丁目レベル（または丁目を含まない代表点名がそのまま一致する）で
    -- address_pointsと完全一致する取引：既存の空間結合ロジック
    SELECT
        t.transaction_id,
        ap.oaza_chome_name                          AS matched_oaza_chome_name,
        ap.geom                                     AS point_geom,
        m2020.mesh_code                             AS mesh_code,
        m2020.population                            AS population_2020,
        m2015.population                            AS population_2015,
        CASE
            WHEN m2015.population IS NOT NULL AND m2015.population > 0
                 AND NOT m2015.is_suppressed AND NOT m2020.is_suppressed
            THEN (m2020.population - m2015.population)::NUMERIC / m2015.population
            ELSE NULL
        END                                          AS population_change_rate_15_20,
        CASE
            WHEN m2020.population IS NOT NULL AND m2020.population > 0
                 AND NOT m2020.is_suppressed
            THEN m2020.population_aged_65_over::NUMERIC / m2020.population
            ELSE NULL
        END                                          AS aging_rate_2020,
        1                                             AS n_source_points,
        'chome_exact'::text                          AS match_type
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
    WHERE t.trade_type = '宅地(土地と建物)'
),
fallback_source AS (
    -- 丁目レベルで一致しない大字レベルの取引について、
    -- district_nameより長い前方一致を持つ配下の代表点をすべて列挙し、
    -- それぞれが属するメッシュの人口統計値を取得する。
    -- 当初は「(漢数字/算用数字)+丁目」の丁目パターンのみに限定していたが、
    -- 三浦市（14210）の南下浦町・三崎町・初声町で「大字+字（あざ）」の
    -- 複合名（例: 南下浦町金田、三崎町小網代）による粒度不一致が別途確認されたため、
    -- 丁目パターンを包含する前方一致条件に一般化した。
    SELECT
        t.transaction_id,
        ap.address_point_id,
        m2020.mesh_code                             AS mesh_code,
        m2020.population                            AS population_2020,
        m2020.is_suppressed                         AS is_suppressed_2020,
        m2020.population_aged_65_over               AS population_aged_65_over_2020,
        m2015.population                            AS population_2015,
        m2015.is_suppressed                         AS is_suppressed_2015
    FROM realestate.transactions t
    JOIN realestate.address_points ap
        ON ap.municipality_code = t.municipality_code
       AND ap.oaza_chome_name LIKE (t.district_name || '%')
       AND length(ap.oaza_chome_name) > length(t.district_name)
    JOIN realestate.census_mesh m2020
        ON m2020.census_year = 2020
       AND ST_Within(ap.geom, m2020.geom)
    LEFT JOIN realestate.census_mesh m2015
        ON m2015.census_year = 2015
       AND m2015.mesh_code = m2020.mesh_code
    WHERE t.trade_type = '宅地(土地と建物)'
      AND t.transaction_id NOT IN (SELECT transaction_id FROM exact_match)
),
fallback_agg AS (
    -- 丁目点1件を1サンプルとして平均する。
    -- 同一メッシュに複数の丁目点が属する場合はそのメッシュの値がより強く反映される。
    -- これは、そのメッシュが大字内で占める（丁目数ベースの）比重を近似する
    -- 簡易的な重み付けとして機能する。
    SELECT
        transaction_id,
        NULL::geometry(Point, 4326)                 AS point_geom,
        NULL::CHAR(9)                                AS mesh_code,
        AVG(population_2020)                         AS population_2020,
        AVG(population_2015)                         AS population_2015,
        AVG(
            CASE
                WHEN population_2015 IS NOT NULL AND population_2015 > 0
                     AND NOT is_suppressed_2015 AND NOT is_suppressed_2020
                THEN (population_2020 - population_2015)::NUMERIC / population_2015
                ELSE NULL
            END
        )                                              AS population_change_rate_15_20,
        AVG(
            CASE
                WHEN population_2020 IS NOT NULL AND population_2020 > 0
                     AND NOT is_suppressed_2020
                THEN population_aged_65_over_2020::NUMERIC / population_2020
                ELSE NULL
            END
        )                                              AS aging_rate_2020,
        COUNT(DISTINCT address_point_id)                AS n_source_points,
        'oaza_mesh_avg'::text                           AS match_type
    FROM fallback_source
    GROUP BY transaction_id
)
SELECT
    t.transaction_id,
    t.municipality_code,
    t.municipality_name,
    t.district_name,
    em.matched_oaza_chome_name                        AS matched_oaza_chome_name,
    COALESCE(em.match_type, fa.match_type, 'unmatched') AS match_type,
    COALESCE(em.n_source_points, fa.n_source_points)    AS n_source_points,
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
    COALESCE(em.point_geom, fa.point_geom)              AS point_geom,
    COALESCE(em.mesh_code, fa.mesh_code)                 AS mesh_code,
    COALESCE(em.population_2020, fa.population_2020)     AS population_2020,
    COALESCE(em.population_2015, fa.population_2015)     AS population_2015,
    COALESCE(em.population_change_rate_15_20, fa.population_change_rate_15_20) AS population_change_rate_15_20,
    COALESCE(em.aging_rate_2020, fa.aging_rate_2020)      AS aging_rate_2020
FROM realestate.transactions t
LEFT JOIN exact_match em ON em.transaction_id = t.transaction_id
LEFT JOIN fallback_agg fa ON fa.transaction_id = t.transaction_id
WHERE t.trade_type = '宅地(土地と建物)';

COMMENT ON VIEW realestate.v_transactions_with_mesh IS
  '取引データに人口統計を付与する統合ビュー。
   match_type=''chome_exact'': 丁目レベルでaddress_pointsと完全一致した取引（既存ロジック）。
   match_type=''oaza_mesh_avg'': 大字レベルの表記しか持たない取引について、
     配下の全丁目代表点が属する各メッシュの人口統計値を平均した近似値（案B）。
   match_type=''unmatched'': どちらにも該当せず人口統計値を付与できなかった取引。
   n_source_points: 人口統計値の算出に使用した丁目代表点の数
     （chome_exactは常に1、oaza_mesh_avgは平均対象の丁目点数、unmatchedはNULL）。';
