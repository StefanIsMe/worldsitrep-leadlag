# worldsaccident-leadlag

**Do ADS-B / AIS ground-truth signals move before Polymarket Ukraine prices?**

Free daily automation (GitHub Actions, no paid services). All inputs are
public except vessel archives, which need a `LEADLAG_PAT` secret
(classic PAT, `repo` read scope on `worldsitrep-vessel-data`).

## What updates daily (07:00 ICT)

| Output | Content |
|---|---|
| `signals/YYYY-MM-DD.json` | `eu_mil`, `eu_tanker_isr`, `bs_air` (flights, public) + `bs_sea`, `odesa`, `ais_dark` (vessels, PAT only) |
| `market.csv` | Daily Yes closes, "Ukraine ceasefire agreement by Dec 31, 2026" ($2.6M rung), from the public trade tape |
| `result.json` | Spike (top-quartile flight days) vs baseline next-day \|move\| — the model output |
| `chart.png` | LinkedIn-ready chart, red bars = spike days |

## Pilot (2026-09-11, n=9 comparable days)

Spike days Sep 4 + Sep 7. Mean next-day |move|: **0.035 after spikes vs
0.013 baseline**. Suggestive, not significant — verdict at n=60
(early November). The Sep 4 coincidence (188 aircraft + 0.19 -> 0.33
price jump) is the thing to watch.

## Without the PAT

Everything except vessel fields works. Add secret `LEADLAG_PAT` in repo
Settings > Secrets to unlock `bs_sea` / `odesa` / `ais_dark`.

## Run locally

```sh
python update.py                  # flights + market (no PAT needed)
LEADLAG_PAT=ghp_xxx python update.py   # + vessels
```
