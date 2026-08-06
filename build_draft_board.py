#!/usr/bin/env python3
"""
build_draft_board.py
--------------------
Pulls REAL NFL player + fantasy stats from the Sleeper API (free, no key,
read-only) and generates:

  1. nfl_fantasy_ppr.csv          - full dataset, every fantasy-relevant player
  2. fantasy-draft-board.html     - self-contained dashboard, opens in a browser

Roster slots produced: QB, RB1, RB2, WR1, WR2, WR3, TE, D/ST, K (+ bench depth)
Scoring: PPR (pts_ppr straight from Sleeper)

Usage:
    python build_draft_board.py
    python build_draft_board.py --teams 10 --seasons 2025 2024
    python build_draft_board.py --outdir ~/fantasy

Re-run any time during the season to refresh. Requires: requests
    pip install requests
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
STATS_URL_V1 = "https://api.sleeper.app/v1/stats/nfl/regular/{season}"
STATS_URL_V2 = "https://api.sleeper.com/stats/nfl/{season}?season_type=regular&order_by=pts_ppr"

FANTASY_POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"]
HTTP_TIMEOUT = 60

# A player with no ADP still belongs on the board if he holds a real role on his
# NFL depth chart. Depth chart order at or below these keeps him.
DEPTH_KEEP = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 1}


def on_board(r):
    """Has ADP, is a rookie, or holds a starter-tier role on his own team."""
    if r.get("adp_composite") is not None:
        return True
    if r["position"] == "DEF":              # all 32 defenses are draftable
        return True
    if r.get("is_rookie"):                  # depth charts aren't set in August
        return True
    limit = DEPTH_KEEP.get(r["position"])
    depth = r.get("depth_chart_order")
    return bool(limit and depth and depth <= limit)

try:
    import adp_sources
    ADP_AVAILABLE = True
except ImportError:
    ADP_AVAILABLE = False


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def get_json(url, label):
    print(f"  -> {label} ...", end="", flush=True)
    resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "draft-board/1.0"})
    resp.raise_for_status()
    data = resp.json()
    print(f" ok ({len(data)} records)")
    return data


def fetch_players():
    """All NFL players Sleeper knows about (~11k, includes backups and team defenses)."""
    return get_json(PLAYERS_URL, "player universe")


def fetch_season_stats(season):
    """
    Season-total stats. Sleeper exposes two shapes depending on endpoint:
      v1: { player_id: {stat: value, ...}, ... }
      v2: [ {player_id: ..., stats: {...}}, ... ]
    Try v1 first (compact), fall back to v2.
    """
    try:
        raw = get_json(STATS_URL_V1.format(season=season), f"{season} season stats")
        if isinstance(raw, dict) and raw:
            return {str(pid): (s or {}) for pid, s in raw.items()}
    except Exception as e:
        print(f" v1 failed ({e}); trying v2")

    raw = get_json(STATS_URL_V2.format(season=season), f"{season} season stats (v2)")
    out = {}
    if isinstance(raw, list):
        for rec in raw:
            pid = str(rec.get("player_id", ""))
            if pid:
                out[pid] = rec.get("stats") or {}
    elif isinstance(raw, dict):
        for pid, rec in raw.items():
            out[str(pid)] = (rec.get("stats") if isinstance(rec, dict) and "stats" in rec else rec) or {}
    return out


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------

def f(stats, key, default=0):
    """Safe numeric pull out of a Sleeper stats dict."""
    v = stats.get(key, default)
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return default


def _team_role(pos, depth):
    """
    A player's spot on his own NFL depth chart, e.g. WR2.
    Kickers default to K1 — teams carry exactly one, and Sleeper often leaves
    depth chart order blank for them.
    """
    if pos == "K":
        return f"K{depth or 1}"
    if pos in ("QB", "RB", "WR", "TE") and depth:
        return f"{pos}{depth}"
    return ""


def player_position(meta):
    pos = meta.get("position")
    if pos in FANTASY_POSITIONS:
        return pos
    for p in (meta.get("fantasy_positions") or []):
        if p in FANTASY_POSITIONS:
            return p
    return None


def build_rows(players, stats_by_season, seasons):
    primary, prior = seasons[0], (seasons[1] if len(seasons) > 1 else None)
    rows = []

    for pid, meta in players.items():
        if not isinstance(meta, dict):
            continue
        pos = player_position(meta)
        if not pos:
            continue

        s_now = stats_by_season.get(primary, {}).get(str(pid), {})
        s_prev = stats_by_season.get(prior, {}).get(str(pid), {}) if prior else {}

        # Drop only true nobodies: no production either season, no NFL team,
        # and not a rookie Sleeper is tracking.
        if (not s_now and not s_prev
                and not meta.get("team")
                and meta.get("years_exp") != 0):
            continue

        name = meta.get("full_name") or " ".join(
            x for x in [meta.get("first_name"), meta.get("last_name")] if x
        ).strip()
        if pos == "DEF":
            name = f"{meta.get('team') or pid} D/ST"
        if not name:
            continue

        gp_now = f(s_now, "gp")
        pts_now = f(s_now, "pts_ppr")
        gp_prev = f(s_prev, "gp")
        pts_prev = f(s_prev, "pts_ppr")

        rows.append({
            "player_id": str(pid),
            "name": name,
            "position": pos,
            "team": meta.get("team") or "FA",
            "age": meta.get("age"),
            "height": meta.get("height") or "",
            "weight": meta.get("weight") or "",
            "college": meta.get("college") or "",
            "jersey": meta.get("number"),
            "birth_date": meta.get("birth_date") or "",
            "years_exp": meta.get("years_exp"),
            "is_rookie": meta.get("years_exp") == 0,
            "status": meta.get("status") or "",
            "injury_status": meta.get("injury_status") or "",
            "injury_notes": (meta.get("injury_notes") or "")[:180],
            "depth_chart_order": meta.get("depth_chart_order"),
            "team_role": _team_role(pos, meta.get("depth_chart_order")),
            "sleeper_rank": meta.get("search_rank"),

            f"gp_{primary}": gp_now,
            f"ppr_{primary}": pts_now,
            f"ppg_{primary}": round(pts_now / gp_now, 2) if gp_now else 0.0,
            f"pass_yd_{primary}": f(s_now, "pass_yd"),
            f"pass_td_{primary}": f(s_now, "pass_td"),
            f"pass_int_{primary}": f(s_now, "pass_int"),
            f"rush_att_{primary}": f(s_now, "rush_att"),
            f"rush_yd_{primary}": f(s_now, "rush_yd"),
            f"rush_td_{primary}": f(s_now, "rush_td"),
            f"rec_{primary}": f(s_now, "rec"),
            f"rec_tgt_{primary}": f(s_now, "rec_tgt"),
            f"rec_yd_{primary}": f(s_now, "rec_yd"),
            f"rec_td_{primary}": f(s_now, "rec_td"),

            f"gp_{prior}": gp_prev,
            f"ppr_{prior}": pts_prev,
            f"ppg_{prior}": round(pts_prev / gp_prev, 2) if gp_prev else 0.0,
            f"pass_yd_{prior}": f(s_prev, "pass_yd"),
            f"pass_td_{prior}": f(s_prev, "pass_td"),
            f"pass_int_{prior}": f(s_prev, "pass_int"),
            f"rush_att_{prior}": f(s_prev, "rush_att"),
            f"rush_yd_{prior}": f(s_prev, "rush_yd"),
            f"rush_td_{prior}": f(s_prev, "rush_td"),
            f"rec_{prior}": f(s_prev, "rec"),
            f"rec_tgt_{prior}": f(s_prev, "rec_tgt"),
            f"rec_yd_{prior}": f(s_prev, "rec_yd"),
            f"rec_td_{prior}": f(s_prev, "rec_td"),
        })

    return rows


def assign_ranks_and_slots(rows, teams, primary):
    """Rank by production. Slots get assigned later, off ADP."""
    by_pos = {}
    for r in rows:
        by_pos.setdefault(r["position"], []).append(r)

    for pos, group in by_pos.items():
        group.sort(key=lambda r: (-r[f"ppr_{primary}"], -(r[f"ppg_{primary}"])))
        for i, r in enumerate(group):
            r["prod_rank"] = i + 1
            r["pos_rank"] = i + 1      # provisional; ADP overwrites it
            r["slot"] = f"{pos} bench"
        assign_tiers(group, primary)

    rows.sort(key=lambda r: (-r[f"ppr_{primary}"],))
    for i, r in enumerate(rows):
        r["overall_rank"] = i + 1
    return rows


SLOT_BANDS = {
    "QB":  [("QB", 1)],
    "RB":  [("RB1", 1), ("RB2", 1)],
    "WR":  [("WR1", 1), ("WR2", 1), ("WR3", 1)],
    "TE":  [("TE", 1)],
    "K":   [("K", 1)],
    "DEF": [("D/ST", 1)],
}


def assign_slots(board, teams, primary):
    """
    Positional rank and roster slot from consensus ADP — draft order, not last
    season's box score. A rookie taken third overall belongs in RB1, even with
    zero NFL production behind him.
    """
    by_pos = {}
    for r in board:
        by_pos.setdefault(r["position"], []).append(r)

    for pos, group in by_pos.items():
        group.sort(key=lambda r: (
            r.get("adp_composite") is None,
            r.get("adp_composite") if r.get("adp_composite") is not None else 0,
            -r.get(f"ppr_{primary}", 0),
        ))
        bands = SLOT_BANDS.get(pos, [])
        for i, r in enumerate(group):
            rank = i + 1
            r["pos_rank"] = rank
            slot, cursor = None, 0
            for label, mult in bands:
                size = teams * mult
                if cursor < rank <= cursor + size:
                    slot = label
                    break
                cursor += size
            r["slot"] = slot or f"{pos} depth"
    return board


def merge_adp(rows, composite, primary):
    """
    Attach multi-platform ADP to each player and compute the value gap:
    where the market drafts them vs. where last season's production ranks them.
    Positive gap = falling further than production justifies.
    """
    matched = 0
    for r in rows:
        key = adp_sources.make_key(r["name"], r["position"], r["team"])
        slot = composite.get(key)
        if not slot:
            # Retry without team constraint for traded players.
            alt = adp_sources.make_key(r["name"], r["position"], "")
            slot = composite.get(alt)
        if not slot:
            r["adp_composite"] = None
            r["adp_sources_n"] = 0
            r["adp_spread"] = None
            r["consensus_pick"] = None
            r["value_gap"] = None
            for sid, _ in adp_sources.SOURCES:
                r[f"adp_{sid}"] = None
            continue

        matched += 1
        r["adp_composite"] = slot["composite_adp"]
        r["adp_sources_n"] = slot["n_sources"]
        r["adp_spread"] = slot["spread"]
        r["consensus_pick"] = slot["consensus_pick"]
        r["trade_value"] = slot["extra"].get("trade_value")
        for sid, _ in adp_sources.SOURCES:
            r[f"adp_{sid}"] = slot["adp"].get(sid)

    # Value gap needs production rank restricted to players who actually have ADP.
    drafted = [r for r in rows if r.get("consensus_pick")]
    drafted.sort(key=lambda r: -r.get(f"ppr_{primary}", 0))
    for i, r in enumerate(drafted):
        r["value_gap"] = round(r["consensus_pick"] - (i + 1), 1)

    return matched


def assign_tiers(group, primary, max_tier_size=6):
    """Break a position group into tiers at natural scoring gaps."""
    scored = [r for r in group if r[f"ppg_{primary}"] > 0]
    if len(scored) < 3:
        for r in group:
            r["tier"] = 1 if r in scored else None
        return

    ppgs = [r[f"ppg_{primary}"] for r in scored]
    gaps = [ppgs[i] - ppgs[i + 1] for i in range(len(ppgs) - 1)]
    threshold = statistics.median(gaps) * 2.0 if gaps else 0

    tier, size = 1, 0
    for i, r in enumerate(scored):
        r["tier"] = tier
        size += 1
        if i < len(gaps) and (gaps[i] > threshold or size >= max_tier_size):
            tier += 1
            size = 0
    for r in group:
        r.setdefault("tier", None)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_csv(rows, path):
    import csv
    if not rows:
        return
    keys = list(rows[0].keys())
    ordered = ["overall_rank", "pos_rank", "slot", "tier"] + [k for k in keys if k not in
               ("overall_rank", "pos_rank", "slot", "tier")]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ordered, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"  CSV  -> {path}  ({len(rows)} players)")


def write_html(rows, path, seasons, teams, adp_info=None, adp_year=None, slot_depth=200,
               depth_charts=None):
    payload = {
        "generated": datetime.now().strftime("%b %d, %Y at %I:%M %p"),
        "primary": seasons[0],
        "prior": seasons[1] if len(seasons) > 1 else None,
        "teams": teams,
        "adp": adp_info,
        "adp_year": adp_year,
        "slot_depth": slot_depth,
        "depth_charts": depth_charts or {},
        "players": rows,
    }
    html = HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(payload, separators=(",", ":")))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  HTML -> {path}")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Draft Board — PPR</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo+Condensed:wght@600;700&family=Inter:wght@400;500;600&family=Roboto+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0C1620;
    --ink-2:#142433;
    --ink-3:#1D3245;
    --paper:#E9EEF2;
    --muted:#7E93A5;
    --line:#22394D;
    --chalk:#F2C14E;
    --pos-qb:#5B8FF0;
    --pos-rb:#48B39B;
    --pos-wr:#D97BA0;
    --pos-te:#E0904A;
    --pos-k:#9B87D4;
    --pos-def:#6E9E5E;
    --warn:#E8705F;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-text-size-adjust:100%}
  body{
    background:var(--ink);
    color:var(--paper);
    font:400 15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    padding:24px 20px 60px;
  }
  .wrap{max-width:1380px;margin:0 auto}

  header{
    display:flex;align-items:flex-end;justify-content:space-between;
    gap:24px;flex-wrap:wrap;
    border-bottom:2px solid var(--line);padding-bottom:18px;margin-bottom:22px;
  }
  h1{
    font:700 34px/1 "Archivo Condensed",Impact,sans-serif;
    letter-spacing:.02em;text-transform:uppercase;
  }
  h1 span{color:var(--chalk)}
  .meta{font:400 12px/1.6 "Roboto Mono",monospace;color:var(--muted);text-align:right}

  /* ---- signature: the slot rail ---- */
  .rail{display:flex;flex-direction:column;gap:8px;margin-bottom:22px}
  .rail-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
  .rail-label{
    font:600 10px/1 "Archivo Condensed",sans-serif;letter-spacing:.11em;
    text-transform:uppercase;color:var(--muted);width:62px;flex-shrink:0;
  }
  .slot-btn{
    background:var(--ink-2);border:1px solid var(--line);color:var(--paper);
    border-radius:3px;padding:9px 14px;cursor:pointer;
    font:600 12px/1 "Archivo Condensed",sans-serif;letter-spacing:.09em;
    text-transform:uppercase;display:flex;align-items:center;gap:9px;
    transition:background .15s,border-color .15s;
  }
  .slot-btn:hover{background:var(--ink-3)}
  .slot-btn .n{font:500 11px/1 "Roboto Mono",monospace;color:var(--muted)}
  .slot-btn[aria-pressed="true"]{
    background:var(--chalk);color:var(--ink);border-color:var(--chalk);
  }
  .slot-btn[aria-pressed="true"] .n{color:var(--ink);opacity:.65}
  .slot-btn:focus-visible{outline:2px solid var(--chalk);outline-offset:2px}

  .filters{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:18px}
  input[type=search],select{
    background:var(--ink-2);border:1px solid var(--line);color:var(--paper);
    border-radius:3px;padding:9px 12px;font:400 14px Inter,sans-serif;min-width:180px;
  }
  input[type=search]:focus,select:focus{outline:2px solid var(--chalk);outline-offset:1px}
  label.chk{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted);cursor:pointer}
  .count{margin-left:auto;font:500 12px "Roboto Mono",monospace;color:var(--muted)}

  .scroll{overflow-x:auto;border:1px solid var(--line);border-radius:4px}
  table{width:100%;border-collapse:collapse;white-space:nowrap}
  thead th{
    position:sticky;top:0;z-index:2;background:var(--ink-2);
    font:600 11px/1 "Archivo Condensed",sans-serif;letter-spacing:.08em;
    text-transform:uppercase;color:var(--muted);
    padding:12px 11px;text-align:left;cursor:pointer;user-select:none;
    border-bottom:1px solid var(--line);
  }
  thead th:hover{color:var(--paper)}
  thead th[data-dir]::after{content:" ▾";color:var(--chalk)}
  thead th[data-dir="asc"]::after{content:" ▴";color:var(--chalk)}
  tbody td{padding:10px 11px;border-bottom:1px solid rgba(34,57,77,.55);font-size:13.5px}
  tbody tr:hover{background:var(--ink-2)}
  .num{font:500 13px "Roboto Mono",monospace;text-align:right}
  .name{font-weight:600;white-space:nowrap}
  .sub{display:block;font:400 11px "Roboto Mono",monospace;color:var(--muted);margin-top:2px}

  .pos{
    display:inline-block;min-width:34px;text-align:center;
    font:700 10px/1 "Archivo Condensed",sans-serif;letter-spacing:.08em;
    padding:4px 6px;border-radius:2px;color:var(--ink);
  }
  .pos.QB{background:var(--pos-qb)} .pos.RB{background:var(--pos-rb)}
  .pos.WR{background:var(--pos-wr)} .pos.TE{background:var(--pos-te)}
  .pos.K{background:var(--pos-k)}   .pos.DEF{background:var(--pos-def)}

  /* tier bands: left edge weight encodes tier depth */
  td.tier{font:500 12px "Roboto Mono",monospace;color:var(--muted);
          border-left:3px solid var(--line)}
  td.tier[data-t="1"]{border-left-color:var(--chalk);color:var(--chalk)}
  td.tier[data-t="2"]{border-left-color:#B99440}
  td.tier[data-t="3"]{border-left-color:#7C6530}

  /* leftmost columns stay put while the rest scrolls */
  .c-take{width:40px;min-width:40px;left:0;padding:6px 4px!important;text-align:center}
  .c-rank{width:48px;min-width:48px;left:40px}
  .c-tier{width:54px;min-width:54px;left:88px}
  .c-name{width:186px;min-width:186px;left:142px;white-space:normal}
  th.c-take,th.c-rank,th.c-tier,th.c-name{position:sticky;z-index:4;background:var(--ink-2)}
  td.c-take,td.c-rank,td.c-tier,td.c-name{position:sticky;z-index:1;background:var(--ink)}
  tbody tr:hover td.c-take,tbody tr:hover td.c-rank,
  tbody tr:hover td.c-tier,tbody tr:hover td.c-name{background:var(--ink-2)}
  .c-name::after{
    content:"";position:absolute;top:0;right:0;bottom:0;width:1px;background:var(--line);
  }

  button.take{
    width:24px;height:24px;padding:0;border-radius:3px;
    background:transparent;border:1px solid var(--line);color:var(--muted);
    font:400 14px/1 Inter,sans-serif;cursor:pointer;
    display:inline-flex;align-items:center;justify-content:center;
  }
  button.take:hover{border-color:var(--warn);color:var(--warn);background:rgba(232,112,95,.12)}
  button.take.back{border-color:var(--pos-rb);color:var(--pos-rb)}
  button.take.back:hover{background:rgba(72,179,155,.14)}
  button.take:focus-visible{outline:2px solid var(--chalk);outline-offset:1px}

  tr.gone td:not(.c-take){opacity:.34}
  tr.gone td.c-name{text-decoration:line-through;text-decoration-color:var(--warn)}

  .draftbar{display:flex;align-items:center;gap:12px;
            padding:6px 12px;border-radius:4px;
            background:var(--ink-2);border:1px solid var(--line)}
  .drafted{font:400 12px "Roboto Mono",monospace;color:var(--muted);white-space:nowrap}
  .drafted b{color:var(--chalk);font-weight:600}
  button.reset{
    padding:6px 11px;border-radius:3px;background:transparent;
    border:1px solid var(--line);color:var(--muted);cursor:pointer;
    font:600 10.5px/1 "Archivo Condensed",sans-serif;letter-spacing:.08em;text-transform:uppercase;
  }
  button.reset:hover{border-color:var(--warn);color:var(--warn)}

  /* ---- player detail ---- */
  .modal{position:fixed;inset:0;z-index:50;display:flex;align-items:center;justify-content:center;padding:24px}
  /* a class rule beats the UA [hidden] style, so restate it explicitly */
  .modal[hidden]{display:none}
  .modal-back{position:absolute;inset:0;background:rgba(4,10,16,.78)}
  .modal-card{
    position:relative;z-index:1;width:min(760px,100%);max-height:88vh;overflow-y:auto;
    background:var(--ink);border:1px solid var(--line);border-radius:6px;
    padding:26px 28px 30px;box-shadow:0 24px 70px rgba(0,0,0,.55);
  }
  .modal-x{
    position:absolute;top:14px;right:16px;width:30px;height:30px;
    background:transparent;border:1px solid var(--line);border-radius:3px;
    color:var(--muted);font:400 17px/1 Inter,sans-serif;cursor:pointer;
  }
  .modal-x:hover{border-color:var(--warn);color:var(--warn)}
  .m-head{border-bottom:2px solid var(--line);padding-bottom:14px;margin-bottom:18px}
  .m-name{font:700 27px/1.1 "Archivo Condensed",sans-serif;letter-spacing:.01em;
          text-transform:uppercase;margin-bottom:7px;padding-right:40px}
  .m-sub{display:flex;gap:9px;align-items:center;flex-wrap:wrap;
         font:400 12.5px "Roboto Mono",monospace;color:var(--muted)}
  .m-sec{margin-bottom:22px}
  .m-sec h4{
    font:600 10.5px/1 "Archivo Condensed",sans-serif;letter-spacing:.13em;
    text-transform:uppercase;color:var(--chalk);margin-bottom:11px;
    padding-bottom:7px;border-bottom:1px solid var(--line);
  }
  .m-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:13px}
  .m-item{font:400 12px "Roboto Mono",monospace}
  .m-item span{display:block;color:var(--muted);font-size:10.5px;
               text-transform:uppercase;letter-spacing:.07em;margin-bottom:3px}
  .m-item b{font:500 14px Inter,sans-serif;color:var(--paper)}
  table.m-stats{width:100%;border-collapse:collapse;white-space:normal}
  table.m-stats th{
    position:static;background:transparent;padding:7px 9px;
    font:600 10px/1 "Archivo Condensed",sans-serif;letter-spacing:.08em;
    color:var(--muted);text-align:right;border-bottom:1px solid var(--line);cursor:default;
  }
  table.m-stats th:first-child{text-align:left}
  table.m-stats td{padding:7px 9px;text-align:right;
                   font:500 13px "Roboto Mono",monospace;
                   border-bottom:1px solid rgba(34,57,77,.5)}
  table.m-stats td:first-child{text-align:left;font:400 12.5px Inter,sans-serif;color:var(--muted)}
  .m-depth{display:flex;flex-direction:column;gap:1px}
  .m-row{
    display:grid;grid-template-columns:46px 1fr auto auto;gap:11px;align-items:center;
    padding:9px 11px;border-radius:3px;font-size:13px;
  }
  .m-row.self{background:rgba(242,193,78,.11);box-shadow:inset 2px 0 0 var(--chalk)}
  .m-row .r{font:600 10.5px/1 "Archivo Condensed",sans-serif;letter-spacing:.06em;color:var(--muted)}
  .m-row .a{font:500 12px "Roboto Mono",monospace;color:var(--muted)}
  .m-row .p{font:500 12px "Roboto Mono",monospace;color:var(--pos-rb)}
  tbody tr{cursor:pointer}
  @media (max-width:640px){.modal{padding:10px}.modal-card{padding:20px 16px 24px}}

  td.prod{white-space:normal;min-width:230px;max-width:300px;
          font:400 12px/1.45 "Roboto Mono",monospace;color:#A8BCCC}

  .colgroups{display:flex;gap:14px;flex-wrap:wrap;align-items:center;
             padding:12px 14px;margin-bottom:14px;
             background:var(--ink-2);border:1px solid var(--line);border-radius:4px}
  .colgroups b{font:600 10.5px/1 "Archivo Condensed",sans-serif;letter-spacing:.1em;
               text-transform:uppercase;color:var(--muted);margin-right:2px}
  .hint{margin-left:auto;font:400 11.5px "Roboto Mono",monospace;color:var(--muted)}

  td.role{font:500 12px "Roboto Mono",monospace;color:var(--muted)}
  td.adp{position:relative}
  td.adp .src{
    display:inline-block;margin-left:6px;padding:1px 4px;border-radius:2px;
    background:var(--ink-3);color:var(--muted);
    font:500 9.5px/1.4 "Roboto Mono",monospace;vertical-align:middle;
  }
  .rookie{
    display:inline-block;margin-left:6px;padding:1px 4px;border-radius:2px;
    background:var(--chalk);color:var(--ink);
    font:700 9px/1.5 "Archivo Condensed",sans-serif;letter-spacing:.06em;vertical-align:middle;
  }
  .undrafted{font:600 9.5px/1 "Archivo Condensed",sans-serif;letter-spacing:.09em;
             color:var(--muted);border:1px solid var(--line);padding:3px 5px;border-radius:2px}
  .contested{color:var(--chalk);font-weight:600}
  .inj{font:600 10px/1 "Archivo Condensed",sans-serif;letter-spacing:.06em;
       text-transform:uppercase;color:var(--warn);border:1px solid var(--warn);
       padding:3px 5px;border-radius:2px}
  .ok{color:var(--muted)}
  .delta.up{color:var(--pos-rb)} .delta.down{color:var(--warn)}
  .empty{padding:48px;text-align:center;color:var(--muted)}
  footer{margin-top:20px;font:400 11.5px/1.7 "Roboto Mono",monospace;color:var(--muted)}
  @media (max-width:640px){h1{font-size:26px}.meta{text-align:left}}
  @media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Draft <span>Board</span></h1>
    <div class="meta" id="meta"></div>
  </header>

  <nav class="rail" id="rail" aria-label="Roster slot"></nav>

  <div class="filters">
    <input type="search" id="q" placeholder="Player or team…" aria-label="Search players">
    <select id="team" aria-label="Filter by team"></select>
    <label class="chk"><input type="checkbox" id="healthy"> Hide injured</label>
    <label class="chk"><input type="checkbox" id="scored"> Played last season</label>
    <label class="chk"><input type="checkbox" id="adponly"> Drafted players only</label>
    <span class="draftbar">
      <span class="drafted"><b id="takencount">0</b> drafted</span>
      <label class="chk"><input type="checkbox" id="showtaken"> Show drafted</label>
      <button class="reset" id="reset">Reset board</button>
    </span>
    <span class="count" id="count"></span>
  </div>

  <div class="colgroups">
    <b>Columns</b>
    <label class="chk"><input type="checkbox" id="g-adp" checked> ADP detail</label>
    <label class="chk"><input type="checkbox" id="g-prior" checked> Prior season</label>
    <label class="chk"><input type="checkbox" id="g-prod" checked> Production</label>
    <span class="hint">shift + scroll to pan sideways</span>
  </div>

  <div class="scroll">
    <table>
      <thead><tr id="head"></tr></thead>
      <tbody id="body"></tbody>
    </table>
  </div>
  <div class="empty" id="empty" hidden>No players match these filters.</div>

  <div class="modal" id="modal" hidden>
    <div class="modal-back" id="modal-back"></div>
    <div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="m-name">
      <button class="modal-x" id="modal-x" aria-label="Close">×</button>
      <div id="modal-body"></div>
    </div>
  </div>

  <footer id="foot"></footer>
</div>

<script>
const DATA = /*__DATA__*/;
const P = DATA.primary, R = DATA.prior;
const $ = id => document.getElementById(id);

const COLS = [
  {k:"__take",       h:"",       sticky:"c-take"},
  {k:"overall_rank", h:"#",      n:true, asc:true, sticky:"c-rank"},
  {k:"tier",         h:"Tier",   n:true, asc:true, cls:"tier", sticky:"c-tier"},
  {k:"name",         h:"Player", sticky:"c-name"},
  {k:"position",     h:"Pos"},
  {k:"team_role",    h:"Role"},
  {k:"team",         h:"Tm"},
  {k:"pos_rank",     h:"Pos Rk", n:true, asc:true},
  {k:"adp_composite",h:"ADP",    n:true, asc:true},
  {k:"adp_spread",   h:"Spread", n:true, g:"adp"},
  {k:"value_gap",    h:"Value",  n:true, g:"adp"},
  {k:`ppr_${P}`,     h:`${P} PPR`, n:true},
  {k:`ppg_${P}`,     h:`${P} PPG`, n:true},
  {k:`gp_${P}`,      h:`${P} GP`,  n:true},
  {k:"delta",        h:"vs "+R,  n:true, g:"prior"},
  {k:`ppr_${R}`,     h:`${R} PPR`, n:true, g:"prior"},
  {k:`ppg_${R}`,     h:`${R} PPG`, n:true, g:"prior"},
  {k:"keystat",      h:`${P} production`, g:"prod", cls:"prod"},
  {k:"injury_status",h:"Status"},
];

const GROUPS = {adp:true, prior:true, prod:true};
const visibleCols = () => COLS.filter(c => !c.g || GROUPS[c.g]);

const SLOTS = ["ALL","QB","RB1","RB2","WR1","WR2","WR3","TE","D/ST","K"];
const POS_ALL = [["QB","QB"],["RB","RB"],["WR","WR"],["TE","TE"],["K","K"],["DEF","D/ST"]];
// Slot buttons filter on a player's NFL depth chart role, not fantasy lineup slot.
const SLOT_ROLE = {QB:"QB1", RB1:"RB1", RB2:"RB2", WR1:"WR1", WR2:"WR2", WR3:"WR3",
                   TE:"TE1", K:"K1"};
const DEPTH_CAP = DATA.slot_depth || 200;
let view = {type:"all", value:"ALL"};
const HAS_ADP = DATA.players.some(p => p.adp_composite != null);
let sort = HAS_ADP ? {k:"adp_composite", dir:"asc"} : {k:`ppr_${P}`, dir:"desc"};

/* ---- draft mode: players already off the board ---- */
const STORE_KEY = "draftboard_taken";
let taken = new Set();
try {
  const saved = localStorage.getItem(STORE_KEY);
  if (saved) taken = new Set(JSON.parse(saved));
} catch(e) { /* file:// with storage disabled — falls back to memory only */ }

function persist(){
  try { localStorage.setItem(STORE_KEY, JSON.stringify([...taken])); } catch(e){}
}

function toggleTaken(id){
  if (taken.has(id)) taken.delete(id); else taken.add(id);
  persist(); buildRail(); render();
}

function resetBoard(){
  if (!taken.size) return;
  if (!confirm(`Put all ${taken.size} drafted players back on the board?`)) return;
  taken.clear(); persist(); buildRail(); render();
}

DATA.players.forEach(p => {
  p.delta = +( (p[`ppr_${P}`]||0) - (p[`ppr_${R}`]||0) ).toFixed(1);
  p.keystat = keystat(p);
});

function keystat(p){
  const n = v => (v||0).toLocaleString();
  switch(p.position){
    case "QB": return `${n(p[`pass_yd_${P}`])} pass yd · ${n(p[`pass_td_${P}`])} TD · ${n(p[`pass_int_${P}`])} INT · ${n(p[`rush_yd_${P}`])} rush yd`;
    case "RB": return `${n(p[`rush_att_${P}`])} att · ${n(p[`rush_yd_${P}`])} yd · ${n(p[`rush_td_${P}`])} TD · ${n(p[`rec_${P}`])} rec / ${n(p[`rec_yd_${P}`])} yd`;
    case "WR":
    case "TE": return `${n(p[`rec_tgt_${P}`])} tgt · ${n(p[`rec_${P}`])} rec · ${n(p[`rec_yd_${P}`])} yd · ${n(p[`rec_td_${P}`])} TD`;
    default:   return "—";
  }
}

const SRC_LABEL = {ffc:"FFCalc", espn:"ESPN", fantasycalc:"FantasyCalc", fantasypros:"FantasyPros"};

function adpDetail(p){
  const parts = Object.keys(SRC_LABEL)
    .filter(s => p["adp_"+s] != null)
    .map(s => `${SRC_LABEL[s]} ${p["adp_"+s]}`);
  return parts.length ? parts.join("  ·  ") : "no ADP found on any platform";
}

function matchesView(p, v){
  if (v.type === "all") return true;
  if (v.type === "pos") return p.position === v.value;
  // slot = NFL depth chart role, capped at positional draft rank
  if ((p.pos_rank || 9999) > DEPTH_CAP) return false;
  if (v.value === "D/ST") return p.position === "DEF";
  return p.team_role === SLOT_ROLE[v.value];
}

function countFor(v){
  return DATA.players.filter(p => !taken.has(p.player_id) && matchesView(p, v)).length;
}

function buildRail(){
  const btn = (v, label) =>
    `<button class="slot-btn" data-type="${v.type}" data-value="${v.value}"
       aria-pressed="${view.type===v.type && view.value===v.value}">
       ${label}<span class="n">${countFor(v)}</span></button>`;

  $("rail").innerHTML =
    `<div class="rail-row">
       <span class="rail-label">Position</span>
       ${btn({type:"all", value:"ALL"}, "All")}
       ${POS_ALL.map(([code,label]) => btn({type:"pos", value:code}, label)).join("")}
     </div>
     <div class="rail-row">
       <span class="rail-label">Team role</span>
       ${SLOTS.filter(s => s!=="ALL").map(s => btn({type:"slot", value:s}, s)).join("")}
     </div>`;

  $("rail").querySelectorAll("button").forEach(b =>
    b.onclick = () => {
      view = {type:b.dataset.type, value:b.dataset.value};
      buildRail(); render();
    });
}

function buildHead(){
  $("head").innerHTML = visibleCols().map(c =>
    `<th data-k="${c.k}" class="${c.sticky||""}" ${sort.k===c.k?`data-dir="${sort.dir}"`:""}>${c.h}</th>`).join("");
  $("head").querySelectorAll("th").forEach(th =>
    th.onclick = () => {
      const k = th.dataset.k;
      const col = COLS.find(c => c.k === k) || {};
      const first = col.asc ? "asc" : "desc";
      sort = {k, dir: sort.k===k ? (sort.dir===first ? (first==="asc"?"desc":"asc") : first) : first};
      buildHead(); render();
    });
}

function cell(p, c){
  const sticky = c.sticky ? ` ${c.sticky}` : "";
  const gone = taken.has(p.player_id);
  switch(c.k){
    case "__take":
      return `<td class="c-take">
        <button class="take ${gone?"back":""}" data-id="${p.player_id}"
          title="${gone?"Put back on the board":"Mark drafted"}"
          aria-label="${gone?"Restore":"Mark drafted"} ${esc(p.name)}">${gone?"↺":"×"}</button></td>`;
    case "tier":
      return `<td class="tier${sticky}" data-t="${p.tier??''}">${p.tier ?? "—"}</td>`;
    case "name":
      return `<td class="name${sticky}">${esc(p.name)}${
        p.is_rookie ? `<span class="rookie" title="Rookie">R</span>` : ""}</td>`;
    case "position":
      return `<td><span class="pos ${p.position}">${p.position}</span></td>`;
    case "team_role":
      return `<td class="role">${p.team_role || "—"}</td>`;
    case "adp_composite":
      return `<td class="num adp" title="${esc(adpDetail(p))}">${
        p.adp_composite != null
          ? `${p.adp_composite}<span class="src">${p.adp_sources_n}</span>`
          : `<span class="undrafted">UDFA</span>`}</td>`;
    case "adp_spread":
      return `<td class="num ${p.adp_spread>=25?"contested":""}">${p.adp_spread ?? "—"}</td>`;
    case "value_gap":
      return `<td class="num delta ${p.value_gap>8?"up":p.value_gap<-8?"down":""}">${
        p.value_gap==null ? "—" : (p.value_gap>0?"+":"")+p.value_gap}</td>`;
    case "delta":
      return `<td class="num delta ${p.delta>0?"up":p.delta<0?"down":""}">${
        p.delta>0?"+":""}${fmt(p.delta)}</td>`;
    case "keystat":
      return `<td class="prod">${esc(p.keystat)}</td>`;
    case "injury_status":
      return `<td>${p.injury_status
        ? `<span class="inj" title="${esc(p.injury_notes||"")}">${esc(p.injury_status)}</span>`
        : `<span class="ok">active</span>`}</td>`;
    case "overall_rank":
    case "pos_rank":
      return `<td class="num${sticky}">${p[c.k]}</td>`;
    default:
      return c.n ? `<td class="num">${fmt(p[c.k])}</td>`
                 : `<td>${esc(p[c.k] ?? "—")}</td>`;
  }
}

function render(){
  const rows = filtered();
  const cols = visibleCols();
  const showTaken = $("showtaken").checked;
  $("count").textContent = `${rows.length} available`;
  $("takencount").textContent = taken.size;
  $("empty").hidden = rows.length > 0;
  $("body").innerHTML = rows.slice(0, 600).map(p =>
    `<tr class="${taken.has(p.player_id)?"gone":""}" data-id="${p.player_id}">${
      cols.map(c => cell(p, c)).join("")}</tr>`).join("");
  $("body").querySelectorAll("button.take").forEach(b =>
    b.onclick = e => { e.stopPropagation(); toggleTaken(b.dataset.id); });
  $("body").querySelectorAll("tr").forEach(tr =>
    tr.onclick = () => openPlayer(tr.dataset.id));
}

function buildTeams(){
  const teams = [...new Set(DATA.players.map(p=>p.team))].filter(Boolean).sort();
  $("team").innerHTML = `<option value="">All teams</option>` +
    teams.map(t=>`<option>${t}</option>`).join("");
}

function filtered(){
  const q = $("q").value.trim().toLowerCase();
  const tm = $("team").value;
  const healthyOnly = $("healthy").checked;
  const scoredOnly = $("scored").checked;
  const showTaken = $("showtaken").checked;

  let out = DATA.players.filter(p => {
    if (!showTaken && taken.has(p.player_id)) return false;
    if ($("adponly").checked && p.adp_composite == null) return false;
    if (!matchesView(p, view)) return false;
    if (tm && p.team !== tm) return false;
    if (healthyOnly && p.injury_status) return false;
    if (scoredOnly && !(p[`ppr_${P}`] > 0)) return false;
    if (q && !(p.name.toLowerCase().includes(q) || (p.team||"").toLowerCase().includes(q))) return false;
    return true;
  });

  const {k, dir} = sort, sign = dir === "asc" ? 1 : -1;
  out.sort((a,b) => {
    const x = a[k], y = b[k];
    const xn = x == null, yn = y == null;
    if (xn && yn) return 0;
    if (xn) return 1;          // blanks always sink to the bottom
    if (yn) return -1;
    if (typeof x === "number" || typeof y === "number") return sign * (x - y);
    return sign * String(x).localeCompare(String(y));
  });
  return out;
}

/* ---- player detail ---- */
const STAT_ROWS = {
  QB: [["Games","gp"],["PPR points","ppr"],["Points per game","ppg"],
       ["Passing yards","pass_yd"],["Passing TD","pass_td"],["Interceptions","pass_int"],
       ["Rush attempts","rush_att"],["Rushing yards","rush_yd"],["Rushing TD","rush_td"]],
  RB: [["Games","gp"],["PPR points","ppr"],["Points per game","ppg"],
       ["Rush attempts","rush_att"],["Rushing yards","rush_yd"],["Rushing TD","rush_td"],
       ["Targets","rec_tgt"],["Receptions","rec"],["Receiving yards","rec_yd"],["Receiving TD","rec_td"]],
  WR: [["Games","gp"],["PPR points","ppr"],["Points per game","ppg"],
       ["Targets","rec_tgt"],["Receptions","rec"],["Receiving yards","rec_yd"],["Receiving TD","rec_td"],
       ["Rush attempts","rush_att"],["Rushing yards","rush_yd"],["Rushing TD","rush_td"]],
  DEF:[["Games","gp"],["PPR points","ppr"],["Points per game","ppg"]],
};
STAT_ROWS.TE = STAT_ROWS.WR;
STAT_ROWS.K  = STAT_ROWS.DEF;

const statVal = (p, key, season) => {
  const v = p[`${key}_${season}`];
  return (v === null || v === undefined) ? 0 : v;
};

function bioBlock(p){
  const items = [
    ["Position", p.position],
    ["Team", p.team],
    ["Depth chart", p.team_role || "not published"],
    ["Age", p.age ?? "—"],
    ["Experience", p.years_exp == null ? "—" : (p.years_exp === 0 ? "Rookie" : p.years_exp + " yr")],
    ["Height", p.height || "—"],
    ["Weight", p.weight ? p.weight + " lb" : "—"],
    ["College", p.college || "—"],
    ["Jersey", p.jersey != null ? "#" + p.jersey : "—"],
  ];
  return `<div class="m-grid">${items.map(([k,v]) =>
    `<div class="m-item"><span>${k}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
}

function marketBlock(p){
  if (p.adp_composite == null)
    return `<p class="m-item"><b>No ADP on any platform.</b> He's on the board for his
            depth chart role or rookie status, which usually means undrafted or a late flier.</p>`;
  const per = Object.keys(SRC_LABEL)
    .filter(s => p["adp_"+s] != null)
    .map(s => [SRC_LABEL[s], p["adp_"+s]]);
  const items = [
    ["Consensus ADP", p.adp_composite],
    ["Platforms", p.adp_sources_n],
    ["Rank spread", p.adp_spread ?? "—"],
    ["Value vs production", p.value_gap == null ? "—" : (p.value_gap > 0 ? "+" : "") + p.value_gap],
    ...per,
  ];
  return `<div class="m-grid">${items.map(([k,v]) =>
    `<div class="m-item"><span>${k}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
}

function statsBlock(p){
  const rows = STAT_ROWS[p.position] || STAT_ROWS.DEF;
  const body = rows.map(([label,key]) => {
    const a = statVal(p, key, P), b = statVal(p, key, R);
    if (!a && !b) return "";
    return `<tr><td>${label}</td><td>${a.toLocaleString()}</td><td>${b.toLocaleString()}</td></tr>`;
  }).filter(Boolean).join("");
  if (!body) return `<p class="m-item">No recorded production in either season.</p>`;
  return `<table class="m-stats">
    <thead><tr><th>Stat</th><th>${P}</th><th>${R}</th></tr></thead>
    <tbody>${body}</tbody></table>`;
}

function depthBlock(p){
  const chart = DATA.depth_charts[`${p.team}|${p.position}`];
  if (!chart || !chart.length)
    return `<p class="m-item">No published depth chart for ${esc(p.team)} ${esc(p.position)}.</p>`;
  return `<div class="m-depth">${chart.map(d => `
    <div class="m-row ${d.id === p.player_id ? "self" : ""}">
      <span class="r">${esc(d.role || "—")}</span>
      <span>${esc(d.name)}${d.rookie ? `<span class="rookie">R</span>` : ""}${
        d.inj ? ` <span class="inj">${esc(d.inj)}</span>` : ""}</span>
      <span class="a">${d.adp != null ? "ADP " + d.adp : "UDFA"}</span>
      <span class="p">${d.ppr ? d.ppr.toFixed(0) + " pts" : "—"}</span>
    </div>`).join("")}</div>`;
}

function section(title, fn){
  let inner;
  try {
    inner = fn();
  } catch (err) {
    inner = `<p class="m-item" style="color:var(--warn)">Couldn't build this section — ${esc(err.message)}</p>`;
  }
  return `<div class="m-sec"><h4>${title}</h4>${inner}</div>`;
}

function openPlayer(id){
  const p = DATA.players.find(x => x.player_id === id);
  if (!p) return;
  $("modal-body").innerHTML = `
    <div class="m-head">
      <div class="m-name" id="m-name">${esc(p.name)}</div>
      <div class="m-sub">
        <span class="pos ${p.position}">${p.position}</span>
        <span>${esc(p.team)}</span>
        <span>·</span><span>${esc(p.team_role || "depth chart TBD")}</span>
        ${p.is_rookie ? `<span class="rookie">R</span>` : ""}
        ${p.injury_status ? `<span class="inj">${esc(p.injury_status)}</span>` : ""}
      </div>
    </div>
    ${p.injury_notes ? `<div class="m-sec"><h4>Injury note</h4>
       <p class="m-item">${esc(p.injury_notes)}</p></div>` : ""}
    ${section("Bio", () => bioBlock(p))}
    ${section("Draft market", () => marketBlock(p))}
    ${section("Full stats", () => statsBlock(p))}
    ${section(`${esc(p.team)} ${esc(p.position)} depth chart`, () => depthBlock(p))}`;
  $("modal").hidden = false;
  $("modal-x").focus();
}

function closePlayer(){ $("modal").hidden = true; }

$("modal-x").addEventListener("click", closePlayer);
$("modal-back").addEventListener("click", closePlayer);
document.addEventListener("keydown", e => { if (e.key === "Escape") closePlayer(); });

const fmt = v => (v===null||v===undefined||v===0) ? "—" : (+v).toFixed(1);
const esc = s => String(s??"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

const A = DATA.adp;
$("meta").innerHTML =
  `PPR &nbsp;·&nbsp; ${DATA.teams}-team &nbsp;·&nbsp; stats ${P} vs ${R}` +
  (A ? ` &nbsp;·&nbsp; ADP ${DATA.adp_year}` : ``) + `<br>
   built ${DATA.generated}<br>
   stats: Sleeper` + (A && A.sources_live.length ? ` &nbsp;·&nbsp; ADP: ${A.sources_live.join(", ")}` : ``);

$("foot").innerHTML =
  `<p><b>Draft mode</b> — click any row to open that player's full profile: bio, both seasons of stats, per-platform ADP, and his team's depth chart at that position with him highlighted. Hit <b>×</b> on the left to take him off the board as he's picked. Slot counts in the buttons above drop as you go, so <i>RB1 4</i> means four RB1-caliber backs are still out there. Tick <b>Show drafted</b> to see them struck through with a <b>↺</b> to undo a misclick, and <b>Reset board</b> clears everything for the next mock. Your picks survive a page refresh.</p>
   <p><b>Role</b> is a player's spot on his actual NFL depth chart — WR2 means he's the second receiver on his own team. The <b>Team role</b> buttons above filter on exactly that: hit <b>RB2</b> and you get every team's second back, <b>WR3</b> every third receiver, ordered by ADP and capped at positional draft rank ${DEPTH_CAP}. Rookies whose depth charts aren't published yet won't appear under a role button — reach them from the <b>Position</b> row, which holds everyone. Tiers break at real scoring gaps, so the row where a tier changes is the cliff.</p>` +
  (A ? `<p><b>ADP</b> averages each platform's rank order, not its raw pick number — pick density differs between boards, rank order doesn't. The small badge is how many platforms carried that player. <b>Spread</b> is the gap between the highest and lowest platform rank; anything 25+ is highlighted, meaning the platforms genuinely disagree and there's an arbitrage window depending on where your leaguemates draft from. <b>Value</b> is consensus pick minus ${P} production rank — positive means the market is letting him fall.</p>
   <p>Live: ${A.sources_live.join(", ") || "none"}${A.sources_failed.length ? ` · unavailable this run: ${A.sources_failed.join(", ")}` : ""}. ${A.matched} of ${A.players} ADP entries matched the board; ${A.players_multi_source} were confirmed by two or more platforms. Yahoo isn't queried directly (OAuth), but FantasyPros' consensus includes it. ADP courtesy of <a href="https://fantasyfootballcalculator.com" style="color:var(--chalk)">Fantasy Football Calculator</a>, ESPN, FantasyCalc and FantasyPros.</p>` : ``) +
  `<p>The board carries every player with ADP on at least one platform, plus every rookie and anyone without ADP who still holds a real role on his own depth chart — RB1–RB2, WR1–WR3, TE1, QB1, K1, and all 32 defenses. Rookies carry an <b>R</b> badge; their depth charts often aren't published until preseason, so they're kept regardless. Players with no ADP show <b>UDFA</b> in the ADP column and sit below the drafted players; tick <b>Drafted players only</b> to hide them. <b>#</b> is board order, so row 1 goes first overall. Everyone else is left out but stays in the CSV beside this file. Rookies show ADP with no ${P} production, which is expected — tick <b>Played last season</b> to hide them.</p>`;

["q","team","healthy","scored","showtaken","adponly"].forEach(id =>
  $(id).addEventListener(id==="q" ? "input" : "change", render));
$("reset").addEventListener("click", resetBoard);

Object.keys(GROUPS).forEach(g =>
  $("g-"+g).addEventListener("change", e => {
    GROUPS[g] = e.target.checked;
    if (!visibleCols().some(c => c.k === sort.k))
      sort = HAS_ADP ? {k:"adp_composite", dir:"asc"} : {k:`ppr_${P}`, dir:"desc"};
    buildHead(); render();
  }));

buildRail(); buildHead(); buildTeams(); render();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------

def build_depth_charts(rows, primary):
    """
    Team + position depth charts, drawn from the whole player universe so a
    detail view shows teammates who never made the board.
    """
    charts = {}
    for r in rows:
        pos, team = r["position"], r.get("team")
        if pos == "DEF" or not team or team == "FA":
            continue
        charts.setdefault(f"{team}|{pos}", []).append({
            "id": r["player_id"],
            "name": r["name"],
            "role": r.get("team_role") or "",
            "depth": r.get("depth_chart_order") or 99,
            "adp": r.get("adp_composite"),
            "ppr": r.get(f"ppr_{primary}") or 0,
            "inj": r.get("injury_status") or "",
            "rookie": bool(r.get("is_rookie")),
        })

    for key, group in charts.items():
        # Listed depth first, then unlisted by production.
        group.sort(key=lambda x: (x["depth"], -x["ppr"]))
        charts[key] = group[:8]
    return charts


def diagnose(rows, needle, primary, prior):
    """Explain exactly why a player is on or off the board."""
    q = needle.lower().strip()
    hits = [r for r in rows if q in r["name"].lower()]
    if not hits:
        print(f"\n  '{needle}' isn't in Sleeper's player file at all.")
        print("  Either the spelling differs or Sleeper hasn't added him yet.")
        return

    for r in sorted(hits, key=lambda r: -(r.get(f"ppr_{primary}") or 0))[:6]:
        print(f"\n  {r['name']}  —  {r['position']} {r['team']}")
        print(f"    experience      {r.get('years_exp')} yr"
              f"{'  (ROOKIE)' if r.get('is_rookie') else ''}")
        print(f"    depth chart     {r.get('team_role') or 'not published'}")
        print(f"    {primary} PPR      {r.get(f'ppr_{primary}') or 0}"
              f"   ({r.get(f'gp_{primary}') or 0} games)")
        print(f"    {prior} PPR      {r.get(f'ppr_{prior}') or 0}")
        if r.get("adp_composite") is not None:
            per = ", ".join(f"{s}={r.get('adp_'+s)}"
                            for s, _ in adp_sources.SOURCES if r.get("adp_" + s) is not None)
            print(f"    ADP             {r['adp_composite']}  [{per}]")
        else:
            print(f"    ADP             none matched on any platform")
        if r.get("injury_status"):
            print(f"    injury          {r['injury_status']}")

        if on_board(r):
            why = ("carries ADP" if r.get("adp_composite") is not None else
                   "team defense" if r["position"] == "DEF" else
                   "rookie" if r.get("is_rookie") else
                   f"depth chart {r.get('team_role')}")
            print(f"    -> ON the board ({why})")
        else:
            print(f"    -> OFF the board: no ADP, not a rookie, and depth chart "
                  f"{r.get('team_role') or 'unknown'} is below the "
                  f"{DEPTH_KEEP.get(r['position'], '?')} cutoff for {r['position']}")


def main():
    ap = argparse.ArgumentParser(description="Build a PPR fantasy draft board from live Sleeper data.")
    ap.add_argument("--seasons", nargs="+", default=["2025", "2024"],
                    help="Seasons, most recent first (default: 2025 2024)")
    ap.add_argument("--teams", type=int, default=12, help="League size (default: 12)")
    ap.add_argument("--outdir", default=".", help="Output directory (default: current dir)")
    ap.add_argument("--adp-year", type=int, default=None,
                    help="Draft year for ADP (default: season after most recent stats)")
    ap.add_argument("--no-adp", action="store_true", help="Skip multi-platform ADP")
    ap.add_argument("--slot-depth", type=int, default=200, metavar="N",
                    help="Cap slot lists at positional draft rank N (default: 200)")
    ap.add_argument("--find", metavar="NAME",
                    help="Look up a player and explain why he is on or off the board")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    seasons = args.seasons[:2]
    adp_year = args.adp_year or (int(seasons[0]) + 1)

    print("Stats — Sleeper (free, read-only, no key)")
    players = fetch_players()

    stats_by_season = {}
    for s in seasons:
        try:
            stats_by_season[s] = fetch_season_stats(s)
        except Exception as e:
            print(f"  !! {s} stats unavailable: {e}")
            stats_by_season[s] = {}

    print("Shaping...")
    rows = build_rows(players, stats_by_season, seasons)
    rows = assign_ranks_and_slots(rows, args.teams, seasons[0])

    adp_info = None
    if not args.no_adp:
        if not ADP_AVAILABLE:
            print("\n  !! adp_sources.py not found next to this script — skipping ADP.")
        else:
            print(f"\nADP — multi-platform, {adp_year} drafts, {args.teams}-team PPR")
            fetched, errors = adp_sources.collect(args.teams, adp_year, "ppr")
            composite = adp_sources.build_composite(fetched)
            matched = merge_adp(rows, composite, seasons[0])
            adp_info = adp_sources.summarize(composite, fetched, errors)
            adp_info["matched"] = matched
            print(f"  matched {matched} players to the board")

    if args.find:
        diagnose(rows, args.find, seasons[0], seasons[1] if len(seasons) > 1 else seasons[0])
        return

    board = rows
    if adp_info and adp_info.get("matched"):
        board = [r for r in rows if on_board(r)]
        # ADP players first in draft order; depth-chart holdovers after,
        # best production first.
        board.sort(key=lambda r: (
            r.get("adp_composite") is None,
            r.get("adp_composite") if r.get("adp_composite") is not None else 0,
            -r.get(f"ppr_{seasons[0]}", 0),
        ))
        for i, r in enumerate(board):
            r["overall_rank"] = i + 1
        assign_slots(board, args.teams, seasons[0])
        with_adp = sum(1 for r in board if r.get("adp_composite") is not None)
        rookies = sum(1 for r in board if r.get("is_rookie"))
        depth_adds = len(board) - with_adp
        print(f"  board: {len(board)} players — {with_adp} with ADP, "
              f"{depth_adds} kept on depth chart role or rookie status "
              f"({rookies} rookies total)")
        print(f"  ({len(rows) - len(board)} irrelevant players held back in the CSV)")

    csv_path = os.path.join(args.outdir, "nfl_fantasy_ppr.csv")
    html_path = os.path.join(args.outdir, "index.html")
    write_csv(rows, csv_path)
    charts = build_depth_charts(rows, seasons[0])
    write_html(board, html_path, seasons, args.teams, adp_info, adp_year,
               args.slot_depth, charts)

    scored = sum(1 for r in board if r.get(f"ppr_{seasons[0]}", 0) > 0)
    print(f"\nDone. {len(board)} players on the board ({scored} with {seasons[0]} production).")
    print(f"Full dataset of {len(rows)} players is in the CSV.")
    print(f"Open {html_path} in your browser.")


if __name__ == "__main__":
    main()
