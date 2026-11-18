# 人口減少が戸建住宅価格に与える影響 — PostGIS × Python 分析パイプライン

国土交通省「不動産情報ライブラリ」の取引価格データと、e-Stat 国勢調査 500m メッシュ人口（2015・2020年）を PostGIS で空間結合し、
**「人口が伸び悩んだ地区ほど、中古戸建の価格水準が低いか」** をヘドニック価格モデルで検証するプロジェクト。

> 最終更新: 2026-11-18（進捗の棚卸し時点）

---

## 1. 進捗サマリー

| フェーズ | 状態 | 成果物 |
|---|---|---|
| 要件定義・スコープ | ✅ 完了 | 対象4自治体・戸建限定・500mメッシュ・ヘドニックモデルの方針確定 |
| データ取得（手動DL・API不使用） | ✅ 完了 | `data/`（Git管理外） |
| DB設計・ロード | ✅ 完了 | `sql/init.sql`, `scripts/load_*.py` |
| 住所 ⇔ メッシュの結合ロジック | ✅ 完了 | `sql/normalize_address.sql`, `sql/oaza_mesh_fallback.sql` |
| EDA Step1（読み込み・欠測・分布） | ✅ 完了 | `notebooks/eda_step1.ipynb` |
| EDA Step2（前処理の自動化） | ✅ 完了 | `notebooks/eda_step2_preprocessing.py` |
| Step3（主モデル + 頑健性チェック） | ✅ 完了 | `notebooks/eda_step3_diagnostics.py`, `notebooks/output/eda_step3/` |
| レポート（非専門家向けリライト） | ✅ 初版完成 | `outputs/人口減少と戸建住宅価格_検証レポート.docx` |


---

## 2. 対象とデータ

**対象地域（神奈川県4市区町村）**

| 群 | 市区町村 | コード |
|---|---|---|
| 人口減少群 | 横須賀市 / 三浦市 | 14201 / 14210 |
| 人口安定・増加群 | 川崎市多摩区 / 川崎市宮前区 | 14135 / 14136 |

**データソース**（いずれも Web から手動ダウンロード。ネットワーク通信・API は使用しない）

| データ | 出典 | 配置先 |
|---|---|---|
| 不動産取引価格（2021Q1〜2026Q1） | 国交省 不動産情報ライブラリ（ダウンロード機能） | `data/raw_transactions/*.csv` |
| 大字・町丁目レベル位置参照情報（令和7年 / 19.0b） | 国土数値情報 | `data/address_points/<コード>-19.0b/` |
| 2分の1地域メッシュ境界（1次メッシュ 5238/5239/5338/5339） | e-Stat 統計GIS | `data/mesh/{2015,2020}_boundary/` |
| メッシュ人口（2015: T000847系 / 2020: T001101系） | e-Stat 統計GIS | `data/mesh/{2015,2020}_population.csv` |

- 物件種別は `宅地(土地と建物)`（＝戸建）に限定。
- メッシュ境界は JGD2000（EPSG:4612）→ EPSG:4326 に再投影してロード。
- メッシュの秘匿処理フラグ（HTKSYORI≠0）は `is_suppressed=True` とし、人口増減率の計算から除外。

---

## 3. 環境構築と実行手順

### 3-1. コンテナ起動

```bash
# .env に DB_USER / DB_PASSWORD / DB_NAME / MLIT_API_KEY を設定（.env はGit管理外）
docker compose up -d --build
# JupyterLab: http://localhost:8888  /  PostGIS: localhost:5432
```

- `db`: `postgis/postgis:16-3.4`。初回起動時に `sql/init.sql` が自動実行される。`./sql` は `/sql` に読み取り専用でマウント。
- `notebook`: Python 3.11（pandas / statsmodels / geopandas / SQLAlchemy / GeoAlchemy2 ほか、バージョンは `Dockerfile.notebook` で固定）。
- VS Code の Dev Container（`.devcontainer/`）からも `notebook` サービスに接続可能。

### 3-2. データロード → 結合ビュー作成

