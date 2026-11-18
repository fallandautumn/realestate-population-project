"""
load_transactions.py

不動産情報ライブラリの「ダウンロード」機能（Web UI経由、API不使用）で取得した
戸建取引価格CSV（data/raw_transactions/*.csv）を読み込み、
realestate.transactions に一括投入する。

対象CSV（確認済み）:
    data/raw_transactions/Kanagawa Prefecture_Yokosuka City_20211_20261.csv
    data/raw_transactions/Kanagawa Prefecture_Miura City_20211_20261.csv
    data/raw_transactions/Kanagawa Prefecture_Tama Ward_20211_20261.csv
    data/raw_transactions/Kanagawa Prefecture_Miyamae Ward_20211_20261.csv

CSVの実ヘッダー（CP932エンコード、カンマ区切り、ダブルクォート囲み）:
    種類,価格情報区分,地域,市区町村コード,都道府県名,市区町村名,地区名,
    最寄駅：名称,最寄駅：距離（分）,取引価格（総額）,坪単価,面積（㎡）,
    取引価格（㎡単価）,土地の形状,間口,延床面積（㎡）,建築年,建物の構造,
    用途,今後の利用目的,前面道路：方位,前面道路：種類,前面道路：幅員（ｍ）,
    都市計画,建ぺい率（％）,容積率（％）,取引時期,取引の事情等

APIを一切使用しない。ネットワーク通信は行わない。
"""

import glob
import os
import re
import unicodedata

import pandas as pd
import psycopg2
import psycopg2.extras

# データの置き場所: 既定はリポジトリ直下の data/（このファイルの1つ上の階層）。
# コンテナ内では /home/jovyan/work/data、ホスト直実行でも同じ相対位置で解決される。
# 別の場所に置く場合のみ環境変数 DATA_ROOT で上書きする。
DATA_ROOT = os.environ.get(
    "DATA_ROOT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
)
RAW_TRANSACTIONS_DIR = os.path.join(DATA_ROOT, "raw_transactions")
TARGET_TRADE_TYPE = "宅地(土地と建物)"

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "db"),
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ.get("DB_NAME", "realestate"),
    "user": os.environ.get("DB_USER", "analyst"),
    "password": os.environ.get("DB_PASSWORD", "analyst_pw"),
}

JP_TO_EN_COLUMNS = {
    "種類": "trade_type",
    "価格情報区分": "price_category",
    "地域": "region",
    "市区町村コード": "municipality_code",
    "都道府県名": "prefecture_name",
    "市区町村名": "municipality_name",
    "地区名": "district_name",
    "最寄駅：名称": "nearest_station_name",
    "最寄駅：距離（分）": "nearest_station_minutes",
    "取引価格（総額）": "trade_price_yen",
    "坪単価": "price_per_tsubo",
    "面積（㎡）": "area_sqm",
    "取引価格（㎡単価）": "unit_price_per_sqm",
    "土地の形状": "land_shape",
    "間口": "frontage_m",
    "延床面積（㎡）": "total_floor_area_sqm",
    "建築年": "building_year_raw",
    "建物の構造": "building_structure",
    "用途": "use_purpose",
    "今後の利用目的": "future_use_purpose",
    "前面道路：方位": "front_road_direction",
    "前面道路：種類": "front_road_classification",
    "前面道路：幅員（ｍ）": "front_road_breadth_m",
    "都市計画": "city_planning_zone",
    "建ぺい率（％）": "building_coverage_ratio",
    "容積率（％）": "floor_area_ratio",
    "取引時期": "transaction_period",
    "取引の事情等": "remarks",
}

ERA_OFFSETS = {
    "昭和": 1925,
    "平成": 1988,
    "令和": 2018,
}

INT_COLUMNS = [
    "trade_price_yen",
    "price_per_tsubo",
    "unit_price_per_sqm",
    "nearest_station_minutes",
    "building_coverage_ratio",
    "floor_area_ratio",
]
FLOAT_COLUMNS = ["area_sqm", "frontage_m", "total_floor_area_sqm", "front_road_breadth_m"]
STRING_COLUMNS = [
    "trade_type",
    "price_category",
    "region",
    "municipality_code",
    "prefecture_name",
    "municipality_name",
    "district_name",
    "nearest_station_name",
    "land_shape",
    "building_structure",
    "use_purpose",
    "future_use_purpose",
    "front_road_direction",
    "front_road_classification",
    "city_planning_zone",
    "remarks",
]


def read_csv_with_fallback(path: str) -> pd.DataFrame:
    """日本語CSV特有の文字コード揺れ（CP932/Shift_JIS/UTF-8/UTF-8 BOM付き）を
    順に試し、最初に成功した結果を返す。全滅した場合は置換文字付きで強制読込する。
    """
    encodings_to_try = ["cp932", "shift_jis", "utf-8-sig", "utf-8"]
    last_error = None
    for encoding in encodings_to_try:
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_error = exc
            continue
    print(f"警告: {path} はいずれの想定エンコードでも正常デコードできず、"
          f"文字化け置換（errors='replace'）で強制読込します。最終エラー: {last_error}")
    return pd.read_csv(path, encoding="cp932", dtype=str, keep_default_na=False, encoding_errors="replace")


