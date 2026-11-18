"""
load_census_mesh.py

e-Stat 統計GISから手動ダウンロード済みの2分の1地域メッシュ（約500m四方）
境界シェープファイル（1次メッシュ単位で4分割: 5238/5239/5338/5339）と、
同年の人口統計CSVをメッシュコード(KEY_CODE)で結合し、
realestate.census_mesh に平成27年(2015)・令和2年(2020)分をロードする。

対象ファイル（確認済み）:
    data/mesh/2015_boundary/MESH05238.shp / MESH05239.shp / MESH05338.shp / MESH05339.shp
    data/mesh/2015_population.csv
    data/mesh/2020_boundary/MESH05238.shp / MESH05239.shp / MESH05338.shp / MESH05339.shp
    data/mesh/2020_population.csv

境界シェープファイルの座標系はJGD2000（.prjで確認済み: GCS_JGD_2000）であり、
EPSG:4326（WGS84）へ明示的に再投影する。

人口統計CSVはCP932エンコードの2行ヘッダー（1行目: 表章項目コード、
2行目: 日本語ラベル）＋データ行という e-Stat 標準形式。
年ごとに表章項目コードの体系が異なる（2015年国勢調査=T000847系、
令和2年国勢調査=T001101系）ため、年別にマッピングを固定する。
また HTKSYORI（秘匿処理フラグ）が0以外の場合、当該メッシュの人口は
秘匿・合算処理により実数を表さないため is_suppressed=True とし、
人口増減率の計算からは除外する（init.sql の v_transactions_with_mesh 参照）。

APIを一切使用しない。ネットワーク通信は行わない。
"""

import glob
import os

import geopandas as gpd
import pandas as pd
from sqlalchemy import create_engine

DB_HOST = os.environ.get("DB_HOST", "db")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "realestate")
DB_USER = os.environ.get("DB_USER", "analyst")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "analyst_pw")

# データの置き場所: 既定はリポジトリ直下の data/（このファイルの1つ上の階層）。
# コンテナ内では /home/jovyan/work/data、ホスト直実行でも同じ相対位置で解決される。
# 別の場所に置く場合のみ環境変数 DATA_ROOT で上書きする。
DATA_ROOT = os.environ.get(
    "DATA_ROOT", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
)
DATA_DIR = os.path.join(DATA_ROOT, "mesh")
SOURCE_CRS = "EPSG:4612"  # JGD2000（.prjファイルで確認済み）

YEAR_COLUMN_CODES = {
    2015: {
        "population": "T000847001",
        "households": "T000847025",
        "population_aged_65_over": "T000847016",
    },
    2020: {
        "population": "T001101001",
        "households": "T001101034",
        "population_aged_65_over": "T001101019",
    },
}


def read_population_csv(path: str) -> pd.DataFrame:
    """e-Stat地域メッシュ統計CSV（CP932、1行目コード・2行目ラベル・3行目以降データ）
    を読み込む。UTF-8配布に切り替わっていた場合のフォールバックも行う。
    """
    for encoding in ("cp932", "shift_jis", "utf-8-sig", "utf-8"):
        try:
            return pd.read_csv(path, encoding=encoding, skiprows=[1], dtype=str)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise UnicodeDecodeError("population_csv", b"", 0, 1, f"{path} をデコードできませんでした")


def load_one_year(year: int) -> gpd.GeoDataFrame:
    boundary_dir = f"{DATA_DIR}/{year}_boundary"
    population_csv = f"{DATA_DIR}/{year}_population.csv"
    shp_paths = sorted(glob.glob(os.path.join(boundary_dir, "MESH0*.shp")))
    if not shp_paths:
        raise FileNotFoundError(f"{boundary_dir}/MESH0*.shp が見つかりません")

    boundary_frames = [gpd.read_file(p) for p in shp_paths]
    boundary = gpd.GeoDataFrame(pd.concat(boundary_frames, ignore_index=True))
    boundary = boundary.rename(columns={"KEY_CODE": "mesh_code"})
    boundary["mesh_code"] = boundary["mesh_code"].astype(str)
    boundary = boundary.drop_duplicates(subset=["mesh_code"])

    if boundary.crs is None:
        boundary = boundary.set_crs(SOURCE_CRS)
    boundary = boundary.to_crs(epsg=4326)

    population = read_population_csv(population_csv)
    population.columns = [c.strip() for c in population.columns]
    population["mesh_code"] = population["KEY_CODE"].astype(str)
    population["htksyori"] = pd.to_numeric(population["HTKSYORI"], errors="coerce").fillna(0)

    codes = YEAR_COLUMN_CODES[year]
    # PostGISの population/households/population_aged_65_over 列はINTEGERで定義しているため、
    # float64のまま(pandas既定)ではCOPY時に"5.0"のようなテキストとなり型不一致で失敗する。
    # pandasのnullable Int64にキャストし、欠損はpd.NA→pandasのto_sqlパイプライン内でNoneに
    # 正規化された上でCOPYのNULLとして書き込まれるようにする。
    population["population"] = pd.to_numeric(
        population[codes["population"]], errors="coerce"
    ).astype("Int64")
    population["households"] = pd.to_numeric(
        population[codes["households"]], errors="coerce"
    ).astype("Int64")
    population["population_aged_65_over"] = pd.to_numeric(
        population[codes["population_aged_65_over"]], errors="coerce"
    ).astype("Int64")
    population["is_suppressed"] = population["htksyori"] != 0

    merged = boundary.merge(
        population[[
            "mesh_code", "population", "households",
            "population_aged_65_over", "is_suppressed",
        ]],
        on="mesh_code",
        how="left",
    )
    merged["is_suppressed"] = merged["is_suppressed"].fillna(True).astype(bool)
    merged["census_year"] = year

    merged = merged[[
        "mesh_code", "census_year", "population", "households",
        "population_aged_65_over", "is_suppressed", "geometry",
    ]]
    merged = merged.rename(columns={"geometry": "geom"}).set_geometry("geom")
    return merged


def main():
    engine = create_engine(
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )

    for year in (2015, 2020):
        gdf = load_one_year(year)
        gdf.to_postgis(
            name="census_mesh",
            con=engine,
            schema="realestate",
            if_exists="append",
            index=False,
        )
        n_suppressed = int(gdf["is_suppressed"].sum())
        print(f"{year}年メッシュ: {len(gdf)} 件を census_mesh に投入しました"
              f"（うち秘匿・欠測扱い {n_suppressed} 件）")


if __name__ == "__main__":
    main()
