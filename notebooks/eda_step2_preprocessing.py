# -*- coding: utf-8 -*-
"""
eda_step2_preprocessing.py
==========================
ヘドニック回帰用の自動前処理（手作業なし・DB再ロードなし）

使い方（Step1 ノートブックで作った df を渡す）
---------------------------------------------
    import eda_step2_preprocessing as pp

    df = pp.attach_extra_columns(df, engine)        # remarks / use_purpose を SELECT で追加（再ロード不要）
    df_clean, filter_log = pp.preprocess(df)        # 除外ログを標準出力し、回帰用 DataFrame を返す

    import pandas as pd, statsmodels.formula.api as smf
    d = pp.get_sample(df_clean, "used")             # 主分析: 中古のみ
    res = smf.ols(pp.build_formula("used"), data=d).fit(
        cov_type="cluster", cov_kwds={"groups": pd.factorize(d["cluster_oaza"])[0]})
    print(res.summary())

    d_all = pp.get_sample(df_clean, "all")          # 頑健性: 全標本（growth:new_build 交互作用つき）
    res_all = smf.ols(pp.build_formula("all"), data=d_all).fit(
        cov_type="cluster", cov_kwds={"groups": pd.factorize(d_all["cluster_oaza"])[0]})

確定済みの設計判断（このファイルはこれを機械的に実装したもの）
------------------------------------------------------------
- 除外は「備考(remarks)の正規表現」と「用途(use_purpose)の完全一致」のみ。p1/p99 の目視確認はしない。
- 駅徒歩は現行 nearest_station_minutes のみでビン化（再ロードなし）。
- 標準誤差は大字単位のクラスタ頑健（wild bootstrap は使わない）。
- 主モデル A = 市区町村FE + growth 主効果 + 年四半期FE。分析標本の主軸は「中古のみ」。
- 築年数の負値は築0（新築・未完成取引）として処理する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# =====================================================================
# 0. 設定（ルールを変えたいときは、この節の定数だけを編集する）
# =====================================================================
CITY = {"14201": "横須賀市", "14210": "三浦市", "14135": "川崎市多摩区", "14136": "川崎市宮前区"}
CITY_ORDER = list(CITY.values())
GROUP = {"横須賀市": "人口減少", "三浦市": "人口減少", "川崎市多摩区": "人口安定・増加", "川崎市宮前区": "人口安定・増加"}

# --- 備考(remarks)による除外 -----------------------------------------
# 意図: 市場価格を反映しない取引、または「全体価格」でない取引を機械的に除く。
#   関係者間取引 : 親族・関係会社間など、価格が市場から乖離しうる
#   持分取引     : 共有持分のみの取引で、価格と面積が整合しない
#   競売・調停等 : 強制的売却で、市場価格より低くなりうる
#   隣地の購入   : 一体利用目的の部分取引で、価格が代表的でない
#   権利・負担付 : 賃借権などの負担付きで、完全所有権の取引と同質でない
# 「ラベル: 正規表現」。追加・削除はここで行う（各パターンの該当件数はログに出る）。
REMARK_EXCLUDE_PATTERNS = {
    "関係者間取引": r"関係者",
    "持分取引": r"持分",
    "競売・調停等": r"競売|調停",
    "隣地の購入": r"隣地",
    "権利・負担付き": r"他の権利|負担付",
}

# --- 用途(use_purpose)による除外 --------------------------------------
# 意図: 店舗・事務所・工場等が混じった取引を除き、「居住用の戸建」に限定する（完全一致）。
USE_PURPOSE_KEEP = "住宅"
# 用途が空欄(NULL)の行の扱い。
#   "keep": 空欄は除外しない（空欄は「非住宅」の証拠ではないため。既定）
#   "drop": 空欄も除外する（'住宅' 以外をすべて除外する厳密な解釈）
# 空欄が多い場合、"drop" は標本を大きく減らす。件数はログに出るので、確認のうえ切り替えられる。
USE_PURPOSE_NULL_POLICY = "keep"

# --- 価格の絶対下限（入力誤りの除去）-----------------------------------
# 意図: 土地+建物の取引価格が 10万円 未満なのは、市場取引としては不自然（入力単位の誤り・0円取引等）。
#   分布の両端を切る「トリム」ではなく、明白な誤りだけを除く絶対基準（実データでは 1 件の想定）。
#   低価格側の分布そのものは（人口減少地域の下位取引を保持するため）切らない。
PRICE_FLOOR_YEN = 100_000

# --- 安全弁（自動化しても異常な大量除外を見逃さないための警告） ---------
MAX_STEP_DROP_WARN_PCT = 15.0   # 1つの除外ステップで元の件数の何%を超えたら警告するか
MIN_CLUSTERS_WARN = 40          # クラスタ数がこれ未満なら警告（標準誤差の信頼性の目安）

# --- 感度分析用フラグ（フィルタはしない。列を足すだけ）------------------
GROWTH_ROBUST_Z = 6.0           # growth が (中央値 + Z × ロバストSD) を超える行に growth_extreme を立てる

# --- ビン・カテゴリ定義 ---------------------------------------------
AGE_LABELS = ["新築", "1-5", "6-10", "11-15", "16-20", "21-25", "26-30", "31-40", "41+"]
AGE_BINS = [-np.inf, 0, 5, 10, 15, 20, 25, 30, 40, np.inf]          # 右閉区間。築年数は整数
STATION_LABELS = ["0-5", "6-10", "11-15", "16-20", "21-29"]
STATION_BINS = [-np.inf, 5, 10, 15, 20, 29]                         # 現行値の最大は29
STATION_UNKNOWN = "30+/不明"
ROAD_LABELS = ["2m未満", "2-4m未満", "4-6m未満", "6m以上"]
ROAD_BINS = [-np.inf, 2, 4, 6, np.inf]                              # 左閉区間 [2,4) など。4.0m は「4-6m未満」
LAND_SHAPE_MAP = {
    "正方形": "整形系", "ほぼ正方形": "整形系", "長方形": "整形系", "ほぼ長方形": "整形系",
    "整形": "整形系", "ほぼ整形": "整形系",
    "台形": "台形系", "ほぼ台形": "台形系",
    "不整形": "不整形", "袋地等": "袋地等",
}
LAND_SHAPE_ORDER = ["整形系", "台形系", "不整形", "袋地等", "不明"]
STRUCTURE_ORDER = ["木造", "非木造", "不明"]
ZONE_ORDER = ["住居系", "商業系", "工業系", "調整区域", "不明・その他"]

# 回帰の基準カテゴリ（Treatment coding の reference）。growth の係数には影響しない。
REF = {
    "age_bin": "11-15", "station_bin": "11-15", "land_shape_grp": "整形系", "structure_grp": "木造",
    "road_bin": "4-6m未満", "zone_grp": "住居系", "city": "横須賀市", "year_quarter": "2021Q1",
}

# 入力 df に必須の列（Step1 の SQL の列 + attach_extra_columns で足す remarks / use_purpose）
REQUIRED_RAW_COLS = [
    "transaction_id", "municipality_code", "district_name", "match_type", "n_source_points",
    "trade_price_yen", "area_sqm", "total_floor_area_sqm", "building_year",
    "nearest_station_minutes", "city_planning_zone", "land_shape", "building_structure",
    "front_road_breadth_m", "transaction_year", "transaction_quarter",
    "population_2015", "population_2020", "population_change_rate_15_20", "aging_rate_2020",
    "use_purpose", "remarks",
]

OUTPUT_COLS = [
    # 識別子・クラスタ・空間結合の品質
    "transaction_id", "municipality_code", "city", "group", "district_name",
    "cluster_oaza", "cluster_district", "match_type", "is_chome_exact", "n_source_points",
    # 時間
    "transaction_year", "transaction_quarter", "year_quarter",
    # 被説明変数・連続変数（対数）
    "trade_price_yen", "ln_price", "ln_land_area", "ln_floor_area",
    # 人口統計
    "growth", "aging", "ln_pop_ratio",
    # 築年数（新築フラグ・ビン）
    "age_signed", "age", "new_build", "presale_flag", "age_bin",
    # カテゴリ集約
    "station_bin", "land_shape_grp", "structure_grp", "road_bin", "zone_grp",
    # 感度分析用フラグ
    "sens_in_p1_p99", "sens_growth_extreme",
]


# =====================================================================
# 1. 補助関数
# =====================================================================
def _nfkc(s: pd.Series) -> pd.Series:
    """文字列を NFKC 正規化して前後の空白を除く。空文字は欠測にする。
    意図: 全角/半角の揺れ（例: 『第１種』と『第1種』）で完全一致・正規表現が外れるのを防ぐ。"""
    out = s.astype("string").str.normalize("NFKC").str.strip()
    return out.mask((out == "").fillna(False))


def _contains(s: pd.Series, pattern: str) -> np.ndarray:
    """正規表現に該当する行の bool 配列（欠測は False）。"""
    return s.str.contains(pattern, regex=True, na=False).to_numpy(dtype=bool)


def _vc(s: pd.Series, n: int | None = None, dropna: bool = True) -> dict:
    """value_counts を「値: 件数」の素の dict にする（表示用。件数0の水準は省く）。"""
    vc = s.value_counts(dropna=dropna)
    vc = vc[vc > 0]
    if n:
        vc = vc.head(n)
    return {("NULL" if pd.isna(k) else k): int(v) for k, v in vc.items()}


def _num(s: pd.Series) -> pd.Series:
    """数値化（変換できない値は欠測）。"""
    return pd.to_numeric(s, errors="coerce")


def _cat(values, order: list[str], name: str) -> pd.Categorical:
    """順序を固定したカテゴリ列にする。想定外の値があれば止める（黙って欠測にしない）。"""
    c = pd.Categorical(values, categories=order, ordered=False)
    n_bad = int(pd.isna(c).sum())
    if n_bad:
        raise ValueError(f"{name}: 想定カテゴリ外の値が {n_bad} 件あります。")
    return c


class _StepLog:
    """除外ステップごとの件数を標準出力し、最後に表として返す。"""

    def __init__(self, n0: int, verbose: bool):
        self.n0, self.verbose = n0, verbose
        self.rows = [{"step": "0. 入力", "removed": 0, "remaining": n0}]

    def add(self, name: str, removed: int, remaining: int) -> None:
        self.rows.append({"step": name, "removed": removed, "remaining": remaining})
        pct = removed / self.n0 * 100 if self.n0 else 0.0
        if self.verbose:
            print(f"  [{name}] 除外 {removed:,} 件（入力の {pct:.2f}%） → 残り {remaining:,} 件")
        if pct > MAX_STEP_DROP_WARN_PCT:
            print(f"  !! WARNING: このステップで入力の {pct:.1f}% を除外しました（警告閾値 {MAX_STEP_DROP_WARN_PCT}%）。"
                  "ルールが広すぎないか、上の内訳を確認してください。")

    def to_frame(self) -> pd.DataFrame:
        t = pd.DataFrame(self.rows)
        t["cum_removed"] = t["removed"].cumsum()
        t["cum_removed_pct"] = t["cum_removed"] / self.n0 * 100
        return t


# =====================================================================
# 2. 追加列の取得（再ロードではなく SELECT のみ）
# =====================================================================
def attach_extra_columns(df: pd.DataFrame, engine) -> pd.DataFrame:
    """ビューに含まれない remarks / use_purpose を transactions から transaction_id で結合する。
    意図: 「DBの再ロード」ではなく既存テーブルへの SELECT だけで、除外ルールに必要な列を揃える。
    何度実行しても同じ結果になる（既にある同名列は置き換える）。"""
    from sqlalchemy import text

    extra = pd.read_sql(text("SELECT transaction_id, use_purpose, remarks FROM realestate.transactions"), engine)
    base = df.drop(columns=[c for c in ("use_purpose", "remarks") if c in df.columns])
    out = base.merge(extra, on="transaction_id", how="left", validate="one_to_one")
    if len(out) != len(df):
        raise RuntimeError("結合で行数が変わりました。transaction_id の一意性を確認してください。")
    print(f"[attach_extra_columns] use_purpose 非NULL {out['use_purpose'].notna().sum():,} / "
          f"remarks 非NULL {out['remarks'].notna().sum():,} / 全 {len(out):,} 件")
    return out


# =====================================================================
# 3. 前処理本体
# =====================================================================
def preprocess(df: pd.DataFrame, verbose: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """除外 → 変数作成 → カテゴリ集約を行い、回帰にそのまま入れられる (df_clean, filter_log) を返す。"""
    missing = [c for c in REQUIRED_RAW_COLS if c not in df.columns]
    if missing:
        hint = "  → remarks / use_purpose は pp.attach_extra_columns(df, engine) で追加できます。" \
            if {"remarks", "use_purpose"} & set(missing) else ""
        raise KeyError(f"入力 df に必要な列がありません: {missing}\n{hint}")

    d = df.copy()
    n0 = len(d)
    log = _StepLog(n0, verbose)
    print(f"[前処理] 入力 {n0:,} 件")

    # -----------------------------------------------------------------
    # ステップ1: 備考(remarks)による除外
    #   意図: 非市場取引・全体価格でない取引を、正規表現で機械的に除く。
    #   処理: NFKC 正規化した備考に各パターンが含まれる行を除外。複数該当は1回だけ数える。
    # -----------------------------------------------------------------
    remarks = _nfkc(d["remarks"])
    m_remark = np.zeros(len(d), dtype=bool)
    for label, pat in REMARK_EXCLUDE_PATTERNS.items():
        hit = _contains(remarks, pat)
        print(f"    - 備考『{label}』 /{pat}/ に該当: {int(hit.sum()):,} 件（重複含む）")
        m_remark |= hit
    d = d.loc[~m_remark].copy()
    log.add("1. 備考(remarks)除外", int(m_remark.sum()), len(d))
    rest = _vc(_nfkc(d["remarks"]), 8)
    if verbose and len(rest):
        print("    （参考）除外後に残っている備考の上位:", rest)

    # -----------------------------------------------------------------
    # ステップ2: 用途(use_purpose)による除外
    #   意図: 居住用の戸建に限定する。
    #   処理: NFKC 後に「住宅」と完全一致する行だけを残す。空欄の扱いは USE_PURPOSE_NULL_POLICY。
    # -----------------------------------------------------------------
    up = _nfkc(d["use_purpose"])
    if verbose:
        print("    （参考）use_purpose の内訳:", _vc(up, 8, dropna=False))
    is_res = (up == USE_PURPOSE_KEEP).fillna(False).to_numpy(dtype=bool)
    is_null = up.isna().to_numpy(dtype=bool)
    if USE_PURPOSE_NULL_POLICY == "keep":
        m_use = ~is_res & ~is_null
    elif USE_PURPOSE_NULL_POLICY == "drop":
        m_use = ~is_res
    else:
        raise ValueError("USE_PURPOSE_NULL_POLICY は 'keep' か 'drop' にしてください。")
    print(f"    - 用途が『{USE_PURPOSE_KEEP}』以外（非NULL）: {int((~is_res & ~is_null).sum()):,} 件 / "
          f"用途NULL: {int(is_null.sum()):,} 件（方針: {USE_PURPOSE_NULL_POLICY}）")
    d = d.loc[~m_use].copy()
    log.add("2. 用途(use_purpose)除外", int(m_use.sum()), len(d))

    # -----------------------------------------------------------------
    # ステップ3: 対数変換と必須変数の欠測除外
    #   意図: 対数が取れない行・主要変数が欠測の行を除く（リストワイズ削除）。
    #   処理: 価格・土地面積・延床面積は 0 以下を欠測扱いにして ln を取り、
    #         ln 3変数・growth・取引時点のいずれかが欠測の行を除外。
    #         （aging の欠測は growth 欠測の部分集合なので、追加の損失は出ない）
    # -----------------------------------------------------------------
    price, land = _num(d["trade_price_yen"]), _num(d["area_sqm"])
    floor_ = _num(d["total_floor_area_sqm"])
    d["ln_price"] = np.log(price.where(price > 0))
    d["ln_land_area"] = np.log(land.where(land > 0))
    d["ln_floor_area"] = np.log(floor_.where(floor_ > 0))
    d["growth"] = _num(d["population_change_rate_15_20"])
    d["aging"] = _num(d["aging_rate_2020"])
    ty, tq = _num(d["transaction_year"]), _num(d["transaction_quarter"])
    need = {
        "ln_price": d["ln_price"].isna(), "ln_land_area": d["ln_land_area"].isna(),
        "ln_floor_area": d["ln_floor_area"].isna(), "growth": d["growth"].isna(),
        "取引年/四半期": ty.isna() | tq.isna(),
    }
    for k, v in need.items():
        print(f"    - {k} が欠測: {int(v.sum()):,} 件")
    n_nonpos = int(((price <= 0) | (land <= 0) | (floor_ <= 0)).sum())
    print(f"    - うち 価格/土地面積/延床面積 が 0 以下: {n_nonpos:,} 件")
    m_miss = np.logical_or.reduce([v.to_numpy(dtype=bool) for v in need.values()])
    d = d.loc[~m_miss].copy()
    log.add("3. 必須変数の欠測除外", int(m_miss.sum()), len(d))
    ty, tq = _num(d["transaction_year"]), _num(d["transaction_quarter"])

    # -----------------------------------------------------------------
    # ステップ3b: 価格の絶対下限（明白な入力誤りの除去）
    #   意図: 0円取引・単位誤り等を除く。ステップ3で 0 以下は除外済みなので、ここは「正だが極小」の行が対象。
    #   処理: trade_price_yen < PRICE_FLOOR_YEN の行を除外（トリムではなく絶対基準）。
    # -----------------------------------------------------------------
    m_floor = (_num(d["trade_price_yen"]) < PRICE_FLOOR_YEN).to_numpy(dtype=bool)
    if m_floor.any() and verbose:
        print(f"    - 価格が {PRICE_FLOOR_YEN:,} 円未満の行（除外）: "
              f"{d.loc[m_floor, 'trade_price_yen'].astype(float).round(0).tolist()[:10]} 円")
    d = d.loc[~m_floor].copy()
    log.add(f"3b. 価格下限(<{PRICE_FLOOR_YEN // 10_000}万円)除外", int(m_floor.sum()), len(d))
    ty, tq = _num(d["transaction_year"]), _num(d["transaction_quarter"])

    # -----------------------------------------------------------------
    # ステップ4: 識別子・固定効果・クラスタ
    #   意図: 市区町村FE・年四半期FE・大字単位のクラスタ頑健SE用の列を作る。
    # -----------------------------------------------------------------
    code = d["municipality_code"].astype(str).str.strip()
    d["city"] = _cat(code.map(CITY), CITY_ORDER, "city")           # 対象4市区以外があればここで止まる
    d["group"] = d["city"].astype(str).map(GROUP)
    d["is_chome_exact"] = (d["match_type"] == "chome_exact").astype(int)
    d["year_quarter"] = _cat(ty.astype(int).astype(str) + "Q" + tq.astype(int).astype(str),
                             sorted((ty.astype(int).astype(str) + "Q" + tq.astype(int).astype(str)).unique()),
                             "year_quarter")
    # growth は (市区町村, district_name) の関数なので、処理割当の単位でクラスタさせる。
    # cluster_oaza は丁目を落とした「大字」単位（採用）、cluster_district は district_name そのまま（比較用）。
    dn = _nfkc(d["district_name"]).fillna("UNKNOWN")
    oaza = dn.str.replace(r"[0-9〇一二三四五六七八九十百]+丁目$", "", regex=True)
    d["cluster_district"] = code + "_" + dn.astype(str)
    d["cluster_oaza"] = code + "_" + oaza.astype(str)

    # -----------------------------------------------------------------
    # ステップ5: 築年数（新築フラグ＋ビン）
    #   意図: 新築は点質量（築0に約3分の1が集中）なので独立させ、中古は関数形を仮定しないビンで扱う。
    #   処理: 築年数 = 取引年 - 建築年 を自前で再計算する（ビューは負値を NULL 化しているため）。
    #         負値は築0に丸め（新築・未完成取引）、presale_flag で識別できるようにする。
    #         建築年が欠測（戦前・空欄）は「不明」ビン。
    # -----------------------------------------------------------------
    age_signed = ty - _num(d["building_year"])
    d["age_signed"] = age_signed
    d["presale_flag"] = (age_signed < 0).astype(int)
    d["age"] = age_signed.clip(lower=0)
    d["new_build"] = (d["age"] == 0).astype(int)
    ab = pd.cut(d["age"], bins=AGE_BINS, labels=AGE_LABELS, right=True)
    d["age_bin"] = ab.cat.as_unordered().cat.add_categories("不明").fillna("不明")
    if "building_age_years" in d.columns:   # 整合性: ビューの築年数と再計算値が（NULL以外で）一致するか
        chk = d["building_age_years"].notna() & (_num(d["building_age_years"]) != age_signed)
        print(f"    - 築年数の整合性: ビュー値と再計算値の不一致 {int(chk.sum()):,} 件（0 が正常）")
    ahead = {int(k): int(v) for k, v in (-age_signed[age_signed < 0]).astype(int).value_counts().sort_index().items()}
    print(f"    - 建築年が取引年より後 → 築0に丸めた行: {int(d['presale_flag'].sum()):,} 件"
          f"（何年後か: {ahead}）")
    if ahead and max(ahead) >= 3:
        print("      注意: 3年以上先の建築年は入力誤りの可能性があります。"
              "全標本の頑健性分析では presale_flag==0 に限定した推定も併記すると安全です。")

    # -----------------------------------------------------------------
    # ステップ6: 駅徒歩のビン化（現行 nearest_station_minutes のみ）
    #   意図: 30分超が NULL 化されている（右側打ち切り）ので、連続値ではなくビンで扱う。
    #   処理: 5分刻みの右閉区間。NULL と 30 以上は「30+/不明」。
    # -----------------------------------------------------------------
    sb = pd.cut(_num(d["nearest_station_minutes"]), bins=STATION_BINS, labels=STATION_LABELS, right=True)
    d["station_bin"] = sb.cat.as_unordered().cat.add_categories(STATION_UNKNOWN).fillna(STATION_UNKNOWN)

    # -----------------------------------------------------------------
    # ステップ7: カテゴリ集約（土地形状・構造・前面道路幅・都市計画区分）
    # -----------------------------------------------------------------
    # 7-1 土地形状: 隣接概念を統合。袋地等（旗竿地等）は接道条件が別種なので分離。未知の値は「不明」。
    ls = _nfkc(d["land_shape"])
    shape = ls.map(LAND_SHAPE_MAP, na_action="ignore")
    unmapped = ls[shape.isna() & ls.notna()]
    if len(unmapped):
        print(f"    - 土地形状: 想定外の値 {len(unmapped):,} 件を『不明』にしました: {_vc(unmapped, 5)}")
    d["land_shape_grp"] = _cat(shape.fillna("不明").astype(str), LAND_SHAPE_ORDER, "land_shape_grp")

    # 7-2 構造: 木造が約87%で他はセルが小さいため 木造 / 非木造 / 不明 の3水準。
    #     混構造（例: RC、木造）も非木造側に入る。
    st = _nfkc(d["building_structure"])
    is_wood = (st == "木造").fillna(False).to_numpy(dtype=bool)
    d["structure_grp"] = _cat(
        np.select([st.isna().to_numpy(dtype=bool), is_wood], ["不明", "木造"], default="非木造"),
        STRUCTURE_ORDER, "structure_grp")

    # 7-3 前面道路幅: 2m（接道義務）と4m（建築基準法上の道路幅員）が閾値。
    #     上限側は「6m以上」に吸収されるので、77m 等の入力誤りに個別のルールは不要。
    rb = pd.cut(_num(d["front_road_breadth_m"]), bins=ROAD_BINS, labels=ROAD_LABELS, right=False)
    d["road_bin"] = rb.cat.as_unordered().cat.add_categories("不明").fillna("不明")

    # 7-4 都市計画区分: キーワード判定（表記ゆれ『2低住専』『第2種低層住居専用地域』等の両方に対応）。
    #     優先順位: 調整区域 > 商業系 > 工業系 > 住居系。どれにも当たらない値は「不明・その他」。
    z = _nfkc(d["city_planning_zone"])
    zone = np.select(
        [z.isna().to_numpy(dtype=bool), _contains(z, "調整"), _contains(z, "商業|近商"),
         _contains(z, "工業|工専|準工"), _contains(z, "住|低層|中高|低専|中専|田園")],
        ["不明・その他", "調整区域", "商業系", "工業系", "住居系"], default="不明・その他")
    d["zone_grp"] = _cat(zone, ZONE_ORDER, "zone_grp")
    other_zone = z[(d["zone_grp"] == "不明・その他").to_numpy() & z.notna().to_numpy()]
    if len(other_zone):
        print(f"    - 都市計画区分: 分類できず『不明・その他』にした値: {_vc(other_zone, 5)}")

    # -----------------------------------------------------------------
    # ステップ8: 感度分析用の列（フィルタはしない。使うかどうかは分析側で選ぶ）
    # -----------------------------------------------------------------
    # 8-1 ln(P2020/P2015): 変化率の右裾（孤立クラスタ）に対する頑健性用。
    #     oaza_mesh_avg 行では「平均人口の比」になり、growth（点ごとの変化率の平均）と定義が異なる。
    p15, p20 = _num(d["population_2015"]), _num(d["population_2020"])
    d["ln_pop_ratio"] = np.log(p20.where(p20 > 0) / p15.where(p15 > 0))
    # 8-2 growth の孤立した高値: ロバストz（中央値・MAD）で機械的に判定。
    med = d["growth"].median()
    mad = (d["growth"] - med).abs().median() * 1.4826
    d["sens_growth_extreme"] = ((d["growth"] - med) / mad > GROWTH_ROBUST_Z) if mad > 0 else False
    # 8-3 価格の p1〜p99 内かどうか（True = 内側）。トリム感度分析は d[d.sens_in_p1_p99] で実行できる。
    lo, hi = d["ln_price"].quantile([0.01, 0.99])
    d["sens_in_p1_p99"] = d["ln_price"].between(lo, hi)

    # -----------------------------------------------------------------
    # ステップ9: 出力の整形と最終サマリ
    # -----------------------------------------------------------------
    df_clean = d[OUTPUT_COLS].reset_index(drop=True)
    _print_summary(df_clean, log, n0, med, mad)
    return df_clean, log.to_frame()


def _print_summary(d: pd.DataFrame, log: _StepLog, n0: int, med: float, mad: float) -> None:
    """最終件数・標本構成・クラスタ数・カテゴリ分布を標準出力する。"""
    n = len(d)
    print("\n[サマリ]")
    print(f"  最終 {n:,} 件 / 入力 {n0:,} 件 / 除外 {n0 - n:,} 件（{(n0 - n) / n0 * 100:.2f}%）")
    assert n0 - n == sum(r["removed"] for r in log.rows), "除外件数の合計が入力−最終と一致しません"
    used = d[d["new_build"] == 0]
    unk = int((used["age_bin"] == "不明").sum())
    print(f"  中古のみ（主分析）: {len(used):,} 件（うち築年数不明 {unk:,}） / "
          f"新築: {int(d['new_build'].sum()):,} 件 / 全標本（頑健性）: {n:,} 件")
    print("  市区町村 × 新築フラグ:\n" + pd.crosstab(d["city"], d["new_build"]).to_string().replace("\n", "\n    "))
    print("  match_type:", _vc(d["match_type"]))
    n_oaza, n_dist = d["cluster_oaza"].nunique(), d["cluster_district"].nunique()
    per = d.groupby("cluster_oaza").size()
    print(f"  クラスタ数: 大字 {n_oaza:,} / district_name {n_dist:,}"
          f"（大字あたり件数: 中央値 {per.median():.0f}, 最大 {per.max():,}）")
    if n_oaza < MIN_CLUSTERS_WARN:
        print(f"  !! WARNING: 大字クラスタ数が {MIN_CLUSTERS_WARN} 未満です。クラスタ頑健SEが過小になる恐れがあります。")
    for col in ["age_bin", "station_bin", "land_shape_grp", "structure_grp", "road_bin", "zone_grp"]:
        print(f"  {col}: {_vc(d[col])}")
    thr = med + GROWTH_ROBUST_Z * mad
    print(f"  感度分析フラグ: growth_extreme {int(d['sens_growth_extreme'].sum()):,} 件"
          f"（growth > {thr:.3f}, クラスタ {d.loc[d['sens_growth_extreme'], 'cluster_oaza'].nunique()} 個）/ "
          f"p1〜p99 の外側 {int((~d['sens_in_p1_p99']).sum()):,} 件")
    print("\n[除外ログ表]\n" + log.to_frame().to_string(index=False, float_format=lambda v: f"{v:,.2f}"))


# =====================================================================
# 4. 分析用ヘルパー
# =====================================================================
def get_sample(df_clean: pd.DataFrame, which: str = "used") -> pd.DataFrame:
    """分析標本を切り出す。which='used': 中古のみ（主分析） / 'all': 全標本（頑健性）。
    意図: 使われないカテゴリ水準（中古のみの『新築』など）が残ると、statsmodels で全ゼロ列 → 特異行列になる。
    処理: 行を選んだあと、未使用カテゴリ水準を落とす。"""
    if which == "used":
        d = df_clean[df_clean["new_build"] == 0].copy()
    elif which == "all":
        d = df_clean.copy()
    else:
        raise ValueError("which は 'used' か 'all'")
    for c in d.select_dtypes("category").columns:
        d[c] = d[c].cat.remove_unused_categories()
    return d


def build_formula(which: str = "used", include_aging: bool = False, include_match_type: bool = True,
                  data: pd.DataFrame | None = None) -> str:
    """モデルA（市区町村FE + growth 主効果 + 年四半期FE）の statsmodels 式を返す。
    - 'all' では growth:new_build（交互作用のみ）を加える。新築の主効果は age_bin の『新築』水準が担うので、
      new_build の主効果を別に入れると完全共線になる。
    - aging は growth と相関が高い可能性があるため、相関確認までは既定で入れない。
    - 間口は方針どおり入れない。
    - data を渡すと、その標本に存在しない基準カテゴリを、最頻カテゴリに自動で置き換える
      （chome_exact のみ等の小標本で patsy が基準カテゴリ不在で落ちるのを防ぐ）。"""
    ref = dict(REF)
    if data is not None:
        for col, level in REF.items():
            if col in data.columns and level not in set(data[col].dropna().unique()):
                ref[col] = data[col].value_counts().idxmax()

    def t(col: str) -> str:
        return f"C({col}, Treatment(reference='{ref[col]}'))"

    rhs = ["growth", "ln_land_area", "ln_floor_area", t("age_bin"), t("station_bin"), t("land_shape_grp"),
           t("structure_grp"), t("road_bin"), t("zone_grp"), t("city"), t("year_quarter")]
    if which == "all":
        rhs.insert(1, "growth:new_build")
    if include_aging:
        rhs.insert(1, "aging")
    if include_match_type:
        rhs.append("is_chome_exact")
    return "ln_price ~ " + " + ".join(rhs)
