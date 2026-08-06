#!/usr/bin/env python3
"""
adp_sources.py
--------------
Pulls Average Draft Position from multiple fantasy platforms and blends them
into a composite, keeping every source's individual number so you can see where
the platforms disagree — which is where draft value actually lives.

Sources (all free, no credentials):
  ffc          Fantasy Football Calculator  - real mock draft ADP, JSON API
  espn         ESPN                         - averageDraftPosition, PPR defaults
  fantasycalc  FantasyCalc                  - ADP + trade value, JSON API
  fantasypros  FantasyPros                  - consensus of Yahoo/ESPN/CBS/NFFC/Sleeper (HTML)

Yahoo is omitted deliberately: its API requires OAuth. FantasyPros' consensus
already folds Yahoo in.

Imported by build_draft_board.py, but runnable standalone:
    python adp_sources.py --teams 12 --year 2026
"""

import argparse
import json
import re
import statistics
import sys

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

HTTP_TIMEOUT = 45
UA = {"User-Agent": "Mozilla/5.0 (compatible; draft-board/1.0)"}

FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year={year}"
ESPN_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}"
            "/segments/0/leaguedefaults/3?view=kona_player_info")
FANTASYCALC_URL = ("https://api.fantasycalc.com/values/current?isDynasty=false"
                   "&numQbs=1&numTeams={teams}&ppr=1&includeAdp=true")
FANTASYPROS_URL = "https://www.fantasypros.com/nfl/adp/ppr-overall.php"

ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}
ESPN_TEAM = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA",
    16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI",
    23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}

# Platforms spell these differently; normalize so joins land.
TEAM_ALIAS = {
    "JAC": "JAX", "WSH": "WAS", "WFT": "WAS", "LA": "LAR", "STL": "LAR",
    "SD": "LAC", "OAK": "LV", "LVR": "LV", "ARZ": "ARI", "BLT": "BAL",
    "CLV": "CLE", "HST": "HOU", "GBP": "GB", "KCC": "KC", "NEP": "NE",
    "NOS": "NO", "SFO": "SF", "TBB": "TB",
}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Full team names -> abbreviation, for matching D/ST across platforms.
DEF_NAMES = {
    "cardinals": "ARI", "falcons": "ATL", "ravens": "BAL", "bills": "BUF",
    "panthers": "CAR", "bears": "CHI", "bengals": "CIN", "browns": "CLE",
    "cowboys": "DAL", "broncos": "DEN", "lions": "DET", "packers": "GB",
    "texans": "HOU", "colts": "IND", "jaguars": "JAX", "chiefs": "KC",
    "raiders": "LV", "chargers": "LAC", "rams": "LAR", "dolphins": "MIA",
    "vikings": "MIN", "patriots": "NE", "saints": "NO", "giants": "NYG",
    "jets": "NYJ", "eagles": "PHI", "steelers": "PIT", "49ers": "SF",
    "niners": "SF", "seahawks": "SEA", "buccaneers": "TB", "titans": "TEN",
    "commanders": "WAS",
}


# --------------------------------------------------------------------------
# Name normalization — the join key across platforms
# --------------------------------------------------------------------------

def norm_team(t):
    t = (t or "").strip().upper()
    return TEAM_ALIAS.get(t, t)