```bash
# 1) ロード（notebook コンテナで実行）
docker compose exec notebook python scripts/load_transactions.py      # → realestate.transactions
docker compose exec notebook python scripts/load_address_points.py    # → realestate.address_points
docker compose exec notebook python scripts/load_census_mesh.py       # → realestate.census_mesh（2015/2020）

# 2) マイグレーション（db コンテナで実行。init.sql の後に順に適用）
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /sql/normalize_address.sql'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f /sql/oaza_mesh_fallback.sql'
```

ロードスクリプトはデータをリポジトリ直下の `data/` から読む（スクリプト位置からの相対パスで解決するため、コンテナ内では `/home/jovyan/work/data/`）。
別の場所に置く場合は環境変数 `DATA_ROOT` で上書きできる。

`sql/verify_*.sql` / `diagnose_*.sql` は、結合率の検証・不一致原因の調査に使った確認用クエリ。

### 3-3. 分析の実行

`notebooks/eda_step1.ipynb` を上から実行（Step1 の `df` 作成 → Step2 前処理 → Step3 診断まで）。Step3 は次の1呼び出しで全出力を生成する。

```python
import importlib, eda_step2_preprocessing as pp, eda_step3_diagnostics as dg
df = pp.attach_extra_columns(df, engine)
df_clean, filter_log = pp.preprocess(df)
summary = dg.run_all(df, df_clean)   # → notebooks/output/eda_step3/ に保存
```

---

## 4. DB設計と空間結合ロジック

スキーマ `realestate`:

| オブジェクト | 内容 |
|---|---|
| `transactions` | 取引データ（位置情報なし。地区名 `district_name` のみ） |
| `address_points` | 町丁目代表点（Point, 4326, GiST索引） |
| `census_mesh` | 500mメッシュ（Polygon, 4326）× 年次の人口・世帯・65歳以上人口 |
| `v_transactions_with_mesh` | 取引に人口増減率・高齢化率を付与した分析ビュー |

**取引データには座標が無い**ため、「地区名 → 町丁目代表点 → `ST_Within` でメッシュ」の2段階で人口統計を付与する。

1. **表記揺れの正規化**: 全角/半角は Python 側で NFKC 統一。丁目の漢数字⇔算用数字（例: `三丁目`→`3丁目`）は PL/pgSQL 関数 `normalize_address_name()` で変換。
2. **粒度不一致の解消**（`diagnose_granularity_mismatch.sql` で原因特定）: 取引側は丁目を持たない「大字」表記が大半で、位置参照情報は丁目単位の点しか持たない。
   - `chome_exact`: 丁目まで一致 → その1点が属するメッシュの値を使用。
   - `oaza_mesh_avg`: 大字のみ一致 → **その大字に属する全丁目代表点それぞれのメッシュ値を平均**（案B）。
     疑似中心点を作る案Aは、点が無関係なメッシュに落ちるリスクがあるため不採用。
3. 結合結果: **7,518件すべてに付与（unmatched 0件）**。`chome_exact` 907件 / `oaza_mesh_avg` 6,611件。growth 欠測23件はメッシュ秘匿による正当な欠測。

**説明変数の定義**

- `growth` = (P2020 − P2015) / P2015（500mメッシュ人口変化率）
- `ln_pop_ratio` = ln(P2020 / P2015)（頑健性用）
- `aging` = 2020年の65歳以上人口比率

---

## 5. 分析内容と結果

### 5-1. 前処理（`eda_step2_preprocessing.py`）

除外ルールはすべて機械的（目視による個別除外はしない）。

| ステップ | 除外 | 残り |
|---|---:|---:|
| 入力 | — | 7,518 |
| 備考による除外（関係者間・競売/調停・隣地購入 等） | 94 | 7,424 |
| 用途が「住宅」以外（NULL は keep） | 737 | 6,687 |
| 必須変数の欠測 | 105 | 6,582 |
| 価格 < 10万円（入力誤り） | 1 | **6,581** |

- 主分析標本: **中古のみ 4,535件**（新築 2,046件は頑健性チェックで使用）。クラスタ（大字）数 157。
- 用途 NULL（約50%）は R12 で「住宅行と価格水準・傾きとも統計的に区別できない」ことを確認し、keep 方針を採用。

### 5-2. モデル

