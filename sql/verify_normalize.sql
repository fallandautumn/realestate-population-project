\echo '--- 1. 正規化前後のJOIN成功率比較 ---'
SELECT
    COUNT(*) AS total_transactions,
    COUNT(ap_raw.address_point_id)  AS matched_raw,
    ROUND(100.0 * COUNT(ap_raw.address_point_id) / COUNT(*), 1)  AS match_rate_raw_pct,
    COUNT(ap_norm.address_point_id) AS matched_normalized,
    ROUND(100.0 * COUNT(ap_norm.address_point_id) / COUNT(*), 1) AS match_rate_normalized_pct
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap_raw
    ON ap_raw.municipality_code = t.municipality_code
   AND ap_raw.oaza_chome_name   = t.district_name
LEFT JOIN realestate.address_points ap_norm
    ON ap_norm.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap_norm.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name);

\echo '--- 2. 市区町村別の正規化後マッチ率 ---'
SELECT
    t.municipality_name,
    COUNT(*) AS n_transactions,
    COUNT(ap.address_point_id) AS n_matched,
    ROUND(100.0 * COUNT(ap.address_point_id) / COUNT(*), 1) AS match_rate_pct
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name)
GROUP BY t.municipality_name
ORDER BY t.municipality_name;

\echo '--- 3. 正規化後もマッチしない地区名（残存分、上位30件） ---'
SELECT t.municipality_name, t.district_name,
       realestate.normalize_address_name(t.district_name) AS normalized,
       COUNT(*) AS n
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name)
WHERE ap.address_point_id IS NULL
GROUP BY t.municipality_name, t.district_name
ORDER BY n DESC
LIMIT 30;

\echo '--- 4. 正規化後の最終ビュー(v_transactions_with_mesh)充足件数 ---'
SELECT COUNT(*) AS n_total,
       COUNT(*) FILTER (WHERE population_change_rate_15_20 IS NOT NULL) AS n_rate_available
FROM realestate.v_transactions_with_mesh;