def norm_name(name):
    """Lowercase, strip punctuation and generational suffixes."""
    s = (name or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[.'`’\-]", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    parts = [p for p in s.split() if p]
    while parts and parts[-1] in SUFFIXES:
        parts.pop()
    return " ".join(parts)


def make_key(name, position, team=""):
    """
    Join key. Defenses key on team alone (platforms name them wildly
    differently: 'Bills D/ST', 'Buffalo Bills', 'BUF Defense').
    """
    pos = (position or "").upper()
    if pos in ("DEF", "DST", "D/ST"):
        abbr = norm_team(team)
        if not abbr or abbr == "FA":
            for word in norm_name(name).split():
                if word in DEF_NAMES:
                    abbr = DEF_NAMES[word]
                    break
        return f"DEF|{abbr}"
    return f"{pos}|{norm_name(name)}"


def canon_pos(p):
    p = (p or "").upper()
    if p in ("DST", "D/ST", "DEF"):
        return "DEF"
    if p == "PK":
        return "K"
    return p


# --------------------------------------------------------------------------
# Individual source fetchers — each returns [{key, name, position, team, adp}]
# --------------------------------------------------------------------------

def fetch_ffc(teams, year, scoring="ppr"):
    url = FFC_URL.format(fmt=scoring, teams=teams, year=year)
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    payload = r.json()
    out = []
    for p in payload.get("players", []):
        adp = p.get("adp")
        if adp in (None, 0):
            continue
        pos = canon_pos(p.get("position"))
        team = norm_team(p.get("team"))
        out.append({
            "key": make_key(p.get("name"), pos, team),
            "name": p.get("name"), "position": pos, "team": team,
            "adp": float(adp),
            "stdev": float(p.get("stdev") or 0),
        })
    return out


def fetch_espn(year):
    filt = {"players": {"limit": 1200,
                        "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "PPR"}}}
    headers = dict(UA)
    headers["X-Fantasy-Filter"] = json.dumps(filt)
    r = requests.get(ESPN_URL.format(year=year), timeout=HTTP_TIMEOUT, headers=headers)
    r.raise_for_status()
    payload = r.json()
    out = []
    for entry in payload.get("players", []):
        pl = entry.get("player") or {}
        adp = ((pl.get("ownership") or {}).get("averageDraftPosition"))
        if not adp or adp <= 0:
            continue
        pos = ESPN_POS.get(pl.get("defaultPositionId"), "")
        team = ESPN_TEAM.get(pl.get("proTeamId"), "FA")
        name = pl.get("fullName") or ""
        out.append({
            "key": make_key(name, pos, team),
            "name": name, "position": pos, "team": team,
            "adp": float(adp),
        })
    return out


def fetch_fantasycalc(teams):
    r = requests.get(FANTASYCALC_URL.format(teams=teams), timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    payload = r.json()
    out = []
    for entry in payload:
        adp = entry.get("adp") or entry.get("redraftAdp")
        if not adp or adp <= 0:
            continue
        pl = entry.get("player") or {}
        pos = canon_pos(pl.get("position"))
        team = norm_team(pl.get("maybeTeam") or pl.get("team"))
        name = pl.get("name") or ""
        out.append({
            "key": make_key(name, pos, team),
            "name": name, "position": pos, "team": team,
            "adp": float(adp),
            "trade_value": entry.get("value"),
        })
    return out


def fetch_fantasypros():
    """
    Scrape the consensus table. Best-effort: FantasyPros changes markup
    periodically, so this degrades to empty rather than raising.
    """
    r = requests.get(FANTASYPROS_URL, timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    html = r.text

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I)
    out = []
    for row in rows:
        cells = [re.sub(r"<[^>]+>", " ", c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
        cells = [re.sub(r"\s+", " ", c).replace("&nbsp;", " ").strip() for c in cells]
        if len(cells) < 4:
            continue

        # Player cell looks like: "Ja'Marr Chase CIN (9)" — name, team, bye.
        player_cell = cells[1] if len(cells) > 1 else ""
        m = re.match(r"^(.+?)\s+([A-Z]{2,3})\s*(?:\(\d+\))?\s*$", player_cell)
        if not m:
            continue
        name, team = m.group(1).strip(), norm_team(m.group(2))

        # Position cell looks like "WR1", "RB12", "DST3".
        pos_cell = next((c for c in cells[2:5] if re.match(r"^[A-Z]{1,3}\d+$", c)), "")
        pos = canon_pos(re.sub(r"\d+", "", pos_cell))
        if not pos:
            continue

        adp = None
        for c in reversed(cells):
            try:
                v = float(c)
                if 0 < v < 400:
                    adp = v
                    break
            except ValueError:
                continue
        if adp is None:
            continue

        out.append({
            "key": make_key(name, pos, team),
            "name": name, "position": pos, "team": team, "adp": adp,
        })
    return out


# --------------------------------------------------------------------------
# Composite
# --------------------------------------------------------------------------

SOURCES = [
    ("ffc",         "Fantasy Football Calculator"),
    ("espn",        "ESPN"),
    ("fantasycalc", "FantasyCalc"),
    ("fantasypros", "FantasyPros consensus"),
]


def collect(teams=12, year=2026, scoring="ppr", verbose=True):
    """Fetch every source. A failure on one degrades the composite, not the run."""
    fetched, errors = {}, {}
    plan = [
        ("ffc",         lambda: fetch_ffc(teams, year, scoring)),
        ("espn",        lambda: fetch_espn(year)),
        ("fantasycalc", lambda: fetch_fantasycalc(teams)),
        ("fantasypros", fetch_fantasypros),
    ]
    for sid, fn in plan:
        label = dict(SOURCES)[sid]
        try:
            if verbose:
                print(f"  -> {label} ...", end="", flush=True)
            rows = fn()
            fetched[sid] = rows
            if verbose:
                print(f" ok ({len(rows)} players)")
        except Exception as e:
            errors[sid] = str(e)[:160]
            fetched[sid] = []
            if verbose:
                print(f" FAILED — {str(e)[:90]}")
    return fetched, errors


def build_composite(fetched):
    """
    Blend sources into one number per player.

    Each source is converted to a *rank* before averaging. Raw ADP isn't
    comparable across platforms — a 12-team FFC board and ESPN's default
    board have different pick densities — but rank order is.
    """
    merged = {}

    for sid, rows in fetched.items():
        ordered = sorted([r for r in rows if r.get("adp")], key=lambda r: r["adp"])
        for i, r in enumerate(ordered):
            slot = merged.setdefault(r["key"], {
                "key": r["key"], "name": r["name"],
                "position": r["position"], "team": r["team"],
                "adp": {}, "rank": {}, "extra": {},
            })
            slot["adp"][sid] = round(r["adp"], 1)
            slot["rank"][sid] = i + 1
            if not slot.get("name"):
                slot["name"] = r["name"]
            if r.get("trade_value") is not None:
                slot["extra"]["trade_value"] = r["trade_value"]
            if r.get("stdev"):
                slot["extra"]["ffc_stdev"] = r["stdev"]

    for slot in merged.values():
        ranks = list(slot["rank"].values())
        adps = list(slot["adp"].values())
        slot["n_sources"] = len(ranks)
        slot["composite_rank"] = round(statistics.mean(ranks), 1)
        slot["composite_adp"] = round(statistics.mean(adps), 1)
        # Spread across platforms in rank terms — big spread = contested valuation.
        slot["spread"] = (max(ranks) - min(ranks)) if len(ranks) > 1 else 0
        slot["high"] = min(ranks) if ranks else None
        slot["low"] = max(ranks) if ranks else None

    ordered = sorted(merged.values(), key=lambda s: s["composite_rank"])
    for i, slot in enumerate(ordered):
        slot["consensus_pick"] = i + 1
    return {s["key"]: s for s in ordered}


def summarize(composite, fetched, errors):
    live = [dict(SOURCES)[s] for s, rows in fetched.items() if rows]
    dead = [dict(SOURCES)[s] for s in errors]
    multi = sum(1 for s in composite.values() if s["n_sources"] > 1)
    return {
        "sources_live": live,
        "sources_failed": dead,
        "errors": errors,
        "players": len(composite),
        "players_multi_source": multi,
    }


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Aggregate fantasy ADP across platforms.")
    ap.add_argument("--teams", type=int, default=12)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--scoring", default="ppr", choices=["ppr", "half-ppr", "standard", "2qb"])
    ap.add_argument("--out", default="adp_composite.csv")
    args = ap.parse_args()

    print("Pulling ADP across platforms")
    fetched, errors = collect(args.teams, args.year, args.scoring)
    composite = build_composite(fetched)
    info = summarize(composite, fetched, errors)

    import csv
    ids = [s for s, _ in SOURCES]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["consensus_pick", "name", "position", "team", "composite_adp",
                    "composite_rank", "n_sources", "spread"] +
                   [f"adp_{s}" for s in ids])
        for slot in sorted(composite.values(), key=lambda s: s["consensus_pick"]):
            w.writerow([slot["consensus_pick"], slot["name"], slot["position"], slot["team"],
                        slot["composite_adp"], slot["composite_rank"],
                        slot["n_sources"], slot["spread"]] +
                       [slot["adp"].get(s, "") for s in ids])

    print(f"\nLive: {', '.join(info['sources_live']) or 'none'}")
    if info["sources_failed"]:
        print(f"Failed: {', '.join(info['sources_failed'])}")
    print(f"{info['players']} players, {info['players_multi_source']} confirmed by 2+ platforms")
    print(f"CSV -> {args.out}")


if __name__ == "__main__":
    main()
