\set ON_ERROR_STOP on

\echo '--- 1. 三浦市(14210)のaddress_points全件（DB格納値そのもの） ---'
SELECT oaza_chome_name, length(oaza_chome_name) AS char_len, oaza_chome_code
FROM realestate.address_points
WHERE municipality_code = '14210'
ORDER BY oaza_chome_code;

\echo '--- 2. 未結合3地区（南下浦町・三崎町・初声町）と前方一致・部分一致するaddress_points ---'
SELECT
    q.district_name AS query_name,
    ap.oaza_chome_name,
    ap.oaza_chome_code
FROM (VALUES ('南下浦町'), ('三崎町'), ('初声町')) AS q(district_name)
LEFT JOIN realestate.address_points ap
    ON ap.municipality_code = '14210'
   AND (ap.oaza_chome_name LIKE '%' || q.district_name || '%'
        OR q.district_name LIKE '%' || ap.oaza_chome_name || '%')
ORDER BY q.district_name, ap.oaza_chome_code;

\echo '--- 3. 未結合3地区に該当する取引の trade_price_yen 等サンプル（データの性質確認） ---'
SELECT municipality_code, district_name, transaction_year, trade_price_yen, area_sqm
FROM realestate.transactions
WHERE municipality_code = '14210'
  AND district_name IN ('南下浦町', '三崎町', '初声町')
ORDER BY district_name, transaction_year
LIMIT 10;
