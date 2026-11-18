\echo '--- A. transactions側の地区名(distinct)サンプル：宿河原/生田/長井を含むもの ---'
SELECT DISTINCT municipality_code, district_name
FROM realestate.transactions
WHERE district_name IN ('宿河原','生田','長井','鴨居','菅生')
ORDER BY municipality_code, district_name;

\echo '--- B. address_points側で同じ市区町村・同じ接頭辞を持つ町丁目名 ---'
SELECT municipality_code, oaza_chome_name
FROM realestate.address_points
WHERE oaza_chome_name LIKE '宿河原%'
   OR oaza_chome_name LIKE '生田%'
   OR oaza_chome_name LIKE '長井%'
   OR oaza_chome_name LIKE '鴨居%'
   OR oaza_chome_name LIKE '菅生%'
ORDER BY municipality_code, oaza_chome_name;

\echo '--- C. 一致しているdistrict_nameの例（参考：どういうケースは一致しているか） ---'
SELECT DISTINCT t.district_name
FROM realestate.transactions t
JOIN realestate.address_points ap
    ON ap.municipality_code = t.municipality_code
   AND ap.oaza_chome_name   = t.district_name
LIMIT 20;

\echo '--- D. district_nameの文字数分布と、丁目を含むかどうかの内訳 ---'
SELECT
    (district_name ~ '(丁目|条|号)$') AS looks_like_chome_suffix,
    COUNT(*) AS n
FROM realestate.transactions
GROUP BY 1;

\echo '--- E. address_points側で丁目を含む/含まないの内訳 ---'
SELECT
    (oaza_chome_name ~ '(丁目|条|号)$') AS looks_like_chome_suffix,
    COUNT(*) AS n
FROM realestate.address_points
GROUP BY 1;
