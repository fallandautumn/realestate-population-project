# -*- coding: utf-8 -*-
"""
eda_step3_diagnostics.py
========================
Step2 の前処理結果（df_clean）に対する「診断 + 頑健性チェック」を、1回の呼び出しでまとめて実行する。

使い方（Step1 / Step2 の続きのセルで）
--------------------------------------
    import importlib, eda_step2_preprocessing as pp, eda_step3_diagnostics as dg
    importlib.reload(pp); importlib.reload(dg)

    df = pp.attach_extra_columns(df, engine)
    df_clean, filter_log = pp.preprocess(df)
    summary = dg.run_all(df, df_clean)             # ← これだけ（戻り値 = 統合比較表の DataFrame）
    summary                                        # ノートブックでは、そのまま表として表示される

出力（ノートブックの出力欄が途中で切れても読めるよう、ファイルにも保存する）
    output/eda_step3/report.txt               画面に出た内容のすべて（貼り付けはこのファイルの中身でよい）
    output/eda_step3/summary_table.csv        統合比較表（G）
    output/eda_step3/growth_coefficients.csv  全仕様・全項の係数（数値）
    output/eda_step3/loco_growth.csv          R11 のクラスタ別結果（除外したクラスタごとの係数・p値）
    output/eda_step3/main_summary_used.txt    主分析（中古のみ）の statsmodels summary 全文

含まれる内容
    A.  標本構成・クラスタ数・カテゴリ分布
    B.  use_purpose が NULL の行の診断（keep / drop の判断材料）
    C.  価格の分位と両端の行（前処理後にも異常な裾が残っていないか）
    D.  growth 極端値クラスタの一覧、growth と aging の共線性
    D2. 極端値クラスタの母数確認（2015年人口・2020年人口・件数・n_source_points）
    E.  頑健性チェックの実行: R0〜R10, R3b（ln人口比・極端値除外）, R12（use_null ダミー）
    F1. R12 の詳細（use_null 主効果と growth:use_null の検定 → keep 方針の判定）
    F2. R11: Leave-one-cluster-out（全大字クラスタを1つずつ除外して再推定）
    G.  統合比較表 [Spec, Coef, SE, 95% CI, p-value, +0.1あたり変化率, n, clusters]
"""
from __future__ import annotations

import contextlib
import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf

import eda_step2_preprocessing as pp

OUT_DIR = Path("output/eda_step3")
# クラスタ頑健SEの推論に t(G-1) を使う（G=クラスタ数）。クラスタ数が少ないときに正規近似より保守的になる。
# 係数と標準誤差は正規近似の場合と同じで、p値と信頼区間だけが変わる。
USE_T = True


# =====================================================================
# 0. 出力を画面とファイルの両方に出す
# =====================================================================
class _Tee:
    """print の内容を画面とファイルの両方へ書く（ノートブックの出力が切れても、ファイルは全文が残る）。"""

    def __init__(self, path: Path):
        self.f = open(path, "w", encoding="utf-8")
        self.stdout = sys.stdout

    def write(self, s):
        self.stdout.write(s)
        self.f.write(s)

    def flush(self):
        self.stdout.flush()
        self.f.flush()

    def close(self):
        self.f.close()


@contextlib.contextmanager
def _tee_to(path: Path):
    tee = _Tee(path)
    old = sys.stdout
    sys.stdout = tee
    try:
        yield
    finally:
        sys.stdout = old
        tee.close()


