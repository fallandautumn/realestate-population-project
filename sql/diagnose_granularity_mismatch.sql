\set ON_ERROR_STOP on

-- =========================================================
-- diagnose_granularity_mismatch.sql
-- 正規化（漢数字→算用数字）適用後も907件のまま変化しなかった原因として、
-- 「transactions.district_name が丁目を含まない大字レベルの表記である一方、
--   address_points.oaza_chome_name は丁目分割済み大字については丁目単位の
--   代表点しか持たない」という粒度不一致仮説を検証する。
-- =========================================================

\echo '--- 1. 正規化JOINで一致しない取引件数（全体） ---'
SELECT COUNT(*) AS n_unmatched
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name)
WHERE t.trade_type = '宅地(土地と建物)'
  AND ap.address_point_id IS NULL;

\echo '--- 2. 不一致件数の内訳：district_nameが「丁目」を含むか否か ---'
SELECT
    (t.district_name ~ '[0-9〇一二三四五六七八九十]+丁目$') AS has_chome_suffix,
    COUNT(*) AS n_unmatched
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name)
WHERE t.trade_type = '宅地(土地と建物)'
  AND ap.address_point_id IS NULL
GROUP BY 1
ORDER BY 1;

\echo '--- 3. 「丁目なし・不一致」の地区名のうち、'
\echo '       同一市区町村のaddress_pointsに同名接頭辞の丁目点が実在するか（粒度不一致の直接証拠） ---'
SELECT
    t.municipality_code,
    t.district_name,
    COUNT(DISTINCT t.transaction_id)                 AS n_transactions,
    COUNT(DISTINCT ap.address_point_id)               AS n_matching_chome_points
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND ap.oaza_chome_name LIKE t.district_name || '%'
   AND ap.oaza_chome_name <> t.district_name
WHERE t.trade_type = '宅地(土地と建物)'
  AND NOT (t.district_name ~ '[0-9〇一二三四五六七八九十]+丁目$')
  AND NOT EXISTS (
      SELECT 1 FROM realestate.address_points ap2
      WHERE ap2.municipality_code = t.municipality_code
        AND realestate.normalize_address_name(ap2.oaza_chome_name)
            = realestate.normalize_address_name(t.district_name)
  )
GROUP BY t.municipality_code, t.district_name
ORDER BY n_transactions DESC
LIMIT 30;

\echo '--- 4. 「丁目あり」だが依然不一致の地区名（表記ゆれ以外の原因の可能性、上位20件） ---'
SELECT
    t.municipality_code,
    t.district_name,
    realestate.normalize_address_name(t.district_name) AS normalized,
    COUNT(*) AS n_unmatched
FROM realestate.transactions t
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND realestate.normalize_address_name(ap.oaza_chome_name)
       = realestate.normalize_address_name(t.district_name)
WHERE t.trade_type = '宅地(土地と建物)'
  AND ap.address_point_id IS NULL
  AND (t.district_name ~ '[0-9〇一二三四五六七八九十]+丁目$')
GROUP BY t.municipality_code, t.district_name
ORDER BY n_unmatched DESC
LIMIT 20;
