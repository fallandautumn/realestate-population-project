\echo '--- 1. 各テーブル件数 ---'
SELECT 'transactions' AS table_name, COUNT(*) FROM realestate.transactions
UNION ALL
SELECT 'address_points', COUNT(*) FROM realestate.address_points
UNION ALL
SELECT 'census_mesh_2015', COUNT(*) FROM realestate.census_mesh WHERE census_year = 2015
UNION ALL
SELECT 'census_mesh_2020', COUNT(*) FROM realestate.census_mesh WHERE census_year = 2020;

\echo '--- 2. 市区町村別 戸建取引件数 ---'
SELECT municipality_name, COUNT(*) AS n,
       MIN(transaction_year) AS from_year, MAX(transaction_year) AS to_year
FROM realestate.transactions
GROUP BY municipality_name
ORDER BY municipality_name;

\echo '--- 3. address_pointsとのJOIN成功率 ---'
SELECT
    COUNT(*) AS total_transactions,
    COUNT(ap.address_point_id) AS matched,
    ROUND(100.0 * COUNT(ap.address_point_id) / COUNT(*), 1) AS match_rate_pct
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND ap.oaza_chome_name   = t.district_name;

\echo '--- 4. JOIN失敗した地区名 上位20件 ---'
SELECT t.municipality_name, t.district_name, COUNT(*) AS n
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND ap.oaza_chome_name   = t.district_name
WHERE ap.address_point_id IS NULL
GROUP BY t.municipality_name, t.district_name
ORDER BY n DESC
LIMIT 20;

\echo '--- 5. 最終ビュー(v_transactions_with_mesh)充足件数 ---'
SELECT COUNT(*) FROM realestate.v_transactions_with_mesh;

\echo '--- 6. 人口増減率が算出できた件数 ---'
SELECT COUNT(*) AS n_with_rate,
       COUNT(*) FILTER (WHERE population_change_rate_15_20 IS NOT NULL) AS n_rate_available
FROM realestate.v_transactions_with_mesh;