def _wlen(s: str) -> int:
    """表示幅（全角=2, 半角=1）。日本語の列を等幅フォントで揃えるために使う。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _pad(s: str, w: int) -> str:
    """表示幅 w になるまで右に空白を足す（左揃え）。"""
    return s + " " * max(0, w - _wlen(s))


def _title(s: str) -> None:
    print("\n" + "=" * 78 + f"\n{s}\n" + "=" * 78)


def _sub(d: pd.DataFrame) -> pd.DataFrame:
    """行を選んだあと、未使用のカテゴリ水準を落とし、index を振り直す（全ゼロ列 → 特異行列を防ぐ）。"""
    d = d.reset_index(drop=True).copy()
    for c in d.select_dtypes("category").columns:
        d[c] = d[c].cat.remove_unused_categories()
    return d


# =====================================================================
# A. 標本構成・クラスタ数・カテゴリ分布
# =====================================================================
def diag_sample(d: pd.DataFrame) -> None:
    _title("A. 標本構成・クラスタ数・カテゴリ分布")
    used = d[d["new_build"] == 0]
    unk = int((used["age_bin"] == "不明").sum())
    print(f"全標本 {len(d):,} 件 / 中古のみ {len(used):,} 件（うち築年数不明 {unk:,}） / 新築 {int(d['new_build'].sum()):,} 件")

    # クラスタ: growth は (市区町村, district_name) の関数なので、実効標本数は取引件数ではなくクラスタ数に近い
    for label, frame in [("全標本", d), ("中古のみ", used)]:
        per = frame.groupby("cluster_oaza").size()
        print(f"[{label}] 大字クラスタ {len(per):,} 個 / district_name クラスタ {frame['cluster_district'].nunique():,} 個 / "
              f"大字あたり件数: 最小 {per.min()}, 中央値 {per.median():.0f}, p90 {per.quantile(.9):.0f}, 最大 {per.max()}")
    top = d.groupby("cluster_oaza").size().sort_values(ascending=False).head(5)
    print("件数が最大の大字クラスタ上位5:", {k: int(v) for k, v in top.items()})
    if d["cluster_oaza"].nunique() < pp.MIN_CLUSTERS_WARN:
        print(f"!! WARNING: 大字クラスタが {pp.MIN_CLUSTERS_WARN} 個未満です。クラスタ頑健SEが過小になる恐れがあります。")

    print("\n市区町村 × 新築フラグ（件数）")
    print(pd.crosstab(d["city"], d["new_build"]).to_string())
    print("\nmatch_type × 新築フラグ（件数）")
    print(pd.crosstab(d["match_type"], d["new_build"]).to_string())

    print("\nカテゴリ変数の分布（全標本）")
    for col in ["age_bin", "station_bin", "land_shape_grp", "structure_grp", "road_bin", "zone_grp"]:
        print(f"  {col}: {pp._vc(d[col])}")

    print("\ngrowth / aging / ln_pop_ratio の要約")
    print(d[["growth", "aging", "ln_pop_ratio"]].describe(percentiles=[.01, .5, .99]).round(3).to_string())
    print(f"\n感度分析フラグ: growth_extreme {int(d['sens_growth_extreme'].sum()):,} 件 / "
          f"価格 p1〜p99 の外側 {int((~d['sens_in_p1_p99']).sum()):,} 件 / presale_flag {int(d['presale_flag'].sum()):,} 件")


# =====================================================================
# B. use_purpose が NULL の行の診断
#    意図: NULL の実体が「住宅」に近いか（keep 妥当）、共同住宅等が混じるか（drop を検討）を数値で判断する。
#    前処理後の df_clean には、use_purpose は「住宅」か NULL のどちらかしか残っていない。
# =====================================================================
def _merge_use(d: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    m = d.merge(df[["transaction_id", "use_purpose"]], on="transaction_id", how="left")
    m["use_null"] = m["use_purpose"].isna()
    m["price_man"] = m["trade_price_yen"] / 1e4
    m["land_sqm"] = np.exp(m["ln_land_area"])
    m["floor_sqm"] = np.exp(m["ln_floor_area"])
    return m


def diag_use_purpose(d: pd.DataFrame, df: pd.DataFrame) -> None:
    _title("B. use_purpose が NULL の行の診断（keep / drop の判断材料）")
    if "use_purpose" not in df.columns:
        print("df に use_purpose がありません（pp.attach_extra_columns を先に実行してください）。スキップします。")
        return
    m = _merge_use(d, df)
    n_null = int(m["use_null"].sum())
    print(f"NULL {n_null:,} 件（{n_null / len(m):.1%}） / 住宅 {len(m) - n_null:,} 件")

    print("\nNULL の割合（属性別）。特定の属性に偏っていれば、NULL は単なる欠測ではなく構造的。")
    for k in ["new_build", "city", "transaction_year", "match_type"]:
        t = m.groupby(k, observed=True)["use_null"].agg(["mean", "sum", "size"])
        t.columns = ["NULL率", "NULL件数", "件数"]
        print(f"\n[{k}]\n" + t.round(3).to_string())

    for label, frame in [("全標本", m), ("中古のみ", m[m["new_build"] == 0])]:
        tab = frame.groupby("use_null")[["floor_sqm", "land_sqm", "price_man"]].quantile([.5, .9, .99]).unstack().round(0)
        tab.index = tab.index.map({True: "NULL", False: "住宅"})
        print(f"\n[{label}] 延床・土地面積（㎡）と価格（万円）の分位: NULL 行と 住宅 行の比較")
        print(tab.to_string())
        if {True, False} <= set(frame["use_null"]):
            r90 = frame.loc[frame.use_null, "floor_sqm"].quantile(.9) / frame.loc[~frame.use_null, "floor_sqm"].quantile(.9)
            r99 = frame.loc[frame.use_null, "floor_sqm"].quantile(.99) / frame.loc[~frame.use_null, "floor_sqm"].quantile(.99)
            print(f"  延床面積の NULL/住宅 比: p90 = {r90:.2f}, p99 = {r99:.2f}"
                  "（1 に近ければ NULL は住宅に近い。1 を大きく超えると右裾が厚く、共同住宅等の混入が疑われる）")


# =====================================================================
# C. 価格の分位と両端の行
#    意図: 備考・用途の機械的な除外のあとに、異常な裾が残っていないかを数値で確認する。
# =====================================================================
def diag_price(d: pd.DataFrame, df: pd.DataFrame) -> None:
    _title("C. 前処理後の価格の分位と、両端の行")
    p = d["trade_price_yen"] / 1e4
    q = p.quantile([0, .001, .01, .05, .5, .95, .99, .999, 1]).round(0)
    print("価格（万円）の分位:\n" + q.to_string())
    print(f"\n10万円未満 {int((p < pp.PRICE_FLOOR_YEN / 1e4).sum()):,} 件（前処理の絶対下限で除外済みなら 0 が正常） / "
          f"100万円未満 {int((p < 100).sum()):,} 件 / 1億円超 {int((p > 1e4).sum()):,} 件 / 10億円超 {int((p > 1e5).sum()):,} 件")
    if "use_purpose" in df.columns:
        m = _merge_use(d, df)
        cols = ["city", "price_man", "land_sqm", "floor_sqm", "age_bin", "match_type", "use_purpose"]
        print("\n価格が最も低い5件:\n" + m.nsmallest(5, "trade_price_yen")[cols].round(0).to_string(index=False))
        print("\n価格が最も高い5件:\n" + m.nlargest(5, "trade_price_yen")[cols].round(0).to_string(index=False))


# =====================================================================
# D. growth 極端値クラスタ・growth と aging の共線性
# =====================================================================
def diag_growth(d: pd.DataFrame) -> None:
    _title("D. growth の極端値クラスタと、aging との共線性")
    ext = d[d["sens_growth_extreme"]]
    if len(ext) == 0:
        print("growth_extreme に該当する行はありません。")
    else:
        t = ext.groupby(["city", "cluster_oaza"], observed=True).agg(
            n=("growth", "size"), growth=("growth", "mean"), ln_pop_ratio=("ln_pop_ratio", "mean"),
            n_source_points=("n_source_points", "mean"), price_man_median=("trade_price_yen", lambda s: s.median() / 1e4),
            chome_exact_share=("is_chome_exact", "mean")).sort_values("n", ascending=False)
        print(f"growth_extreme: {len(ext):,} 件 / クラスタ {ext['cluster_oaza'].nunique()} 個（頑健性 R3 で除外して推定する）")
        print(t.round(3).head(20).to_string())

    used = d[d["new_build"] == 0]
    g_w = used["growth"] - used.groupby("city", observed=True)["growth"].transform("mean")
    a_w = used["aging"] - used.groupby("city", observed=True)["aging"].transform("mean")
    r_w, r_raw = g_w.corr(a_w), used["growth"].corr(used["aging"])
    print(f"\n中古のみ: corr(growth, aging) 全体 = {r_raw:+.3f} / 市区町村内 = {r_w:+.3f}"
          f" → 市区町村内での VIF(growth) ≈ {1 / (1 - r_w ** 2):.2f}（2変数の場合。5〜10 以上なら同時投入は不安定）")


# =====================================================================
# D2. growth 極端値クラスタの母数（分母）確認
#     意図: 変化率 = (P2020 − P2015) / P2015 は、分母 P2015 が小さいほど不安定になる。
#           極端値クラスタが「小さい母数からの変化率」なのか「大きな人口増」なのかを、実数で確認する。
#     処理: df_clean に無い population_2015 / population_2020 を transaction_id で df から結合し、
#           クラスタ単位で集計する。全クラスタの分布と並べて、その2クラスタの位置（百分位）を示す。
# =====================================================================
def diag_extreme_clusters(d: pd.DataFrame, df: pd.DataFrame) -> None:
    _title("D2. growth 極端値クラスタの母数確認（2015年人口・2020年人口・件数・n_source_points）")
    ext = d[d["sens_growth_extreme"]]
    if len(ext) == 0:
        print("growth_extreme に該当する行はありません。")
        return
    need = ["transaction_id", "population_2015", "population_2020"]
    if not set(need) <= set(df.columns):
        print("df に population_2015 / population_2020 がありません。スキップします。")
        return
    m = d.merge(df[need].drop_duplicates("transaction_id"), on="transaction_id", how="left")
    g = m.groupby("cluster_oaza", observed=True).agg(
        city=("city", "first"),
        n=("growth", "size"),
        n_used=("new_build", lambda s: int((s == 0).sum())),
        n_new=("new_build", "sum"),
        n_flagged=("sens_growth_extreme", "sum"),
        growth=("growth", "mean"),
        pop_2015=("population_2015", "mean"),
        pop_2020=("population_2020", "mean"),
        n_source_points=("n_source_points", "mean"),
        chome_exact_share=("is_chome_exact", "mean"),
        price_man_median=("trade_price_yen", lambda s: s.median() / 1e4),
    )
    g["growth_ratio_of_means"] = g["pop_2020"] / g["pop_2015"] - 1
    g["pop_2015_pctile"] = g["pop_2015"].rank(pct=True) * 100
    g["pop_2020_pctile"] = g["pop_2020"].rank(pct=True) * 100

    ext_cl = g[g["n_flagged"] > 0].sort_values("n_flagged", ascending=False)
    cols = ["city", "n", "n_used", "n_new", "n_flagged", "growth", "growth_ratio_of_means", "pop_2015", "pop_2020",
            "pop_2015_pctile", "pop_2020_pctile", "n_source_points", "chome_exact_share", "price_man_median"]
    with pd.option_context("display.unicode.east_asian_width", True, "display.width", 250, "display.max_columns", 30):
        print(f"極端値クラスタ {len(ext_cl)} 個（growth_extreme に該当する行を含むクラスタ）:")
        print(ext_cl[cols].round(3).to_string())
        ref = g[["pop_2015", "pop_2020", "growth", "growth_ratio_of_means"]].quantile([.05, .10, .25, .5, .75, .90, .95, .99]).round(3)
        print(f"\n全 {len(g)} クラスタの分布（クラスタ平均値の分位）:")
        print(ref.to_string())
    print("\n読み方:")
    print("  - pop_* は 500m メッシュの人口（人）。oaza_mesh_avg 行は『丁目代表点メッシュの平均』。")
    print("  - growth は点ごとの変化率の平均。growth_ratio_of_means は pop_2020/pop_2015−1（平均の比）で、定義が異なるため両方を示す。")
    print("  - pop_2015_pctile が低い（下位）ほど『小さい母数からの変化率』。n_source_points は大字平均に使った代表点の数。")
    print("  - n_flagged < n の場合、極端値フラグは行単位（R3/R3b は行単位で除外）なので、同クラスタ内に非該当行が残っている。")


# =====================================================================
# E. 頑健性チェック（growth 等の係数を並べる）
# =====================================================================
def _fit(formula: str, d: pd.DataFrame, cluster_col: str = "cluster_oaza"):
    """OLS + クラスタ頑健SE。NaN で落ちる行があっても、クラスタの群ラベルを残った行に揃える。"""
    mod = smf.ols(formula, data=d)
    idx = mod.data.row_labels
    groups = pd.factorize(d.loc[idx, cluster_col])[0]
    res = mod.fit(cov_type="cluster", cov_kwds={"groups": groups}, use_t=USE_T)
    rank_ok = int(np.linalg.matrix_rank(mod.exog)) == mod.exog.shape[1]
    return res, int(groups.max() + 1), rank_ok


def _implied(res, weights: dict[str, float]):
    """係数の線形結合（例: growth + growth:new_build）の推定値・標準誤差・信頼区間・p値。"""
    R = np.zeros(len(res.params))
    for name, w in weights.items():
        R[res.params.index.get_loc(name)] = w
    tt = res.t_test(R)
    ci = np.asarray(tt.conf_int()).reshape(-1)
    return (float(np.squeeze(tt.effect)), float(np.squeeze(tt.sd)), float(ci[0]), float(ci[1]), float(np.squeeze(tt.pvalue)))


def _spec_key(spec: str) -> tuple[int, str]:
    """'R3b …' → (3, 'b')。R番号の自然順（R3 < R3b < R4 … < R10）に並べるためのキー。"""
    mt = re.match(r"R(\d+)(b?)", spec)
    return (int(mt.group(1)), mt.group(2)) if mt else (999, "")


def run_robustness(d_clean: pd.DataFrame, df: pd.DataFrame | None = None):
    """R0〜R10, R3b, R12 を実行し、係数を1つの長い表にまとめて返す。
    戻り値: (tab, main_res, ctx)。ctx は後段（R12 詳細・R11）に渡す標本と推定結果。"""
    _title("E. 頑健性チェックの実行（被説明変数 ln(価格)・大字クラスタ頑健SE・p値/CIは t(G−1)）")
    used = _sub(d_clean[d_clean["new_build"] == 0])
    allS = _sub(d_clean)
    rows: list[dict] = []
    ctx: dict = {"used": used, "res12": None, "ncl12": None}
    counter = {"i": 0}

    def run(spec, note, data, formula, terms, implied=None, labels=None):
        """1つの仕様を推定して rows に追加。失敗しても全体は止めない。
        labels: {項名: 表示用ラベル}（交互作用モデルで、どの傾きかを表に明記するため）。
        implied: {ラベル: {項名: 重み}}（係数の線形結合。例: 新築の傾き = growth + growth:new_build）。"""
        try:
            res, ncl, rank_ok = _fit(formula, data)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {spec} は推定に失敗: {type(e).__name__}: {e}")
            rows.append(dict(spec=spec, term="(ERROR)", note=f"{note} / {type(e).__name__}: {e}"))
            return None, None
        ci = res.conf_int()
        base = dict(spec=spec, n=int(res.nobs), n_clusters=ncl, rank_ok=rank_ok, note=note)
        labels = labels or {}
        first = None
        for t in terms:
            if t in res.params.index:
                counter["i"] += 1
                rows.append({**base, "term": t, "label": labels.get(t, ""), "_ord": counter["i"], "coef": res.params[t],
                             "se": res.bse[t], "ci_low": ci.loc[t, 0], "ci_high": ci.loc[t, 1], "p": res.pvalues[t]})
                first = first if first is not None else t
        for label, w in (implied or {}).items():
            c, se, lo, hi, p = _implied(res, w)
            counter["i"] += 1
            rows.append({**base, "term": f"{terms[0]}（{label}）", "label": label, "_ord": counter["i"],
                         "coef": c, "se": se, "ci_low": lo, "ci_high": hi, "p": p})
        if first is not None:
            print(f"  {_pad(spec, 34)} {first}: coef = {res.params[first]:+.4f}  (SE {res.bse[first]:.4f}, "
                  f"p = {res.pvalues[first]:.4f}, n = {int(res.nobs):,}, G = {ncl})")
        return res, ncl

    F = pp.build_formula

    # 用途NULLの識別列（R2 / R12 で共用）。df に use_purpose があるときだけ作る。
    usedm = None
    if df is not None and "use_purpose" in df.columns:
        usedm = used.merge(df[["transaction_id", "use_purpose"]].drop_duplicates("transaction_id"),
                           on="transaction_id", how="left")
        usedm["use_null"] = usedm["use_purpose"].isna().astype(int)

    # R0 主分析 -----------------------------------------------------------
    # （旧 R0b: SE のクラスタ単位比較は、district_name が既に大字粒度で同一分割だったため削除）
    res0, _ = run("R0 主分析", "中古のみ / 用途NULLは残す / 大字クラスタ", used, F("used", data=used), ["growth"])
    ctx["main_res"] = res0

    # R1 全標本（新築を含む）: 新築の傾きを別に推定 ---------------------------
    run("R1 全標本+新築交互作用", "growth = 中古の傾き / growth:new_build = 新築との差", allS, F("all", data=allS),
        ["growth", "growth:new_build"], labels={"growth": "中古の傾き", "growth:new_build": "新築との差"},
        implied={"新築の傾き": {"growth": 1, "growth:new_build": 1}})

    # R2 用途NULLを除外（drop）--------------------------------------------
    if usedm is not None:
        u2 = _sub(usedm[usedm["use_null"] == 0])
        run("R2 用途NULLを除外", "use_purpose='住宅' の行のみ（drop 方針と同じ）", u2, F("used", data=u2), ["growth"])
    else:
        print("  R2 はスキップ（df に use_purpose がありません）")

    # R3 / R3b growth 極端値クラスタを除外 --------------------------------
    ext_mask = used["sens_growth_extreme"].to_numpy(dtype=bool)
    if ext_mask.any():
        u3 = _sub(used[~ext_mask])
        run("R3 growth極端値を除外", f"孤立した高値 {int(ext_mask.sum())} 件（{used.loc[ext_mask, 'cluster_oaza'].nunique()} クラスタ）を除く",
            u3, F("used", data=u3), ["growth"])
        # R3b: 説明変数を ln(P2020/P2015) にして、同じ極端値を除外（対称変換で影響力が解消するかの検証）
        u3b = _sub(u3.dropna(subset=["ln_pop_ratio"]))
        f3b = F("used", data=u3b).replace("ln_price ~ growth", "ln_price ~ ln_pop_ratio", 1)
        run("R3b ln(人口比)・極端値除外", "説明変数=ln(P2020/P2015)、R3 と同じ極端値を除外", u3b, f3b, ["ln_pop_ratio"])
    else:
        print("  R3 / R3b はスキップ（growth 極端値の該当なし）")

    # R4 ln(2020人口/2015人口) を説明変数に -------------------------------
    u4 = _sub(used.dropna(subset=["ln_pop_ratio"]))
    f4 = F("used", data=u4).replace("ln_price ~ growth", "ln_price ~ ln_pop_ratio", 1)
    run("R4 ln(人口比)", "growth の代わりに ln(P2020/P2015)（対称的で外れ値に強い指標）", u4, f4, ["ln_pop_ratio"])

    # R5 価格 p1〜p99 のみ ------------------------------------------------
    u5 = _sub(used[used["sens_in_p1_p99"]])
    run("R5 価格p1〜p99のみ", "価格の両端1%を除く", u5, F("used", data=u5), ["growth"])

    # R6 / R10 空間結合の精度別（測定誤差の確認）---------------------------
    #   match_type ダミーは標本内で定数になるので式から外す
    u6 = _sub(used[used["is_chome_exact"] == 1])
    run("R6 chome_exact のみ", "丁目レベルで結合できた行（測定誤差が小さい側・標本小）", u6,
        F("used", include_match_type=False, data=u6), ["growth"])
    u10 = _sub(used[used["is_chome_exact"] == 0])
    run("R10 oaza_mesh_avg のみ", "大字平均で近似した行", u10, F("used", include_match_type=False, data=u10), ["growth"])

    # R7 growth × is_chome_exact -----------------------------------------
    run("R7 growth×chome_exact", "growth = 大字平均行の傾き / 交互作用 = chome_exact との差", used,
        F("used", data=used) + " + growth:is_chome_exact", ["growth", "growth:is_chome_exact"],
        labels={"growth": "大字平均行の傾き", "growth:is_chome_exact": "chome_exactとの差"},
        implied={"chome_exact の傾き": {"growth": 1, "growth:is_chome_exact": 1}})

    # R8 growth × 人口減少群 -----------------------------------------------
    u8 = used.copy()
    u8["grp_decline"] = (u8["group"] == "人口減少").astype(int)
    run("R8 growth×人口減少群", "growth = 安定・増加群の傾き / 交互作用 = 減少群との差", u8,
        F("used", data=u8) + " + growth:grp_decline", ["growth", "growth:grp_decline"],
        labels={"growth": "安定・増加群の傾き", "growth:grp_decline": "減少群との差"},
        implied={"人口減少群の傾き": {"growth": 1, "growth:grp_decline": 1}})

    # R9 aging を追加 ------------------------------------------------------
    run("R9 aging を追加", "65歳以上比率を同時投入（共線性の影響を見る）", used, F("used", include_aging=True, data=used),
        ["growth", "aging"], labels={"aging": "aging係数"})

    # R12 use_null ダミー -----------------------------------------------------
    #   意図: 用途NULL行が住宅行と系統的に違うか（価格水準の差 = use_null、growth の傾きの差 = growth:use_null）を、
    #         共変量・年四半期FEを統制したうえで検定する。差がなければ keep 方針（NULLを残す）を支持する。
    if usedm is None:
        print("  R12 はスキップ（df に use_purpose がありません）")
    elif usedm["use_null"].nunique() < 2:
        print("  R12 はスキップ（use_null が定数です。USE_PURPOSE_NULL_POLICY='drop' の場合など）")
    else:
        u12 = _sub(usedm)
        res12, ncl12 = run("R12 use_null ダミー", "growth + use_null + growth:use_null（NULL=1, 住宅=0）", u12,
                           F("used", data=u12) + " + use_null + growth:use_null",
                           ["growth", "use_null", "growth:use_null"],
                           labels={"growth": "住宅行の傾き", "use_null": "NULLダミー主効果",
                                   "growth:use_null": "傾きの差 NULL−住宅"},
                           implied={"NULL行の傾き": {"growth": 1, "growth:use_null": 1}})
        ctx["res12"], ctx["ncl12"] = res12, ncl12

    tab = pd.DataFrame(rows)
    if "coef" in tab:
        # 説明変数が 0.1 動いたときの価格変化（%）。growth は変化率(0.1=10ポイント)、ln_pop_ratio は対数比(0.1≒10.5%)
        tab["pct_per_0.1"] = (np.exp(tab["coef"] * 0.1) - 1) * 100
    if "rank_ok" in tab and (tab["rank_ok"] == False).any():  # noqa: E712
        print("\n!! rank_ok=False の仕様があります。標本が小さく説明変数が共線のため、係数の信頼性が低いです。")
    return tab, res0, ctx


# =====================================================================
# F1. R12 の詳細（use_purpose NULL の識別検証）
# =====================================================================
def report_r12(res, ncl) -> None:
    _title("F1. R12: use_purpose NULL の識別検証（keep 方針の正当性）")
    if res is None or "use_null" not in res.params.index:
        print("R12 は実行されていません（df に use_purpose が無い、または use_null が定数）。")
        return
    ci = res.conf_int()

    def show(term: str, label: str) -> tuple[float, float, float, float, float]:
        c, se, lo, hi, p = res.params[term], res.bse[term], ci.loc[term, 0], ci.loc[term, 1], res.pvalues[term]
        print(f"  {label:<44s} coef = {c:+.4f}  SE = {se:.4f}  95%CI [{lo:+.4f}, {hi:+.4f}]  p = {p:.4f}")
        return c, se, lo, hi, p

    print(f"n = {int(res.nobs):,} / 大字クラスタ {ncl}（NULL = 1, 住宅 = 0。年四半期FE・市区町村FE・全共変量を統制）")
    show("growth", "growth（住宅行の傾き）")
    c, se, lo, hi, p = show("use_null", "use_null 主効果（growth=0 での NULL−住宅）")
    print(f"      → 価格差 {np.expm1(c) * 100:+.1f}%（95%CI {np.expm1(lo) * 100:+.1f}% 〜 {np.expm1(hi) * 100:+.1f}%）")
    c2, se2, lo2, hi2, p2 = show("growth:use_null", "growth:use_null（傾きの差 NULL−住宅）")
    ci_, se_, lo_, hi_, p_ = _implied(res, {"growth": 1, "growth:use_null": 1})
    print(f"  {'growth（NULL行の傾き）':<44s} coef = {ci_:+.4f}  SE = {se_:.4f}  95%CI [{lo_:+.4f}, {hi_:+.4f}]  p = {p_:.4f}")

    main_zero, slope_zero = lo <= 0 <= hi, lo2 <= 0 <= hi2
    print("\n判定:")
    print(f"  use_null 主効果の 95%CI が 0 を{'含む' if main_zero else '含まない'} / "
          f"傾きの差の 95%CI が 0 を{'含む' if slope_zero else '含まない'}")
    if main_zero and slope_zero:
        print("  → 共変量と年四半期FEを統制すると、NULL 行は 住宅 行と（価格水準・growth の傾きとも）系統的に区別できない。keep 方針を支持。")
        print(f"    ただし『差がない』の証明ではない。検出できる差は CI 幅が上限（価格水準 ±{max(abs(np.expm1(lo)), abs(np.expm1(hi))) * 100:.1f}% 程度まで）。")
    else:
        print("  → NULL 行が 住宅 行と系統的に異なる可能性がある。R2（NULL 除外）を主分析候補に格上げして再検討すること。")
        print("    （USE_PURPOSE_NULL_POLICY を 'drop' に切り替えて前処理を再実行すれば、R2 と同じ標本になる）")


# =====================================================================
# F2. R11: Leave-one-cluster-out（影響力の大きい大字クラスタの特定）
#     意図: growth は大字ごとに1つの値なので、実効標本はクラスタ数。特定のクラスタ1つが結論を左右していないかを、
#           全クラスタを1つずつ外して再推定して確認する（R3 の『2クラスタ除外』を全クラスタに一般化）。
#     注意: 影響の大きいクラスタを見つけるための診断であり、それを除外する根拠にはしない（結果依存の削除になるため）。
#     処理: 設計行列を1回だけ作り、行マスクで再推定する（式の再評価をしないので速い）。
#           除外で水準が空になったダミー列（全ゼロ）は自動で落とす。
# =====================================================================
LOCO_TARGETS = ("growth",)     # 対象の説明変数。('growth', 'ln_pop_ratio') にすると両方を実行する
LOCO_ALPHA = 0.05


def loco(d: pd.DataFrame, var: str = "growth", cluster_col: str = "cluster_oaza"):
    """全クラスタを1つずつ除外して再推定。戻り値: (クラスタ別の表, 全標本の (coef, se, p, G))。"""
    u = _sub(d.dropna(subset=[var]))
    f = pp.build_formula("used", data=u)
    if var != "growth":
        f = f.replace("ln_price ~ growth", f"ln_price ~ {var}", 1)
    mod = smf.ols(f, data=u)
    idx = mod.data.row_labels
    y, X = np.asarray(mod.endog, dtype=float), np.asarray(mod.exog, dtype=float)
    j_full = list(mod.exog_names).index(var)
    codes, uniq = pd.factorize(u.loc[idx, cluster_col].to_numpy())

    def fit(mask: np.ndarray):
        keep = np.any(X[mask] != 0, axis=0)                  # 全ゼロになった水準ダミーを落とす
        g = pd.factorize(codes[mask])[0]
        res = sm.OLS(y[mask], X[mask][:, keep]).fit(cov_type="cluster", cov_kwds={"groups": g}, use_t=USE_T)
        j = int(keep[:j_full].sum())
        return float(res.params[j]), float(res.bse[j]), float(res.pvalues[j]), int(g.max() + 1)

    base = fit(np.ones(len(y), dtype=bool))
    coef0, se0, _, _ = base
    out = []
    for gi, name in enumerate(uniq):
        mask = codes != gi
        c, se, p, G = fit(mask)
        out.append(dict(cluster=name, n_rows=int((~mask).sum()), coef_loo=c, se_loo=se, p_loo=p,
                        delta=c - coef0, delta_over_se=(c - coef0) / se0, sign_flip=bool(np.sign(c) != np.sign(coef0)),
                        p_above_alpha=bool(p > LOCO_ALPHA)))
    return pd.DataFrame(out), base


def report_loco(t: pd.DataFrame, base, var: str) -> None:
    coef0, se0, p0, G = base
    print(f"対象: {var} / 全標本の推定（{G} クラスタ）: coef = {coef0:+.4f}, SE = {se0:.4f}, p = {p0:.4f}")
    a = t.reindex(t["delta_over_se"].abs().sort_values(ascending=False).index)
    top = a.iloc[0]
    direction = "上がる" if top["delta"] > 0 else "下がる"
    print(f"\n1. max |Δcoef| / SE = {abs(top['delta_over_se']):.3f}  （クラスタ: {top['cluster']} / {int(top['n_rows'])} 件 / "
          f"除外すると係数が {direction}: {coef0:+.4f} → {top['coef_loo']:+.4f}）")
    print(f"   参考: 2/√G = {2 / np.sqrt(G):.3f}（観測値レベルの DFBETAS の目安を転用したもの。クラスタ単位の厳密な閾値ではない）")
    print("   影響の大きい上位5クラスタ:")
    with pd.option_context("display.unicode.east_asian_width", True, "display.width", 200):
        print(a.head(5)[["cluster", "n_rows", "coef_loo", "p_loo", "delta", "delta_over_se"]].round(4).to_string(index=False))

    lo, hi = t.loc[t["coef_loo"].idxmin()], t.loc[t["coef_loo"].idxmax()]
    print(f"\n2. 係数の範囲: 最小 {lo['coef_loo']:+.4f}（{lo['cluster']} を除外） 〜 最大 {hi['coef_loo']:+.4f}（{hi['cluster']} を除外）"
          f" / 幅 {hi['coef_loo'] - lo['coef_loo']:.4f} = 全標本SEの {(hi['coef_loo'] - lo['coef_loo']) / se0:.2f} 倍")

    ns = t[t["p_above_alpha"]].sort_values("p_loo", ascending=False)
    print(f"\n3. 除外すると p > {LOCO_ALPHA}（有意でなくなる）クラスタ: {len(ns)} / {len(t)} 個")
    if len(ns):
        with pd.option_context("display.unicode.east_asian_width", True, "display.width", 200):
            print(ns.head(15)[["cluster", "n_rows", "coef_loo", "se_loo", "p_loo"]].round(4).to_string(index=False))
        if len(ns) > 15:
            print(f"   （上位15件のみ表示。全件は CSV）")
        print(f"   該当クラスタの合計件数: {int(ns['n_rows'].sum()):,} 件")
    n_flip = int(t["sign_flip"].sum())
    print(f"\n4. 除外すると係数の符号が反転するクラスタ: {n_flip} 個")


def run_loco_all(used: pd.DataFrame, out: Path) -> None:
    _title("F2. R11: Leave-one-cluster-out（全大字クラスタを1つずつ除外して再推定）")
    t0 = time.time()
    for var in LOCO_TARGETS:
        if var not in used.columns:
            print(f"{var} が df_clean にありません。スキップします。")
            continue
        print(f"[{var}] 再推定中…")
        t, base = loco(used, var)
        print(f"  完了: {len(t)} 回 / {time.time() - t0:.1f} 秒\n")
        report_loco(t, base, var)
        t.to_csv(out / f"loco_{var}.csv", index=False, encoding="utf-8-sig")


# =====================================================================
# G. 統合比較表
# =====================================================================
def make_summary(tab: pd.DataFrame) -> pd.DataFrame:
    """全仕様の係数を、指定の列 [Spec, Coef, SE, 95% CI, p-value, +0.1あたり変化率, n, clusters] の表に整形する。
    交互作用モデルは『どの傾きか』を Spec に明記した行に展開する。"""
    t = tab[tab.get("coef").notna()].copy() if "coef" in tab else tab.iloc[0:0].copy()
    t["_k"] = t["spec"].map(_spec_key)
    t = t.sort_values(["_k", "_ord"], kind="stable")

    def label(r) -> str:
        lab = r.get("label", "")
        return f"{r['spec']}（{lab}）" if isinstance(lab, str) and lab else r["spec"]

    no_pct = {"aging", "use_null"}                 # 単位が『0.1 の変化』に馴染まない項は空欄
    out = pd.DataFrame({
        "Spec": t.apply(label, axis=1),
        "Coef": t["coef"].map(lambda v: f"{v:+.4f}"),
        "SE": t["se"].map(lambda v: f"{v:.4f}"),
        "95% CI": [f"[{lo:+.3f}, {hi:+.3f}]" for lo, hi in zip(t["ci_low"], t["ci_high"])],
        "p-value": t["p"].map(lambda v: "<0.0001" if v < 1e-4 else f"{v:.4f}"),
        "+0.1あたり変化率": ["—" if term in no_pct else f"{v:+.1f}%" for term, v in zip(t["term"], t["pct_per_0.1"])],
        "n": t["n"].map(lambda v: f"{int(v):,}"),
        "clusters": t["n_clusters"].astype(int),
    })
    return out.reset_index(drop=True)


def print_summary(summary: pd.DataFrame, tab: pd.DataFrame) -> None:
    _title("G. 統合比較表: 頑健性チェック（被説明変数 ln(価格)・大字クラスタ頑健SE・p値/CIは t(G−1)）")
    disp = summary.copy()
    w = max([_wlen(x) for x in disp["Spec"]] + [4])
    disp["Spec"] = disp["Spec"].map(lambda x: _pad(x, w))          # Spec 列を左揃えにする（全角幅を考慮）
    with pd.option_context("display.unicode.east_asian_width", True, "display.width", 250,
                           "display.max_colwidth", 100, "display.max_columns", 20):
        print(disp.to_string(index=False, justify="left"))
    print("\n単位: R3b・R4 の説明変数は ln(P2020/P2015)（0.10 ≒ 人口比 +10.5%）。それ以外は growth（変化率。0.10 = 人口が10%増）。")
    print("Coef は ln(価格) の変化。『+0.1あたり変化率』は説明変数が 0.1 増えたときの価格変化 = (exp(Coef×0.1)−1)×100。")
    print("交互作用モデルの『（… の傾き）』は、その群の傾きそのもの。『… との差』は傾きの差。")
    print("R9 の aging 係数と R12 の NULLダミー主効果は単位が異なる（『+0.1あたり』は —）。NULLダミーは growth=0 での NULL−住宅 の ln 価格差（詳細は F1）。")
    if "term" in tab and (tab["term"] == "(ERROR)").any():
        bad = tab.loc[tab["term"] == "(ERROR)", "spec"].tolist()
        print(f"!! 推定に失敗した仕様（上の表には出ません）: {bad}")


# =====================================================================
# 一括実行
# =====================================================================
def run_all(df: pd.DataFrame, df_clean: pd.DataFrame, out_dir: str | Path = OUT_DIR) -> pd.DataFrame:
    """A〜G をすべて実行し、統合比較表（DataFrame）を返す。
    保存: report.txt / growth_coefficients.csv（全項・数値） / summary_table.csv（整形表） /
          loco_growth.csv（R11 のクラスタ別） / main_summary_used.txt（R0 の statsmodels summary）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with _tee_to(out / "report.txt"):
        diag_sample(df_clean)
        diag_use_purpose(df_clean, df)
        diag_price(df_clean, df)
        diag_growth(df_clean)
        diag_extreme_clusters(df_clean, df)
        tab, main_res, ctx = run_robustness(df_clean, df)
        report_r12(ctx["res12"], ctx["ncl12"])
        run_loco_all(ctx["used"], out)
        summary = make_summary(tab)
        print_summary(summary, tab)
        tab.drop(columns=[c for c in ("_ord",) if c in tab.columns]).to_csv(
            out / "growth_coefficients.csv", index=False, encoding="utf-8-sig")
        summary.to_csv(out / "summary_table.csv", index=False, encoding="utf-8-sig")
        if main_res is not None:
            (out / "main_summary_used.txt").write_text(str(main_res.summary()), encoding="utf-8")
        print(f"\n保存先: {out.resolve()}\n  - report.txt（この画面出力の全文）\n  - summary_table.csv（統合比較表）"
              "\n  - growth_coefficients.csv（全項の数値）\n  - loco_growth.csv（R11 のクラスタ別）\n  - main_summary_used.txt")
    return summary
