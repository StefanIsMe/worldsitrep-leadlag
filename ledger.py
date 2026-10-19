"""Signal Ledger: automatic tests of "does signal X move before market Y?"

Runs on GitHub Actions (public repo = free minutes), stdlib only.

  python ledger.py sample   every 3h: snapshot ship counts per region
  python ledger.py score    daily: markets, signals, tests, site JSON
  python ledger.py all      both

Inputs (all public unless noted):
  - Polymarket gamma-api: every open event tagged geopolitics above
    MIN_VOLUME, plus its daily price history from clob prices-history.
  - worldsitrep-flight-data archive: military aircraft per snapshot per
    region (backfilled from 2026-08-30).
  - worldsitrep-cache global-vessel-data/latest.json: ships seen in each
    region, sampled every 3h (series starts when this ledger starts).
  - worldsitrep-cache <theatre>-events/latest.json: wire items dated
    yesterday per theatre (72h rolling feed, capped at 1500 records).

Each market is matched to regions by keyword (ledger/regions.json) and
tested against every signal in those regions. A test:
  - uses complete UTC days only;
  - removes the weekday pattern from both the signal and the next-day
    absolute price move (the trap that fooled the first lead-lag pilot);
  - measures Spearman rho between them and gets a p-value from circular
    shifts, which keeps each series' own autocorrelation;
  - corrects for testing many pairs at once (Benjamini-Hochberg);
  - records the date it was first registered, so later data is a clean
    out-of-sample check.

Verdicts: too_early (n < MIN_N), no_effect, holds_up (q < Q_MAX),
confirmed (holds_up and the out-of-sample days agree).

Writes ledger/index.json (all tests) and ledger/topics.json (one entry
per market question, for the site).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "ledger")
MARKET_DIR = os.path.join(OUT, "markets")
SIGNAL_DIR = os.path.join(OUT, "signals")
SAMPLE_DIR = os.path.join(OUT, "samples")
REGISTRY = os.path.join(OUT, "registry.json")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
FLIGHT_ARCHIVE = ("https://raw.githubusercontent.com/StefanIsMe/"
                  "worldsitrep-flight-data/main/flight-data/archive")
FLIGHT_LIST = ("https://api.github.com/repos/StefanIsMe/"
               "worldsitrep-flight-data/contents/flight-data/archive")
CACHE_RAW = ("https://raw.githubusercontent.com/StefanIsMe/"
             "worldsitrep-cache/main")
UA = {"User-Agent": "worldsitrep-ledger/1.0"}

TAG = os.environ.get("LEDGER_TAG", "geopolitics")
MIN_VOLUME = float(os.environ.get("LEDGER_MIN_VOLUME", "100000"))
MAX_EVENTS = int(os.environ.get("LEDGER_MAX_EVENTS", "80"))
FLIGHT_START = dt.date(2026, 8, 30)
MIN_N = 30
Q_MAX = 0.10
OOS_P = 0.10
SHIFT_GAP = 7
SHIP_FRESH_HOURS = 6


# ---------------------------------------------------------------- utils

def get(url: str, timeout: int = 60, tries: int = 3) -> bytes:
    headers = dict(UA)
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com"):
        headers["Authorization"] = f"Bearer {token}"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 ** (i + 1))
    raise RuntimeError("unreachable")


def get_json(url: str, **kw):
    return json.loads(get(url, **kw).decode("utf-8", "replace"))


def load(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path: str, obj, indent=None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=indent, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def in_box(lat, lng, box) -> bool:
    try:
        return box[0] <= float(lat) <= box[1] and box[2] <= float(lng) <= box[3]
    except (TypeError, ValueError):
        return False


def load_regions() -> dict:
    return load(os.path.join(OUT, "regions.json"), {})["regions"]


def match_regions(text: str, regions: dict) -> list[str]:
    low = text.lower()
    hits = []
    for rid, reg in regions.items():
        for kw in reg["keywords"]:
            if re.search(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", low):
                hits.append(rid)
                break
    return hits or ["global"]


# --------------------------------------------------------------- sample

def sample_ships(regions: dict, now: dt.datetime | None = None) -> dict:
    """Count ships seen in the last few hours in each region."""
    now = now or dt.datetime.now(dt.timezone.utc)
    feed = get_json(f"{CACHE_RAW}/global-vessel-data/latest.json", timeout=120)
    cutoff = now - dt.timedelta(hours=SHIP_FRESH_HOURS)
    counts = {rid: 0 for rid in regions}
    for v in feed.get("vessels", []):
        try:
            seen = dt.datetime.fromisoformat(
                str(v.get("observedAt")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if seen < cutoff:
            continue
        for rid, reg in regions.items():
            if in_box(v.get("lat"), v.get("lng"), reg["bbox"]):
                counts[rid] += 1
    row = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "feedAt": feed.get("collectedAt"), "ships": counts}
    path = os.path.join(SAMPLE_DIR, now.strftime("%Y-%m-%d") + ".jsonl")
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"ships sample: {sum(counts.values())} region hits", flush=True)
    return row


# -------------------------------------------------------------- signals

def flight_days() -> list[str]:
    days = []
    start = FLIGHT_START
    end = today_utc()
    months = sorted({(d.year, d.month) for d in
                     (start + dt.timedelta(n) for n in range((end - start).days + 1))})
    for y, m in months:
        try:
            files = get_json(f"{FLIGHT_LIST}/{y}/{m:02d}")
        except Exception as e:
            print(f"flight list {y}-{m:02d}: {str(e)[:80]}", flush=True)
            continue
        for f in files:
            name = f.get("name", "")
            if name.endswith(".jsonl"):
                days.append(f"{y}-{m:02d}-{name[:-6]}")
    return sorted(days)


def flight_counts(day: str, regions: dict) -> dict:
    y, m, d = day.split("-")
    raw = get(f"{FLIGHT_ARCHIVE}/{y}/{m}/{d}.jsonl").decode("utf-8", "replace")
    totals = {rid: 0 for rid in regions}
    snaps = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        snaps += 1
        for a in snap.get("aircraft", []):
            for rid, reg in regions.items():
                if in_box(a.get("lat"), a.get("lng"), reg["bbox"]):
                    totals[rid] += 1
    if not snaps:
        return {}
    return {"snapshots": snaps,
            "perSnapshot": {r: round(c / snaps, 2) for r, c in totals.items()}}


def ship_means(day: str) -> dict:
    path = os.path.join(SAMPLE_DIR, day + ".jsonl")
    rows = []
    if os.path.exists(path):
        for line in open(path):
            try:
                rows.append(json.loads(line)["ships"])
            except (json.JSONDecodeError, KeyError):
                continue
    # Fewer than four samples (12h) is not a full day.
    if len(rows) < 4:
        return {}
    keys = rows[0].keys()
    return {"samples": len(rows),
            "mean": {k: round(sum(r.get(k, 0) for r in rows) / len(rows), 1)
                     for k in keys}}


def wire_counts(day: str, regions: dict) -> dict:
    theatres = sorted({t for r in regions.values() for t in r["theatres"]})
    out = {}
    for t in theatres:
        try:
            feed = get_json(f"{CACHE_RAW}/{t}-events/latest.json", timeout=120)
        except Exception as e:
            print(f"wire {t}: {str(e)[:80]}", flush=True)
            continue
        evs = feed.get("events", [])
        n = sum(1 for e in evs if str(e.get("isoDate") or e.get("date") or "")[:10] == day)
        # A capped feed that is full of newer items has cut yesterday short.
        out[t] = {"count": n, "capped": len(evs) >= 1500}
    return out


def update_signals(regions: dict) -> None:
    today = today_utc()
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    for day in flight_days():
        if day >= today.isoformat():
            continue
        path = os.path.join(SIGNAL_DIR, day + ".json")
        row = load(path, {"date": day})
        changed = False
        if "flights" not in row or set(row["flights"].get("perSnapshot", {})) != set(regions):
            try:
                row["flights"] = flight_counts(day, regions)
                changed = True
            except Exception as e:
                print(f"flights {day}: {str(e)[:80]}", flush=True)
        if "ships" not in row:
            ships = ship_means(day)
            if ships:
                row["ships"] = ships
                changed = True
        if day == yesterday and "wire" not in row:
            row["wire"] = wire_counts(day, regions)
            changed = True
        if changed:
            save(path, row)
            print(f"signals {day}", flush=True)


def signal_series(regions: dict) -> dict[str, dict[str, float]]:
    """{signal_id: {date: value}} from the daily signal files."""
    series: dict[str, dict[str, float]] = {}
    if not os.path.isdir(SIGNAL_DIR):
        return series
    for f in sorted(os.listdir(SIGNAL_DIR)):
        if not f.endswith(".json"):
            continue
        row = load(os.path.join(SIGNAL_DIR, f), {})
        day = row.get("date")
        for rid, v in row.get("flights", {}).get("perSnapshot", {}).items():
            series.setdefault(f"flights:{rid}", {})[day] = v
        for rid, v in row.get("ships", {}).get("mean", {}).items():
            series.setdefault(f"ships:{rid}", {})[day] = v
        for t, w in row.get("wire", {}).items():
            if not w.get("capped"):
                series.setdefault(f"wire:{t}", {})[day] = w["count"]
    return series


# -------------------------------------------------------------- markets

def best_market(event: dict) -> dict | None:
    best = None
    for m in event.get("markets", []):
        if m.get("closed") or not m.get("active", True):
            continue
        try:
            tokens = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            tokens = []
        if not tokens:
            continue
        vol = float(m.get("volumeNum") or m.get("volume") or 0)
        if best is None or vol > best["volume"]:
            best = {"question": m.get("question", ""), "token": tokens[0],
                    "volume": vol, "endDate": m.get("endDate")}
    return best


def discover_events() -> list[dict]:
    events, offset = [], 0
    while offset < 1000:
        q = urllib.parse.urlencode({
            "tag_slug": TAG, "active": "true", "closed": "false",
            "limit": 100, "offset": offset,
            "order": "volume", "ascending": "false"})
        batch = get_json(f"{GAMMA}/events?{q}")
        if not batch:
            break
        events.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    keep = [e for e in events if float(e.get("volume") or 0) >= MIN_VOLUME]
    keep.sort(key=lambda e: -float(e.get("volume") or 0))
    return keep[:MAX_EVENTS]


def price_history(token: str) -> dict[str, float]:
    q = urllib.parse.urlencode({"market": token, "interval": "max",
                                "fidelity": 1440})
    hist = get_json(f"{CLOB}/prices-history?{q}").get("history", [])
    closes: dict[str, float] = {}
    for h in sorted(hist, key=lambda h: h["t"]):
        day = dt.datetime.fromtimestamp(h["t"], dt.timezone.utc).date().isoformat()
        closes[day] = round(float(h["p"]), 4)
    return closes


def update_markets(regions: dict) -> list[str]:
    """Refresh markets; returns slugs that are currently open."""
    try:
        events = discover_events()
    except Exception as e:
        print(f"gamma: {str(e)[:120]} (keeping cached markets)", flush=True)
        return [f[:-5] for f in sorted(os.listdir(MARKET_DIR))] if os.path.isdir(MARKET_DIR) else []
    today = today_utc().isoformat()
    live = []
    for e in events:
        m = best_market(e)
        if not m:
            continue
        slug = e["slug"]
        path = os.path.join(MARKET_DIR, slug + ".json")
        rec = load(path, {"slug": slug, "closes": {}})
        text = " ".join([e.get("title", ""), m["question"],
                         (e.get("description") or "")[:600]])
        rec.update({
            "title": e.get("title", ""), "question": m["question"],
            "url": f"https://polymarket.com/event/{slug}",
            "token": m["token"], "volume": round(m["volume"]),
            "eventVolume": round(float(e.get("volume") or 0)),
            "endDate": m["endDate"], "regions": match_regions(text, regions),
        })
        try:
            hist = price_history(m["token"])
            # Today's bar is still moving; keep complete days only.
            rec["closes"].update({d: p for d, p in hist.items() if d < today})
        except Exception as ex:
            print(f"history {slug}: {str(ex)[:80]}", flush=True)
        save(path, rec)
        live.append(slug)
        time.sleep(0.3)
    print(f"markets: {len(live)} live", flush=True)
    return live


# ---------------------------------------------------------------- stats

def ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    ma, mb = sum(a) / n, sum(b) / n
    sa = sum((x - ma) ** 2 for x in a) ** 0.5
    sb = sum((y - mb) ** 2 for y in b) ** 0.5
    if sa == 0 or sb == 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def spearman(a: list[float], b: list[float]) -> float:
    return pearson(ranks(a), ranks(b))


def deweekday(days: list[str], xs: list[float]) -> list[float]:
    groups: dict[int, list[float]] = {}
    wd = [dt.date.fromisoformat(d).weekday() for d in days]
    for w, x in zip(wd, xs):
        groups.setdefault(w, []).append(x)
    means = {w: sum(v) / len(v) for w, v in groups.items()}
    return [x - means[w] for w, x in zip(wd, xs)]


def shift_p(a: list[float], b: list[float], rho: float) -> float:
    """Share of circular shifts of a with |rho| at least as large."""
    n = len(a)
    shifts = range(SHIFT_GAP, n - SHIFT_GAP + 1)
    if not shifts:
        return 1.0
    ra, rb = ranks(a), ranks(b)
    hits = sum(1 for k in shifts
               if abs(pearson(ra[k:] + ra[:k], rb)) >= abs(rho) - 1e-12)
    return (hits + 1) / (len(shifts) + 1)


def bh(ps: list[float]) -> list[float]:
    m = len(ps)
    order = sorted(range(m), key=lambda i: ps[i])
    q = [1.0] * m
    prev = 1.0
    for rank_i in range(m, 0, -1):
        i = order[rank_i - 1]
        prev = min(prev, ps[i] * m / rank_i)
        q[i] = prev
    return q


def run_test(signal: dict[str, float], closes: dict[str, float],
             registered: str) -> dict:
    days = sorted(closes)
    nxt = {days[i]: abs(closes[days[i + 1]] - closes[days[i]])
           for i in range(len(days) - 1)
           if (dt.date.fromisoformat(days[i + 1]) -
               dt.date.fromisoformat(days[i])).days == 1}
    common = sorted(d for d in signal if d in nxt)
    res = {"n": len(common)}
    if len(common) < 10:
        return res
    s = [float(signal[d]) for d in common]
    y = [nxt[d] for d in common]
    sr, yr = deweekday(common, s), deweekday(common, y)
    rho = spearman(sr, yr)
    res.update({"rho": round(rho, 3), "p": round(shift_p(sr, yr, rho), 4),
                "from": common[0], "to": common[-1]})
    # Does the market move first? The move closing on day t+1 against
    # the signal on day t+2.
    rev = []
    for d in common:
        d2 = (dt.date.fromisoformat(d) + dt.timedelta(days=2)).isoformat()
        if d2 in signal:
            rev.append((nxt[d], float(signal[d2])))
    if len(rev) >= 10:
        res["reverseRho"] = round(spearman([a for a, _ in rev], [b for _, b in rev]), 3)
    # Plain-language spike view: top-quartile (weekday-adjusted) days.
    cut = sorted(sr)[int(len(sr) * 0.75)]
    hi = [y[i] for i in range(len(sr)) if sr[i] >= cut]
    lo = [y[i] for i in range(len(sr)) if sr[i] < cut]
    if hi and lo:
        res["moveAfterHigh"] = round(sum(hi) / len(hi), 4)
        res["moveOtherwise"] = round(sum(lo) / len(lo), 4)
    oos = [i for i, d in enumerate(common) if d > registered]
    res["oosN"] = len(oos)
    if len(oos) >= MIN_N:
        a, b = [sr[i] for i in oos], [yr[i] for i in oos]
        orho = spearman(a, b)
        res["oosRho"] = round(orho, 3)
        res["oosP"] = round(shift_p(a, b, orho), 4)
    return res


# ---------------------------------------------------------------- score

def verdict(t: dict) -> str:
    if t.get("n", 0) < MIN_N or "p" not in t:
        return "too_early"
    if t.get("q", 1) >= Q_MAX:
        return "no_effect"
    if (t.get("oosN", 0) >= MIN_N and t.get("oosP", 1) < OOS_P
            and (t.get("oosRho", 0) > 0) == (t["rho"] > 0)):
        return "confirmed"
    return "holds_up"


def score(regions: dict, live: list[str]) -> dict:
    series = signal_series(regions)
    registry = load(REGISTRY, {})
    today = today_utc().isoformat()
    tests, topics = [], []
    for slug in live:
        m = load(os.path.join(MARKET_DIR, slug + ".json"), None)
        if not m or not m.get("closes"):
            continue
        sigs = [sid for sid in series
                if sid.split(":")[1] in m["regions"]
                or (sid.startswith("wire:") and any(
                    sid[5:] in regions[r]["theatres"] for r in m["regions"]))]
        for sid in sorted(sigs):
            tid = f"{sid}->{slug}"
            registry.setdefault(tid, today)
            t = {"id": tid, "signal": sid, "market": slug,
                 "registered": registry[tid]}
            t.update(run_test(series[sid], m["closes"], registry[tid]))
            tests.append(t)
        days = sorted(m["closes"])
        last = m["closes"][days[-1]] if days else None
        week = m["closes"].get((dt.date.fromisoformat(days[-1]) - dt.timedelta(7)).isoformat()) if days else None
        topics.append({
            "slug": slug, "title": m["title"], "question": m["question"],
            "url": m["url"], "regions": m["regions"],
            "volume": m.get("eventVolume", m.get("volume")),
            "endDate": m.get("endDate"), "probability": last,
            "change7d": round(last - week, 4) if last is not None and week is not None else None,
            "history": [[d, m["closes"][d]] for d in days[-120:]],
            "tests": [t["id"] for t in tests if t["market"] == slug],
        })
    scored = [t for t in tests if t.get("n", 0) >= MIN_N and "p" in t]
    for t, q in zip(scored, bh([t["p"] for t in scored])):
        t["q"] = round(q, 4)
    for t in tests:
        t["verdict"] = verdict(t)
    order = {"confirmed": 0, "holds_up": 1, "no_effect": 2, "too_early": 3}
    tests.sort(key=lambda t: (order[t["verdict"]], t.get("q", 1), t["id"]))
    counts = {k: sum(1 for t in tests if t["verdict"] == k) for k in order}
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    index = {
        "schemaVersion": 1, "updated": now, "counts": counts,
        "method": {
            "minDays": MIN_N, "qMax": Q_MAX, "oosP": OOS_P,
            "summary": ("Signal on day t vs absolute market move from t to "
                        "t+1, both weekday-adjusted; Spearman rho; p from "
                        "circular shifts; Benjamini-Hochberg q across all "
                        "tests; out-of-sample check after registration.")},
        "regions": {r: v["name"] for r, v in regions.items()},
        "tests": tests,
    }
    topics.sort(key=lambda t: -(t["volume"] or 0))
    save(REGISTRY, registry, indent=1)
    save(os.path.join(OUT, "index.json"), index, indent=1)
    save(os.path.join(OUT, "topics.json"),
         {"schemaVersion": 1, "updated": now, "count": len(topics),
          "topics": topics})
    print(f"ledger: {len(topics)} topics, {len(tests)} tests, {counts}", flush=True)
    return index


def main(argv: list[str]) -> None:
    mode = argv[1] if len(argv) > 1 else "all"
    regions = load_regions()
    if mode in ("sample", "all"):
        try:
            sample_ships(regions)
        except Exception as e:
            print(f"ships sample failed: {str(e)[:120]}", flush=True)
    if mode in ("score", "all"):
        update_signals(regions)
        live = update_markets(regions)
        score(regions, live)


if __name__ == "__main__":
    main(sys.argv)
