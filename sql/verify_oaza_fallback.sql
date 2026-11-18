\set ON_ERROR_STOP on

\echo '--- 1. match_type別の件数・全体(7518件)に占める割合 ---'
SELECT
    match_type,
    COUNT(*) AS n,
    ROUND(
        100.0 * COUNT(*)
        / (SELECT COUNT(*) FROM realestate.transactions WHERE trade_type = '宅地(土地と建物)')
    , 1) AS pct_of_total
FROM realestate.v_transactions_with_mesh
GROUP BY match_type
ORDER BY n DESC;

\echo '--- 2. 従来未結合だった6,611件のうち、案Bで救済できた件数・救済率 ---'
SELECT
    COUNT(*) FILTER (WHERE match_type = 'oaza_mesh_avg')  AS n_rescued,
    COUNT(*) FILTER (WHERE match_type = 'unmatched')       AS n_still_unmatched,
    COUNT(*)                                                AS n_total_previously_unmatched,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE match_type = 'oaza_mesh_avg')
        / NULLIF(COUNT(*), 0)
    , 1)                                                     AS rescue_rate_pct
FROM realestate.v_transactions_with_mesh
WHERE match_type IN ('oaza_mesh_avg', 'unmatched');

\echo '--- 3. population_change_rate_15_20が実際に算出できた件数（秘匿メッシュ除外後の最終充足率） ---'
SELECT
    COUNT(*)                                                          AS n_total,
    COUNT(*) FILTER (WHERE population_change_rate_15_20 IS NOT NULL)  AS n_rate_available,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE population_change_rate_15_20 IS NOT NULL)
        / COUNT(*)
    , 1)                                                                AS rate_available_pct
FROM realestate.v_transactions_with_mesh;

\echo '--- 4. oaza_mesh_avgで使用された丁目点数の分布（近似の粗さの目安） ---'
SELECT
    n_source_points,
    COUNT(*) AS n_transactions
FROM realestate.v_transactions_with_mesh
WHERE match_type = 'oaza_mesh_avg'
GROUP BY n_source_points
ORDER BY n_source_points;

\echo '--- 5. なお救済できなかった大字（上位20件、原因切り分け用） ---'
SELECT
    municipality_code,
    district_name,
    COUNT(*) AS n_transactions
FROM realestate.v_transactions_with_mesh
WHERE match_type = 'unmatched'
GROUP BY municipality_code, district_name
ORDER BY n_transactions DESC
LIMIT 20;
