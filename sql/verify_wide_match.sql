\set ON_ERROR_STOP on

\echo '--- 1. n_source_pointsが多い順の大字（上位10件、district_nameの文字数付き） ---'
SELECT
    municipality_code,
    district_name,
    length(district_name) AS district_name_len,
    n_source_points,
    COUNT(*) AS n_transactions
FROM realestate.v_transactions_with_mesh
WHERE match_type = 'oaza_mesh_avg'
GROUP BY municipality_code, district_name, n_source_points
ORDER BY n_source_points DESC
LIMIT 10;

\echo '--- 2. n_source_points=24 の大字について、実際にマッチしたaddress_points側の地名一覧（前方一致の妥当性を目視確認） ---'
SELECT DISTINCT
    t.municipality_code,
    t.district_name AS query_name,
    ap.oaza_chome_name,
    ap.oaza_chome_code
FROM realestate.transactions t
JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND ap.oaza_chome_name LIKE (t.district_name || '%')
   AND length(ap.oaza_chome_name) > length(t.district_name)
WHERE t.trade_type = '宅地(土地と建物)'
  AND t.district_name IN (
      SELECT district_name
      FROM realestate.v_transactions_with_mesh
      WHERE match_type = 'oaza_mesh_avg' AND n_source_points = 24
      LIMIT 1
  )
ORDER BY ap.oaza_chome_code;
