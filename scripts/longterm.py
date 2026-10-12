"""年単位の長い流れ（家計調査と国民生活に関する世論調査）をまとめて docs/data/longterm.json を作る。

家計調査は e-Stat から毎回取り直す。世論調査は内閣府のサイトがブラウザ以外からの取得を
受け付けないので、sources/cao/ に置いた CSV を読む（年に1回、手で差し替える）。

使い方:
    python scripts/longterm.py
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

from collect import DATA, fetch

ROOT = Path(__file__).resolve().parent.parent
CAO = ROOT / "sources" / "cao"
ESTAT = "https://www.e-stat.go.jp/stat-search/file-download?statInfId={}&fileKind=1"

# 家計調査 長期時系列 表1（二人以上の世帯、小分類、2000年〜）
KAKEI_LONG = "000040270652"
# 品目分類の年次表（全品目）。小分類より細かい品目はこちらにしかない
KAKEI_DETAIL = ("000040270655", "000040270656", "000040409801")  # 2015〜19, 2020〜24, 2025〜

# 画面に出す品目。(表示名, 長期表の品目名)
LONG_ITEMS = [
    ("消費支出（全体）", "消費支出"),
    ("外食", "外食"),
    ("調理食品（総菜・弁当など）", "調理食品"),
    ("米", "米"),
    ("生鮮魚介", "生鮮魚介"),
    ("酒類", "酒類"),
    ("コーヒー・ココア", "コーヒー・ココア"),
    ("通信", "通信"),
    ("書籍・他の印刷物", "書籍・他の印刷物"),
    ("宿泊料", "宿泊料"),
    ("パック旅行費", "パック旅行費"),
    ("理美容サービス", "理美容サービス"),
    ("たばこ", "たばこ"),
    ("こづかい（使途不明）", "こづかい(使途不明)"),
]
# (表示名, 符号) 符号は品目改定をまたいでも同じ
DETAIL_ITEMS = [
    ("携帯電話通信料", "763"),
    ("新聞", "850"),
    ("喫茶代", "397"),
]


def _csv_rows(raw: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(raw.decode("cp932"))))


def _year(s: str) -> int | None:
    m = re.match(r"(\d{4})年", s.strip())
    return int(m.group(1)) if m else None


def _num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def kakei() -> dict:
    rows = _csv_rows(fetch(ESTAT.format(KAKEI_LONG)))
    years = [_year(c) for c in rows[1][6:]]
    by_name: dict[str, list] = {}
    for r in rows[3:]:
        if len(r) > 6 and r[5] not in by_name:
            by_name[r[5]] = r[6:]
    items = []
    for label, name in LONG_ITEMS:
        vals = by_name.get(name)
        if not vals:
            raise ValueError(f"家計調査の長期表に「{name}」がありません")
        items.append({"name": label, "points": [[y, _num(v)] for y, v in zip(years, vals) if y and _num(v) is not None]})

    detail: dict[str, dict[int, float]] = {code: {} for _, code in DETAIL_ITEMS}
    for sid in KAKEI_DETAIL:
        rows = _csv_rows(fetch(ESTAT.format(sid)))
        ys = [_year(c) for c in rows[1][8:]]
        for r in rows[3:]:
            if len(r) > 8 and r[6] in detail:
                for y, v in zip(ys, r[8:]):
                    if y and _num(v) is not None:
                        detail[r[6]][y] = _num(v)
    for label, code in DETAIL_ITEMS:
        items.append({"name": label, "points": [[y, v] for y, v in sorted(detail[code].items())]})
    # 取得元の形が変わって一部の品目が欠けたまま保存しないよう、全品目の年数を確かめる
    # 長期表は2000年から、品目分類の年次表は2015年からなので、基準の年数を分ける
    for i, it in enumerate(items):
        need = 20 if i < len(LONG_ITEMS) else 10
        if len(it["points"]) < need:
            raise ValueError(f"家計調査「{it['name']}」の年数が {len(it['points'])} 年しかありません（{need} 年以上のはず）")
    return {
        "unit": "円（1世帯あたりの年間支出、二人以上の世帯）",
        "items": items,
        "source": "総務省統計局「家計調査（家計収支編）」",
        "source_url": "https://www.e-stat.go.jp/stat-search/files?toukei=00200561&tstat=000000330001",
    }


# ---- 国民生活に関する世論調査 ------------------------------------------------------

def _cao_table(name: str, cols: dict[str, int]) -> dict[str, list]:
    """時系列表（CP932）から、男女計・全年齢の行だけを取り出す。年は調査月を小数にした値。"""
    rows = list(csv.reader(io.StringIO((CAO / name).read_bytes().decode("cp932"))))
    out: dict[str, list] = {k: [] for k in cols}
    for r in rows:
        if len(r) > 7 and r[4].startswith("00") and r[5].startswith("00") and re.match(r"\d{4}_\d{2}", r[3]):
            y, m = map(int, r[3].split("_"))
            for k, i in cols.items():
                v = _num(r[i])
                if v is not None:
                    out[k].append([round(y + (m - 1) / 12, 2), v])
    return out


_REIWA = re.compile(r"令和(\d+|元|[０-９]+)年\s*(\d+|[０-９]+)月")


def _cao_report(name: str, cols: dict[str, int]) -> dict[str, list]:
    """報告書の時系列 CSV（UTF-8）。時系列表より新しい年が入っている。"""
    rows = list(csv.reader(io.StringIO((CAO / name).read_text(encoding="utf-8-sig"))))
    out: dict[str, list] = {k: [] for k in cols}
    for r in rows:
        m = _REIWA.match(r[0].translate(str.maketrans("０１２３４５６７８９", "0123456789"))) if r else None
        if not m:
            continue
        y = 2018 + (1 if m.group(1) == "元" else int(m.group(1)))
        x = round(y + (int(m.group(2)) - 1) / 12, 2)
        for k, i in cols.items():
            v = _num(r[i])
            if v is not None:
                out[k].append([x, v])
    return out


def _merge(*tables: dict[str, list], by_year: bool = False) -> dict[str, list]:
    """後ろの表を優先してつなぐ。by_year のときは同じ年の調査を1つにまとめる
    （時系列表と報告書で同じ回の調査月の書き方が違うことがあるため）。"""
    out: dict[str, dict] = {}
    for t in tables:
        for k, pts in t.items():
            for x, v in pts:
                out.setdefault(k, {})[int(x) if by_year else x] = (x, v)
    return {k: sorted(d.values()) for k, d in out.items()}


def opinion() -> list[dict]:
    satisfaction = _merge(
        _cao_table("00202.csv", {"満足": 7, "不満": 10}),
        _cao_table("00201.csv", {"満足": 7, "不満": 10}),
    )
    satisfaction_new = _merge(
        _cao_table("00200.csv", {"満足": 7, "不満": 10}),
        _cao_report("report_satisfaction.csv", {"満足": 2, "不満": 5}),
        by_year=True,
    )
    richness = _cao_table("02001.csv", {"心の豊かさ": 7, "物の豊かさ": 8})
    richness_new = _merge(
        _cao_table("02000.csv", {"心の豊かさ": 7, "物の豊かさ": 10}),
        _cao_report("report_richness.csv", {"心の豊かさ": 2, "物の豊かさ": 5}),
        by_year=True,
    )
    outlook = _cao_table("01801.csv", {"良くなっていく": 7, "悪くなっていく": 9})
    outlook_new = _merge(
        _cao_table("01800.csv", {"良くなっていく": 7, "悪くなっていく": 9}),
        _cao_report("report_outlook.csv", {"良くなっていく": 2, "悪くなっていく": 4}),
        by_year=True,
    )
    charts = [
        {"title": "いまの生活に満足しているか", "old": satisfaction, "new": satisfaction_new},
        {"title": "これからは心の豊かさか、物の豊かさか", "old": richness, "new": richness_new},
        {"title": "これからの生活は良くなるか", "old": outlook, "new": outlook_new},
    ]
    for c in charts:
        for part in ("old", "new"):
            for k, pts in c[part].items():
                if len(pts) < 3:
                    raise ValueError(f"世論調査「{c['title']}」の{k}のデータが少なすぎます")
    return charts


def main() -> None:
    out = {
        "kakei": kakei(),
        "opinion": {
            "charts": opinion(),
            "note": "2021年から調査方法（郵送）と選択肢が変わったため、2019年までと2021年以降は線を分けています。2020年は調査がありません。",
            "source": "内閣府「国民生活に関する世論調査」",
            "source_url": "https://survey.gov-online.go.jp/jikeiretsu/j-index.html#kokumin",
        },
    }
    (DATA / "longterm.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print("longterm.json:", len(out["kakei"]["items"]), "品目,", len(out["opinion"]["charts"]), "グラフ")


if __name__ == "__main__":
    main()
