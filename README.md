# Fantasy Draft Board

PPR draft board built from live data. Rebuilds itself every night and publishes
to GitHub Pages.

- **Stats** — [Sleeper](https://docs.sleeper.com) (free, no key)
- **ADP** — [Fantasy Football Calculator](https://fantasyfootballcalculator.com),
  ESPN, FantasyCalc, and FantasyPros, blended into a consensus

## Setup

1. Push this repo to GitHub.
2. **Settings → Pages → Source: GitHub Actions**
3. **Actions → Build draft board → Run workflow** to build immediately.

Your board lands at `https://raulvacab.github.io/roronfl/`.

### League settings

Change these without touching code, under **Settings → Secrets and variables →
Actions → Variables**:

| Variable | Default | What it does |
|---|---|---|
| `LEAGUE_TEAMS` | `12` | League size |
| `SLOT_DEPTH` | `200` | Cap on positional rank in team-role views |

## Running locally

```bash
pip install -r requirements.txt
python build_draft_board.py --teams 12
```

Outputs `index.html` and `nfl_fantasy_ppr.csv`.

### Useful flags

```bash
--teams 10                 # league size
--slot-depth 60            # tighter team-role lists
--seasons 2025 2024        # which seasons to compare
--adp-year 2026            # draft year for ADP
--no-adp                   # skip the ADP pass (much faster)
--outdir public            # where to write
--html-name board.html     # rename the output (default: index.html)
--find "Jeremiyah Love"    # why is this player on/off the board?
--min-players 100          # abort instead of publishing an empty board
```

## How the columns work

**Role** — a player's spot on his own NFL depth chart. WR2 means second
receiver on his team.

**ADP** — consensus across platforms. Each platform is converted to a rank
before averaging, since raw pick numbers aren't comparable across boards of
different sizes. The small badge is how many platforms carried him.

**Spread** — gap between his highest and lowest platform rank. 25+ is
highlighted. Large spread means the platforms disagree, which is where draft
value hides.

**Value** — consensus pick minus last season's production rank. Positive means
the market is letting him fall. Check the games-played column before trusting a
big negative: a player who missed half a season looks overpriced on totals even
if his per-game rate was fine.

**Tier** — breaks at real scoring gaps within a position. The row where the tier
changes is the cliff.

## Draft mode

Click **×** on a row to take a player off the board as he's picked. Role and
position counts drop as you go. **Show drafted** reveals them struck through
with an undo. Picks persist in browser storage.

Click any row for full stats, bio, and that team's depth chart at his position.

## Notes

- Scheduled workflows are **disabled after 60 days without a commit** to the
  repo. If the board goes stale, push anything or hit Run workflow.
- Cron times are UTC and GitHub may delay runs under load.
- The build aborts rather than publishing if the board comes back suspiciously
  small, so a failed upstream API leaves the previous board live.
- A public repo means a public board. It's only public fantasy data, but use a
  private repo if you'd rather not share your prep.
