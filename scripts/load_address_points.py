"""
load_address_points.py

国土数値情報「大字・町丁目レベル位置参照情報」（令和7年/19.0b版、
https://nlftp.mlit.go.jp/isj/ から市区町村単位で手動ダウンロード済み）
のCSV群を読み込み、realestate.address_points にロードする。

対象ディレクトリ構成（確認済み）:
    data/address_points/14201-19.0b/14201_2025.csv  横須賀市
    data/address_points/14210-19.0b/14210_2025.csv  三浦市
    data/address_points/14135-19.0b/14135_2025.csv  川崎市多摩区
    data/address_points/14136-19.0b/14136_2025.csv  川崎市宮前区

CSVの実ヘッダー（CP932エンコード）:
    都道府県コード,都道府県名,市区町村コード,市区町村名,大字町丁目コード,
    大字町丁目名,緯度,経度,原典資料コード,大字・字・丁目区分コード

APIを一切使用しない。ネットワーク通信は行わない。
"""

import glob
import os
import unicodedata

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point
from sqlalchemy import create_engine

# データの置き場所: 既定はリポジトリ直下の data/（このファイルの1つ上の階層）。
# コンテナ内では /home/jovyan/work/data、ホスト直実行でも同じ相対位置で解決される。
# 別の場所に置く場合のみ環境変数 DATA_ROOT で上書きする。
DATA_ROOT = os.environ.get(
    "DATA_ROOT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
)
ADDRESS_POINTS_DIR = os.path.join(DATA_ROOT, "address_points")

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "realestate")
DB_USER = os.environ.get("DB_USER", "analyst")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "analyst_pw")

JP_TO_EN_COLUMNS = {
    "都道府県コード": "prefecture_code",
    "都道府県名": "prefecture_name",
    "市区町村コード": "municipality_code",
    "市区町村名": "municipality_name",
    "大字町丁目コード": "oaza_chome_code",
    "大字町丁目名": "oaza_chome_name",
    "緯度": "latitude",
    "経度": "longitude",
    "原典資料コード": "origin_code",
    "大字・字・丁目区分コード": "oaza_class_code",
}


def read_csv_with_fallback(path: str) -> pd.DataFrame:
    """CP932/Shift_JIS/UTF-8(BOM付き含む)の順にデコードを試し、
    最初に成功したものを採用する。
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


def load_one_csv(path: str) -> pd.DataFrame:
    df = read_csv_with_fallback(path)
    df.columns = [unicodedata.normalize("NFKC", c).strip() for c in df.columns]
    missing = set(JP_TO_EN_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"{path} に想定した列が不足しています: {missing}")
    return df.rename(columns=JP_TO_EN_COLUMNS)


def main():
    csv_paths = sorted(glob.glob(os.path.join(ADDRESS_POINTS_DIR, "*", "*.csv")))
    if not csv_paths:
        raise FileNotFoundError(f"{ADDRESS_POINTS_DIR}/*/*.csv にCSVが見つかりません")

    frames = [load_one_csv(path) for path in csv_paths]
    df = pd.concat(frames, ignore_index=True)

    for col in ["oaza_chome_name", "prefecture_name", "municipality_name",
                "oaza_chome_code", "origin_code", "oaza_class_code"]:
        df[col] = df[col].apply(lambda v: unicodedata.normalize("NFKC", v).strip() if v else None)

    df["municipality_code"] = df["municipality_code"].str.zfill(5)
    df["prefecture_code"] = df["prefecture_code"].str.zfill(2)
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df = df.dropna(subset=["latitude", "longitude"])

    # 同一町丁目の重複行（原典資料コード違い等）は代表点として先頭行のみ残す
    df = df.drop_duplicates(subset=["municipality_code", "oaza_chome_name"], keep="first")

    geometry = [Point(lon, lat) for lon, lat in zip(df["longitude"], df["latitude"])]
    gdf = gpd.GeoDataFrame(
        df[[
            "prefecture_code", "prefecture_name", "municipality_code",
            "municipality_name", "oaza_chome_code", "oaza_chome_name",
            "longitude", "latitude", "origin_code", "oaza_class_code",
        ]],
        geometry=geometry,
        crs="EPSG:4326",
    )
    gdf = gdf.rename(columns={"geometry": "geom"}).set_geometry("geom")

    engine = create_engine(
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

    gdf.to_postgis(
        name="address_points",
        con=engine,
        schema="realestate",
        if_exists="append",
        index=False,
    )

    print(f"{len(gdf)} 件の町丁目代表点を address_points に投入しました（{len(csv_paths)}市区分）")


if __name__ == "__main__":
    main()