def clean_str(value):
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value).strip()
    return value if value != "" else None


def parse_building_year(raw):
    """"2020年" のような西暦表記、および "昭和55年"/"平成2年"/"令和3年" のような
    和暦表記の両方に対応して西暦(int)へ正規化する。空欄はNoneを返す。
    """
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "" or raw == "戦前":
        return None

    for era_name, offset in ERA_OFFSETS.items():
        if raw.startswith(era_name):
            digits = re.search(r"\d+", raw)
            if digits:
                return offset + int(digits.group())
            return None

    digits = re.search(r"(\d{4})", raw)
    if digits:
        return int(digits.group(1))
    return None


def parse_transaction_period(raw):
    """"2021年第1四半期" を (2021, 1) に変換する。"""
    match = re.match(r"(\d{4})年第(\d)四半期", raw.strip())
    if not match:
        raise ValueError(f"取引時期の形式が想定外です: {raw!r}")
    return int(match.group(1)), int(match.group(2))


def load_one_csv(path: str) -> pd.DataFrame:
    df = read_csv_with_fallback(path)
    # 列名はunicodedata.normalize("NFKC", ...)を適用しない。
    # 実データで確認済みの通り、cp932デコード自体は列名（全角括弧・全角％・㎡・全角コロン
    # を含む）を正しく再現するが、NFKCをかけると"（％）"→"(%)"、"㎡"→"m2"のように
    # 半角/分解形へ変換され、JP_TO_EN_COLUMNS辞書（実ヘッダーそのままの全角表記）と
    # 一致しなくなる。列名は固定語彙のため揺れ吸収は不要で、空白除去のみで足りる。
    df.columns = [c.strip() for c in df.columns]
    missing = set(JP_TO_EN_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} に想定した列が不足しています: {missing}")
    df = df.rename(columns=JP_TO_EN_COLUMNS)
    df["source_file"] = os.path.basename(path)
    return df


def transform(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["trade_type"].apply(clean_str) == TARGET_TRADE_TYPE].copy()

    for col in STRING_COLUMNS:
        df[col] = df[col].apply(clean_str)

    for col in INT_COLUMNS:
        df[col] = pd.to_numeric(df[col].apply(clean_str), errors="coerce").astype("Int64")

    for col in FLOAT_COLUMNS:
        df[col] = pd.to_numeric(df[col].apply(clean_str), errors="coerce")

    df["building_year"] = df["building_year_raw"].apply(parse_building_year).astype("Int64")

    periods = df["transaction_period"].apply(clean_str).apply(parse_transaction_period)
    df["transaction_year"] = periods.apply(lambda t: t[0]).astype("Int64")
    df["transaction_quarter"] = periods.apply(lambda t: t[1]).astype("Int64")

    # 築年数 = 取引時点の年 - 建築年（取引時点基準。現在年基準ではない点に注意）
    df["building_age_years"] = (df["transaction_year"] - df["building_year"]).astype("Int64")
    df.loc[df["building_age_years"] < 0, "building_age_years"] = pd.NA

    df["municipality_code"] = df["municipality_code"].str.zfill(5)

    return df


OUTPUT_COLUMNS = [
    "trade_type", "price_category", "region", "municipality_code",
    "prefecture_name", "municipality_name", "district_name",
    "nearest_station_name", "nearest_station_minutes",
    "trade_price_yen", "price_per_tsubo", "area_sqm", "unit_price_per_sqm",
    "land_shape", "frontage_m", "total_floor_area_sqm",
    "building_year", "building_age_years", "building_structure",
    "use_purpose", "future_use_purpose", "front_road_direction",
    "front_road_classification", "front_road_breadth_m",
    "city_planning_zone", "building_coverage_ratio", "floor_area_ratio",
    "transaction_period", "transaction_year", "transaction_quarter",
    "remarks", "source_file",
]

INSERT_SQL = "INSERT INTO realestate.transactions (" + ", ".join(OUTPUT_COLUMNS) + ") VALUES %s"


def to_row(record):
    return tuple(
        None if pd.isna(record[col]) else record[col] for col in OUTPUT_COLUMNS
    )


def main():
    csv_paths = sorted(glob.glob(os.path.join(RAW_TRANSACTIONS_DIR, "*.csv")))
    if not csv_paths:
        raise FileNotFoundError(f"{RAW_TRANSACTIONS_DIR} にCSVが見つかりません")

    all_frames = []
    for path in csv_paths:
        df = load_one_csv(path)
        df = transform(df)
        print(f"{os.path.basename(path)}: 戸建 {len(df)} 件を抽出")
        all_frames.append(df[OUTPUT_COLUMNS])

    combined = pd.concat(all_frames, ignore_index=True)

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            rows = [to_row(record) for record in combined.to_dict(orient="records")]
            psycopg2.extras.execute_values(cur, INSERT_SQL, rows, page_size=1000)
        conn.commit()
        print(f"合計 {len(rows)} 件を realestate.transactions に投入しました")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
