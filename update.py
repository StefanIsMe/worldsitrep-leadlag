"""Daily lead-lag updater (runs on GitHub Actions, free tier).

Reads (all public, no keys):
  - flight archives: StefanIsMe/worldsitrep-flight-data
      flight-data/archive/2026/MM/DD.jsonl (raw GitHub, public)
  - Polymarket: gamma-api (rung discovery) + data-api (trade tape)

Writes (committed back to THIS repo):
  - signals/2026-09-11.json   daily feature row (append-only, one file/day)
  - market.csv                 date,yes_close (rebuilt from tape daily)
  - result.json                spike-vs-baseline table (the model output)
  - chart.png                  LinkedIn-ready chart (matplotlib)

Vessels (private repo) are folded in later via a PAT secret: when the
LEADLAG_PAT env var is present the script also pulls the vessel archive
through the authenticated GitHub API and adds bs_sea/odesa/ais_dark.
Without it, vessel fields are null and everything else still works.

Boxes: EU (36-60, -12-45), BlackSea air (40-48, 27-43).
Market rung: highest-volume "December 31, 202" market in the
  russia-x-ukraine-ceasefire-agreement-by event (currently Dec-31-2026).
Spike = eu_mil at/above 75th percentile of observed days.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import statistics
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
SIG_DIR = os.path.join(BASE, "signals")
MKT_CSV = os.path.join(BASE, "market.csv")
RESULT = os.path.join(BASE, "result.json")
CHART = os.path.join(BASE, "chart.png")

FLIGHT_RAW = ("https://raw.githubusercontent.com/StefanIsMe/"
              "worldsitrep-flight-data/main/flight-data/archive")
API = "https://api.github.com"

EU_BOX = (36.0, 60.0, -12.0, 45.0)
BS_AIR = (40.0, 48.0, 27.0, 43.0)
BS_SEA = (41.0, 47.5, 27.5, 42.0)
ODESA = (46.2, 46.7, 30.5, 31.2)

TANKER = {"K35R", "K46", "KC10", "KC135", "A332", "A333", "B762",
          "KC2", "VOYAGER", "MRTT"}
ISR = ("RC135", "RIVET", "E3", "E8", "P8", "RQ4", "MQ9", "U2",
       "EP3", "E7", "GLEX", "CL60")

GAMMA = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
EVENT_SLUG = "russia-x-ukraine-ceasefire-agreement-by"
UA = {"User-Agent": "worldsitrep-leadlag/1.0"}


def get(url: str, timeout=60) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def in_box(lat, lng, box) -> bool:
    try:
        return box[0] <= float(lat) <= box[1] and box[2] <= float(lng) <= box[3]
    except (TypeError, ValueError):
        return False


def list_dir(repo: str, path: str, token: str | None) -> list[str]:
    url = f"{API}/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers=dict(UA))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    return sorted(x["name"] for x in data
                  if x.get("type") == "file" and x["name"].endswith(".jsonl"))


def flight_day(url: str) -> dict:
    eu = ti = bs = 0
    for line in get(url).decode("utf-8", "replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        acs = snap.get("aircraft", snap if isinstance(snap, list) else [])
        for a in acs:
            lat, lng = a.get("lat"), a.get("lng")
            if lat is None or lng is None:
                continue
            if in_box(lat, lng, EU_BOX):
                eu += 1
                t = str(a.get("type") or "").upper()
                cs = str(a.get("callsign") or "").upper()
                if t in TANKER or any(h in t or h in cs for h in ISR):
                    ti += 1
            if in_box(lat, lng, BS_AIR):
                bs += 1
    return {"eu_mil": eu, "eu_tanker_isr": ti, "bs_air": bs}


def vessel_day(repo: str, path: str, token: str) -> dict:
    url = f"{API}/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers=dict(UA))
    req.add_header("Authorization", f"Bearer {token}")
    meta = json.loads(urllib.request.urlopen(req, timeout=60).read())
    blob = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{API}/repos/{repo}/git/blobs/{meta['sha']}",
        headers={**UA, "Authorization": f"Bearer {token}"}),
        timeout=180).read())["content"]
    import base64
    text = base64.b64decode("".join(blob.split())).decode("utf-8", "replace")
    bs = od = 0
    mmsi: set[str] = set()
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            snap = json.loads(line)
        except json.JSONDecodeError:
            continue
        for v in snap.get("vessels", []):
            lat, lng = v.get("lat"), v.get("lng")
            if lat is None or lng is None:
                continue
            if in_box(lat, lng, BS_SEA):
                bs += 1
                m = str(v.get("mmsi") or v.get("id") or "")
                if m:
                    mmsi.add(m)
            if in_box(lat, lng, ODESA):
                od += 1
    return {"bs_sea": bs, "odesa": od, "mmsi": sorted(mmsi)}


def update_signals(token: str | None) -> list[str]:
    os.makedirs(SIG_DIR, exist_ok=True)
    days: list[str] = []
    for mon in ("08", "09", "10", "11", "12"):
        base = f"flight-data/archive/2026/{mon}"
        try:
            files = json.loads(get(
                f"{API}/repos/StefanIsMe/worldsitrep-flight-data"
                f"/contents/{base}").decode())
            files = sorted(x["name"] for x in files
                           if x["name"].endswith(".jsonl"))
        except Exception as e:
            print(f"month {mon}: {str(e)[:100]}", flush=True)
            continue
        for f in files:
            day = f"2026-{mon}-{f.replace('.jsonl', '')}"
            out = os.path.join(SIG_DIR, f"{day}.json")
            if os.path.exists(out):
                days.append(day)
                continue
            try:
                row = {"date": day}
                row.update(flight_day(f"{FLIGHT_RAW}/2026/{mon}/{f}"))
                if token:
                    try:
                        v = vessel_day("StefanIsMe/worldsitrep-vessel-data",
                                       f"vessel-data/archive/2026/{mon}/{f}",
                                       token)
                        row["bs_sea"] = v["bs_sea"]
                        row["odesa"] = v["odesa"]
                        prev = os.path.join(
                            SIG_DIR,
                            f"{day_minus(day, 1)}.json")
                        if os.path.exists(prev):
                            pm = set(json.load(open(prev)).get("mmsi", []))
                            row["ais_dark"] = len(pm - set(v["mmsi"]))
                        row["mmsi"] = v["mmsi"][:5000]
                    except Exception as e:
                        print(f"vessels {day}: {str(e)[:120]}", flush=True)
                json.dump(row, open(out, "w"))
                print(f"signals {day}: eu={row['eu_mil']}", flush=True)
            except Exception as e:
                print(f"flights {day}: {str(e)[:120]}", flush=True)
                continue
            days.append(day)
    return sorted(days)


def day_minus(day: str, n: int) -> str:
    d = datetime.datetime.strptime(day, "%Y-%m-%d")
    return (d - datetime.timedelta(days=n)).strftime("%Y-%m-%d")


def update_market() -> None:
    e = json.loads(get(f"{GAMMA}/events/slug/{EVENT_SLUG}").decode())
    cond = q = ""
    best = 0.0
    for m in e.get("markets", []):
        if "December 31, 202" in m.get("question", ""):
            v = float(m.get("volume") or 0)
            if v > best:
                best, cond, q = v, m["conditionId"], m["question"]
    print(f"rung: {q} vol={best:,.0f}", flush=True)
    start = int(datetime.datetime(2026, 8, 30,
                                  tzinfo=datetime.timezone.utc).timestamp())
    trades, offset = [], 0
    while True:
        batch = json.loads(get(
            f"{DATA_API}/trades?market={cond}&limit=500&offset={offset}"
        ).decode())
        if not batch:
            break
        trades.extend(batch)
        if len(batch) < 500 or min(t["timestamp"] for t in batch) < start:
            break
        offset += 500
        if offset > 20000:
            break
    by_day: dict[str, list] = {}
    for t in trades:
        if t["timestamp"] < start:
            continue
        dt = datetime.datetime.fromtimestamp(
            t["timestamp"], datetime.timezone.utc).strftime("%Y-%m-%d")
        px = float(t["price"])
        oc = str(t.get("outcome", "")).lower()
        yes = px if oc == "yes" else (1 - px if oc == "no" else px)
        by_day.setdefault(dt, []).append((t["timestamp"], yes))
    with open(MKT_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "yes_close"])
        for dt in sorted(by_day):
            w.writerow([dt, round(sorted(by_day[dt])[-1][1], 4)])
    print(f"market: {len(by_day)} days, {len(trades)} trades", flush=True)


def analyze() -> dict:
    rows = []
    for f in sorted(os.listdir(SIG_DIR)):
        if f.endswith(".json"):
            r = json.load(open(os.path.join(SIG_DIR, f)))
            r.pop("mmsi", None)
            rows.append(r)
    mkt = {r["date"]: float(r["yes_close"])
           for r in csv.DictReader(open(MKT_CSV))}
    closes = sorted(mkt.items())
    moves = {closes[i][0]: abs(closes[i + 1][1] - closes[i][1])
             for i in range(len(closes) - 1)}
    eu = [r["eu_mil"] for r in rows
          if isinstance(r.get("eu_mil"), int)]
    thresh = sorted(eu)[int(len(eu) * 0.75)] if eu else 0
    spikes, sm, bm = [], [], []
    for r in rows:
        d = r["date"]
        if d not in moves:
            continue
        s = isinstance(r.get("eu_mil"), int) and r["eu_mil"] >= thresh
        (sm if s else bm).append(moves[d])
        if s:
            spikes.append(d)
    res = {
        "updated": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_days": len(rows),
        "n comparable": len(sm) + len(bm),
        "spike_threshold": thresh,
        "spike_days": spikes,
        "mean_move_after_spike": round(statistics.mean(sm), 4) if sm else None,
        "mean_move_baseline": round(statistics.mean(bm), 4) if bm else None,
        "n_spike": len(sm),
        "n_base": len(bm),
        "latest_yes": closes[-1][1] if closes else None,
        "note": ("pilot: suggestive, not significant"
                 if len(sm) < 10 else "n>=10 spikes: read the means"),
    }
    json.dump(res, open(RESULT, "w"), indent=1)
    print(json.dumps(res, indent=1), flush=True)
    # chart
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    ds = [datetime.datetime.strptime(d, "%Y-%m-%d") for d, _ in closes]
    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax1.plot(ds, [p for _, p in closes], "o-", color="#38bdf8",
             label="Yes (Dec-26 rung)")
    ax1.set_ylabel("Yes price", color="#38bdf8")
    ax2 = ax1.twinx()
    sds = [datetime.datetime.strptime(r["date"], "%Y-%m-%d") for r in rows]
    ev = [r["eu_mil"] if isinstance(r.get("eu_mil"), int) else 0
          for r in rows]
    cols = ["#ef4444" if r["date"] in spikes else "#f59e0b" for r in rows]
    ax2.bar(sds, ev, alpha=0.6, color=cols)
    ax2.set_ylabel("EU mil-aircraft", color="#f59e0b")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.autofmt_xdate()
    fig.suptitle("Do ADS-B signals lead Polymarket? (daily, auto-updated)")
    fig.tight_layout()
    fig.savefig(CHART, dpi=130)
    print(f"chart: {CHART}", flush=True)
    return res


def main():
    token = os.environ.get("LEADLAG_PAT") or None
    update_signals(token)
    update_market()
    analyze()


if __name__ == "__main__":
    main()