```
ln(価格) = β·growth + ln(土地面積) + ln(延床面積)
          + 築年数ビン + 駅徒歩ビン + 土地形状 + 構造 + 前面道路幅 + 用途地域
          + 市区町村FE + 年四半期FE + ε
```

- 標準誤差: 大字単位のクラスタ頑健SE。p値・CI は t(G−1) 分布。
- 主分析の R² = 0.642。

### 5-3. 主な結果（`notebooks/output/eda_step3/report.txt`）

| 仕様 | growth係数 | SE | p | 人口+10%あたり価格 |
|---|---:|---:|---:|---:|
| **R0 主分析（中古のみ）** | **+0.363** | 0.107 | 0.0009 | **+3.7%** |
| R1 全標本（中古の傾き） | +0.418 | 0.123 | 0.0009 | +4.3% |
| R4 ln(人口比) | +0.619 | 0.188 | 0.0012 | +6.4% |
| R5 価格 p1〜p99 のみ | +0.350 | 0.089 | 0.0001 | +3.6% |
| R8 人口減少群の傾き | +0.420 | 0.126 | 0.0011 | +4.3% |
| R8 安定・増加群の傾き | −0.042 | 0.198 | 0.833 | −0.4% |
| R9 aging を追加 | +0.289 | 0.146 | 0.0497 | +2.9% |
| R3 growth 極端値2地区を除外 | +0.624 | 0.367 | 0.091 | +6.4% |

**解釈**

- 人口が伸び悩んだ地区ほど中古戸建の価格水準が低い、という**正の関係が多くの仕様で一貫**（中古標本の growth 係数は R0〜R12 のすべてで正。ただし安定・増加群や新築に限った傾きは 0 付近）。
- 関係は**人口減少群（横須賀・三浦）で強く、安定・増加群ではほぼゼロ**。
- **重要な限界**: Leave-one-cluster-out（R11）で、横須賀市「佐島の丘」（人口 330→636人の新興住宅地）1地区を除くと p = 0.162 となり有意性が失われる。符号が反転するクラスタは 0/157。growth が地区内で一定のため、実効的な標本は地区数（157）で、この1地区が growth の分散の大半を担っている。
- 示せたのは**クロスセクションの相関**であり、因果（人口減少 → 価格下落）の証明ではない（逆因果・未観測の地域特性の可能性）。

### 5-4. レポート

`outputs/人口減少と戸建住宅価格_検証レポート.docx`（非専門家向け・付録に技術詳細）
構成: はじめに → 仮説 → データと方法 → 結果 → 意図した結果が出ない場合の原因と改善策 → 相関と因果 → まとめと今後の課題 → 付録（モデル式・頑健性一覧・処理ログ）

---

## 6. 今後の課題

1. **時系列での検証**: 地区FE × 時間トレンド（または人口動向 × 時間の交差項）で、同一地区内で価格が実際に下落していくかを検証する。
2. **対象市区町村の追加**: クラスタ数を増やし、1地区への依存を薄める。
3. 神奈川県外・より長期間のデータでの再検証。
4. 再現性の整備: 実行手順のスクリプト化（Makefile 等）。
5. LICENSE の追加（未設定のままだと閲覧のみ可・再利用不可の扱いになる）。

---

## 7. ディレクトリ構成

```
.
├── docker-compose.yml / Dockerfile.notebook / .devcontainer/
├── .env                      # 接続情報（Git管理外）
├── data/                     # 生データ（Git管理外）
├── scripts/                  # ロードスクリプト（transactions / address_points / census_mesh）
├── sql/
│   ├── init.sql              # スキーマ・テーブル・正規化関数・初版ビュー
│   ├── normalize_address.sql # 丁目表記の正規化マイグレーション
│   ├── oaza_mesh_fallback.sql# 大字レベル取引のメッシュ平均フォールバック（現行ビュー）
│   └── verify_* / diagnose_*.sql  # 検証・調査用
├── notebooks/
│   ├── eda_step1.ipynb       # 読み込み・欠測・分布 → Step2/3 の呼び出し
│   ├── eda_step2_preprocessing.py
│   ├── eda_step3_diagnostics.py
│   └── output/               # 図・report.txt・各種CSV
└── outputs/                  # レポート（.docx）ほか
```
