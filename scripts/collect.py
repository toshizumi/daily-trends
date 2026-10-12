"""毎日のトレンドを集めて docs/data に保存し、画面用の latest.json を作る。

外部ライブラリは使わない（GitHub Actions で pip install なしに動かすため）。

使い方:
    python scripts/collect.py                  # 今日の分を集めて latest.json を作り直す
    python scripts/collect.py --backfill-wiki 90  # Wikipedia の日別ランキングを過去90日分さかのぼって取る
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
DAILY = DATA / "daily"
WIKI = DATA / "wiki"
JST = dt.timezone(dt.timedelta(hours=9))
UA = "daily-trends/1.0 (+https://github.com/toshizumi/daily-trends)"
WINDOWS = (7, 30, 90)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return res.read()


def safe_url(url: str) -> str:
    """外部から来た URL は http と https だけ通す（javascript: などを画面のリンクに入れないため）。"""
    url = (url or "").strip()
    return url if urllib.parse.urlparse(url).scheme in ("http", "https") else ""


def require(items: list, name: str, minimum: int = 3) -> list:
    """取得元のページ構造が変わると0件のまま正常終了してしまうので、少なすぎたら失敗として扱う。"""
    if len(items) < minimum:
        raise ValueError(f"{name} の件数が {len(items)} 件しかありません（ページの形が変わった可能性があります）")
    return items


def norm(term: str) -> str:
    """表記ゆれをそろえた比較用のキー。"#ドジャース" と "ドジャース" を同じ語として数える。"""
    t = unicodedata.normalize("NFKC", term).strip().lstrip("#").lower()
    return re.sub(r"\s+", " ", t)


# ---- Google トレンド（急上昇ワード） -------------------------------------------

def google_trends() -> list[dict]:
    ns = {"ht": "https://trends.google.com/trending/rss"}
    root = ET.fromstring(fetch("https://trends.google.com/trending/rss?geo=JP"))
    items = []
    for it in root.iter("item"):
        news = it.find("ht:news_item", ns)
        items.append({
            "term": (it.findtext("title") or "").strip(),
            "traffic": (it.findtext("ht:approx_traffic", namespaces=ns) or "").strip(),
            "news_title": html.unescape(news.findtext("ht:news_item_title", namespaces=ns) or "") if news is not None else "",
            "news_url": safe_url(news.findtext("ht:news_item_url", namespaces=ns)) if news is not None else "",
            "news_source": (news.findtext("ht:news_item_source", namespaces=ns) or "") if news is not None else "",
        })
    # 検索数の多い順に並べる（"2000+" や "1万+" の形で来る）
    def volume(s: str) -> int:
        s = s.replace(",", "").replace("+", "")
        m = re.match(r"(\d+)(万)?", s)
        if not m:
            return 0
        return int(m.group(1)) * (10000 if m.group(2) else 1)
    items.sort(key=lambda x: volume(x["traffic"]), reverse=True)
    return require(items, "Google トレンド")


# ---- X のトレンド（trends24.in の1時間ごとの一覧を24時間分まとめる） ----------------

class _Trends24Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.lists: list[list[str]] = []
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "ol" and "trend-card__list" in (a.get("class") or ""):
            self.lists.append([])
        elif tag == "a" and a.get("class") == "trend-link" and self.lists:
            self._in_link = True
            self.lists[-1].append("")

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False

    def handle_data(self, data):
        if self._in_link:
            self.lists[-1][-1] += data


def x_trends() -> list[dict]:
    p = _Trends24Parser()
    p.feed(fetch("https://trends24.in/japan/").decode("utf-8", "replace"))
    hourly = p.lists[:24]
    agg: dict[str, dict] = {}
    for lst in hourly:
        for rank, name in enumerate(lst, 1):
            name = name.strip()
            if not name:
                continue
            k = norm(name)
            e = agg.setdefault(k, {"term": name, "hours": 0, "best_rank": rank})
            e["hours"] += 1
            e["best_rank"] = min(e["best_rank"], rank)
    items = sorted(agg.values(), key=lambda e: (-e["hours"], e["best_rank"]))
    for e in items:
        e["url"] = "https://x.com/search?q=" + urllib.parse.quote(e["term"])
    return require(items, "X のトレンド", 10)[:40]


# ---- はてなブックマーク 人気エントリー ------------------------------------------------

def hatena() -> list[dict]:
    ns = {"rss": "http://purl.org/rss/1.0/", "hatena": "http://www.hatena.ne.jp/info/xmlns#"}
    root = ET.fromstring(fetch("https://b.hatena.ne.jp/hotentry/all.rss"))
    items = []
    for it in root.findall("rss:item", ns):
        items.append({
            "title": (it.findtext("rss:title", namespaces=ns) or "").strip(),
            "url": safe_url(it.findtext("rss:link", namespaces=ns)),
            "users": int(it.findtext("hatena:bookmarkcount", namespaces=ns) or 0),
        })
    return require(items, "はてなブックマーク")


# ---- Wikipedia 日本語版の閲覧数ランキング ------------------------------------------

_WIKI_SKIP = re.compile(r"^(メインページ|特別:|Wikipedia:|ファイル:|Help:|ノート:|利用者:|Portal:|Category:|-$)")


def wiki_top(day: dt.date) -> list[dict] | None:
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/ja.wikipedia/all-access/"
           f"{day:%Y/%m/%d}")
    try:
        raw = json.loads(fetch(url))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # まだ集計されていない日
            return None
        raise
    arts = raw["items"][0]["articles"]
    out = [{"article": a["article"], "views": a["views"]} for a in arts if not _WIKI_SKIP.match(a["article"])]
    return out[:100]


def save_wiki(day: dt.date) -> bool:
    path = WIKI / f"{day:%Y-%m-%d}.json"
    if path.exists():
        return True
    top = wiki_top(day)
    if top is None:
        return False
    path.write_text(json.dumps({"date": f"{day:%Y-%m-%d}", "articles": top}, ensure_ascii=False, indent=1))
    return True


# ---- 中期の集計 ----------------------------------------------------------------

def load_dir(d: Path) -> dict[dt.date, dict]:
    out = {}
    for f in sorted(d.glob("*.json")):
        out[dt.date.fromisoformat(f.stem)] = json.loads(f.read_text())
    return out


def recurring_terms(daily: dict[dt.date, dict], today: dt.date, days: int) -> list[dict]:
    """期間内に Google と X のトレンドに何日登場したか。長く話題になっている語ほど上に来る。"""
    start = today - dt.timedelta(days=days - 1)
    agg: dict[str, dict] = {}
    for day, snap in daily.items():
        if not (start <= day <= today):
            continue
        seen: dict[str, set[str]] = {}
        for src, key in (("google", "term"), ("x", "term")):
            for it in snap.get(src, []):
                seen.setdefault(norm(it[key]), set()).add(src)
                agg.setdefault(norm(it[key]), {"term": it[key].lstrip("#"), "days": set(), "sources": set(),
                                               "last": day})
        for k, srcs in seen.items():
            e = agg[k]
            e["days"].add(day)
            e["sources"] |= srcs
            e["last"] = max(e["last"], day)
    items = [e for e in agg.values() if len(e["days"]) >= 2]
    items.sort(key=lambda e: (-len(e["days"]), -e["last"].toordinal()))
    return [{
        "term": e["term"],
        "days": len(e["days"]),
        "sources": sorted(e["sources"]),
        "dates": sorted(d.isoformat() for d in e["days"]),
    } for e in items[:30]]


def wiki_window(wiki: dict[dt.date, dict], end: dt.date, days: int) -> dict:
    start = end - dt.timedelta(days=days - 1)
    total: dict[str, int] = {}
    covered = 0
    for day, snap in wiki.items():
        if start <= day <= end:
            covered += 1
            for a in snap["articles"]:
                total[a["article"]] = total.get(a["article"], 0) + a["views"]
    top = sorted(total.items(), key=lambda kv: -kv[1])[:30]
    return {"covered_days": covered, "articles": [{"article": a, "views": v} for a, v in top]}


def wiki_rising(wiki: dict[dt.date, dict], end: dt.date) -> list[dict]:
    """直近7日の1日平均が、その前の23日の1日平均の何倍になったか。

    ランキング外の日は0として数える。Wikimedia 側で欠けている日があるので、
    平均は実際にデータがある日数で割る。
    """
    recent_start = end - dt.timedelta(days=6)
    base_start = end - dt.timedelta(days=29)
    recent: dict[str, int] = {}
    base: dict[str, int] = {}
    n_recent = n_base = 0
    for day, snap in wiki.items():
        if recent_start <= day <= end:
            bucket, n_recent = recent, n_recent + 1
        elif base_start <= day < recent_start:
            bucket, n_base = base, n_base + 1
        else:
            continue
        for a in snap["articles"]:
            bucket[a["article"]] = bucket.get(a["article"], 0) + a["views"]
    if not n_recent or not n_base:
        return []
    fresh, grown = [], []
    for art, v in recent.items():
        r_avg = v / n_recent
        b_avg = base.get(art, 0) / n_base
        if r_avg < 30000:  # 閲覧数が少ない記事は倍率がぶれるので外す
            continue
        e = {"article": art, "recent_avg": round(r_avg), "base_avg": round(b_avg),
             "ratio": round(r_avg / b_avg, 1) if b_avg else None}
        if not b_avg:
            fresh.append(e)  # 前の期間には一度もランキングに入っていない
        elif r_avg / b_avg >= 2:
            grown.append(e)
    fresh.sort(key=lambda e: -e["recent_avg"])
    grown.sort(key=lambda e: -e["ratio"])
    return (fresh + grown)[:20]


# ---- 実行 ----------------------------------------------------------------------

def collect_today(now: dt.datetime) -> None:
    today = now.date()
    path = DAILY / f"{today:%Y-%m-%d}.json"
    # 同じ日に2回目を動かしたとき、失敗した取得元は1回目の結果を残す
    old = json.loads(path.read_text()) if path.exists() else {}
    snap = {"date": today.isoformat(), "collected_at": now.isoformat(timespec="minutes"), "errors": {}}
    for name, fn in (("google", google_trends), ("x", x_trends), ("hatena", hatena)):
        try:
            snap[name] = fn()
        except Exception as e:  # 1つの取得元が落ちても他は出す
            print(f"[warn] {name}: {e}", file=sys.stderr)
            snap["errors"][name] = f"{type(e).__name__}: {e}"
            snap[name] = old.get(name, [])
            if snap[name]:
                snap.setdefault("kept_from", {})[name] = old.get("collected_at")
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=1))

    # Wikipedia は前日分が翌日に出るので、直近数日の取りこぼしを埋める
    for back in range(1, 5):
        try:
            save_wiki(today - dt.timedelta(days=back))
        except Exception as e:
            print(f"[warn] wiki {back}: {e}", file=sys.stderr)


def build_latest(now: dt.datetime) -> None:
    daily = load_dir(DAILY)
    wiki = load_dir(WIKI)
    # 当日分がまだ無いときは最新の日を表示するので、中期の集計もその日を基準にそろえる
    today = now.date() if now.date() in daily else (max(daily) if daily else now.date())
    snap = daily.get(today, {})
    wiki_end = max(wiki) if wiki else today
    latest = {
        "generated_at": now.isoformat(timespec="minutes"),
        "today": snap,
        "history_days": len(daily),
        "history_since": min(daily).isoformat() if daily else None,
        "wiki_yesterday": wiki[wiki_end]["articles"][:20] if wiki else [],
        "wiki_date": wiki_end.isoformat() if wiki else None,
        "wiki_rising": wiki_rising(wiki, wiki_end),
        "windows": {
            str(n): {
                "recurring": recurring_terms(daily, today, n),
                "wiki": wiki_window(wiki, wiki_end, n),
            } for n in WINDOWS
        },
    }
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-wiki", type=int, default=0, metavar="DAYS")
    ap.add_argument("--no-collect", action="store_true", help="取得せず latest.json だけ作り直す")
    args = ap.parse_args()
    DAILY.mkdir(parents=True, exist_ok=True)
    WIKI.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(JST)

    if args.backfill_wiki:
        for back in range(1, args.backfill_wiki + 1):
            day = now.date() - dt.timedelta(days=back)
            ok = save_wiki(day)
            print(f"wiki {day}: {'ok' if ok else 'not yet'}")
    if not args.no_collect:
        collect_today(now)
    build_latest(now)


if __name__ == "__main__":
    main()
