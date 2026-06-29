"""
calculate_scores.py
World Cup 2026 Charity Buster — Automated Score Calculator
-----------------------------------------------------------
Fetches live match results from football-data.org and calculates
points for each participant based on their team picks.

Scoring rules:
  Match result  : Win = +3, Draw = +1 (group stage only), Loss = 0
  Stage bonus   : +3 for each round a team advances through
  Tournament win: +10 for winning the Final

Run by GitHub Actions every 2 hours during the tournament.
Output: scores.json (committed back to the repo automatically)
"""

import os
import json
import requests
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

# ── API CONFIG ────────────────────────────────────────────────────────────────
API_KEY  = os.environ["FD_API_KEY"]          # Set in GitHub Secrets
BASE_URL = "https://api.football-data.org/v4"
HEADERS  = {"X-Auth-Token": API_KEY}
SEASON   = 2026

# ── PARTICIPANT PICKS ─────────────────────────────────────────────────────────────────────────────────
# Team names must match the normalised names in TEAM_NAME_MAP below.
# Add a new dict entry for each additional participant.
PARTICIPANTS = [
    {"name": "Brian Lehane", "teams": ["England", "Japan", "Norway", "Turkey", "Germany"]},
    {"name": "Squirrel", "teams": ["Spain", "Morocco", "Norway", "Ghana", "France"]},
    {"name": "Niamh Corrigan", "teams": ["Spain", "Colombia", "Norway", "Turkey", "France"]},
    {"name": "MICKODEA11", "teams": ["France", "Switzerland", "South Africa", "Czech Republic", "England"]},
    {"name": "Adam Feery", "teams": ["Brazil", "Croatia", "Norway", "Turkey", "France"]},
    {"name": "Mick F", "teams": ["France", "Croatia", "Egypt", "Czech Republic", "Spain"]},
    {"name": "Paul Boreham", "teams": ["England", "Senegal", "Norway", "Sweden", "Spain"]},
    {"name": "Brian Cooper", "teams": ["Spain", "Colombia", "Scotland", "Czech Republic", "Brazil"]},
    {"name": "Sean Purcell", "teams": ["France", "Colombia", "Norway", "Turkey", "Brazil"]},
    {"name": "Monique Mehaffey", "teams": ["Spain", "Croatia", "Algeria", "Sweden", "Argentina"]},
    {"name": "Ivan Peharda", "teams": ["Argentina", "Croatia", "Norway", "New Zealand", "South Africa"]},
    {"name": "Brian O Connell", "teams": ["Brazil", "Japan", "Scotland", "Curaçao", "Germany"]},
    {"name": "Erika Kind", "teams": ["Argentina", "Colombia", "Paraguay", "Turkey", "Brazil"]},
    {"name": "John Pepper", "teams": ["Spain", "Colombia", "Norway", "Czech Republic", "France"]},
    {"name": "Michael Murphy", "teams": ["Spain", "Colombia", "Norway", "Bosnia and Herzegovina", "Brazil"]},
    {"name": "Jack O'Connor", "teams": ["France", "Croatia", "Norway", "Sweden", "Spain"]},
    {"name": "Adrian Kehoe", "teams": ["Spain", "Croatia", "Norway", "New Zealand", "Argentina"]},
    {"name": "Clinton Ogundero", "teams": ["France", "Uruguay", "Scotland", "Ghana", "England"]},
    {"name": "Derek (Ted) Mccarthy", "teams": ["England", "Colombia", "Norway", "Sweden", "Belgium"]},
    {"name": "Denis O Connell", "teams": ["Spain", "Senegal", "Scotland", "Turkey", "France"]},
    {"name": "Tim Greene", "teams": ["Netherlands", "Uruguay", "Norway", "Sweden", "Portugal"]},
    {"name": "Guzza", "teams": ["France", "Uruguay", "Norway", "Sweden", "Argentina"]},
    {"name": "Eric Murphy", "teams": ["France", "Croatia", "Ivory Coast", "Turkey", "Spain"]},
    {"name": "Hillington FC", "teams": ["Spain", "Morocco", "Norway", "Turkey", "France"]},
]
# ── TEAM NAME NORMALISATION ───────────────────────────────────────────────────
# Maps football-data.org API team names → names used in PARTICIPANTS above.
# Extend this if the API returns unexpected names.
TEAM_NAME_MAP = {
    "United States": "United States",
    "USA":           "United States",
    "Côte d'Ivoire": "Ivory Coast",
    "Korea Republic":"South Korea",
    "Czechia":       "Czech Republic",
    "Bosnia-Herzegovina":     "Bosnia and Herzegovina",  # API uses hyphen
    "Bosnia and Herzegovina":  "Bosnia and Herzegovina",
    "DR Congo":      "DR Congo",
    "Congo DR":      "DR Congo",
    "Cabo Verde":    "Cape Verde",
    "Cape Verde Islands": "Cape Verde",                  # API variant
    "England":       "England",      # API uses "England" not "Great Britain"
}

def normalise(name: str) -> str:
    """Return a canonical team name, or the original if not in the map."""
    return TEAM_NAME_MAP.get(name, name)


# ── STAGE ORDERING ────────────────────────────────────────────────────────────
# Maps football-data.org stage strings to a numeric rank (higher = further).
STAGE_RANK = {
    "GROUP_STAGE":    1,
    "LAST_32":        2,   # API name for Round of 32
    "LAST_16":        3,   # API name for Round of 16
    "QUARTER_FINALS": 4,
    "SEMI_FINALS":    5,
    "THIRD_PLACE":    5,   # 3rd place playoff (same round as Final)
    "FINAL":          6,
}

# Human-readable stage banners shown on the leaderboard page
STAGE_DISPLAY = {
    "PRE_TOURNAMENT": "⏳ Tournament begins 11 June 2026",
    "GROUP_STAGE":    "🟢 Group Stage — In Progress",
    "LAST_32":        "⚽ Round of 32 — In Progress",
    "LAST_16":        "⚽ Round of 16 — In Progress",
    "QUARTER_FINALS": "⚽ Quarter-Finals — In Progress",
    "SEMI_FINALS":    "⚽ Semi-Finals — In Progress",
    "FINAL":          "🏆 The Final — In Progress",
    "COMPLETE":       "🏆 Tournament Complete",
}

# Stage bonus points awarded when a team REACHES each stage
# (i.e. they won the previous round)
STAGE_BONUS = {
    "LAST_32":        3,   # qualified from group stage
    "LAST_16":        3,   # won Round of 32
    "QUARTER_FINALS": 3,   # won Round of 16
    "SEMI_FINALS":    3,   # won Quarter-Final
    "FINAL":          3,   # won Semi-Final
    # THIRD_PLACE: no bonus (teams already earned SEMI_FINALS bonus)
}
WINNER_BONUS = 10          # awarded to team that wins the Final


def fetch_matches() -> list:
    """
    Fetch all WC 2026 matches and merge with previously confirmed results.

    Strategy:
      1. Load results.json (confirmed matches already scored — never removed)
      2. Fetch all matches from API — identifies FINISHED matches
      3. For FINISHED matches with null scores, fetch individually
      4. Merge new confirmed results into results.json
      5. Return full match list using confirmed scores where available

    This means once a match result is confirmed it is locked in permanently,
    even if the API later reverts the match to TIMED/SCHEDULED.
    """
    results_file = Path("results.json")
    confirmed = json.loads(results_file.read_text()) if results_file.exists() else {}
    prev_count = len(confirmed)

    # Fetch all matches (schedule + status info)
    url = f"{BASE_URL}/competitions/WC/matches"
    resp = requests.get(url, headers=HEADERS,
                        params={"season": SEASON}, timeout=15)
    resp.raise_for_status()
    matches = resp.json().get("matches", [])

    # For FINISHED matches not yet confirmed, fetch real scores individually
    new_confirmed = 0
    for i, m in enumerate(matches):
        mid = str(m["id"])
        if mid in confirmed:
            # Already locked in — use confirmed result, ignore API status
            matches[i] = confirmed[mid]
            continue

        if m.get("status") == "FINISHED":
            ft = m.get("score", {}).get("fullTime", {})
            if ft.get("home") is None:
                # Null score — fetch individually
                detail = requests.get(
                    f"{BASE_URL}/matches/{mid}",
                    headers=HEADERS, timeout=15
                )
                if detail.ok:
                    matches[i] = detail.json()
                    ft = matches[i].get("score", {}).get("fullTime", {})

            # Confirm and lock if we now have a real score
            if ft.get("home") is not None:
                confirmed[mid] = matches[i]
                new_confirmed += 1
                print(f"   🔒 Locked result: "
                      f"{matches[i]['homeTeam']['name']} "
                      f"{ft['home']}-{ft['away']} "
                      f"{matches[i]['awayTeam']['name']}")

    # Save updated confirmed results
    if new_confirmed > 0:
        results_file.write_text(json.dumps(confirmed, indent=2))
        print(f"   📁 results.json: {prev_count} → {len(confirmed)} confirmed matches")
    else:
        print(f"   📁 results.json: {len(confirmed)} confirmed match(es) — no new results")

    return matches


def build_team_stats(matches: list, confirmed: dict) -> dict:
    """
    Build a per-team stats dict from match results.

    Returns:
        {
          "France": {
              "matchPts": 12,
              "gf": 8,
              "ga": 3,
              "stages": {"GROUP_STAGE", "ROUND_OF_32", "ROUND_OF_16"},
              "won_final": False
          }, ...
        }
    """
    stats = {}

    def ensure(team):
        if team not in stats:
            stats[team] = {"matchPts": 0, "gf": 0, "ga": 0,
                           "stages": set(), "won_final": False}

    def apply_match(m):
        """Score a single confirmed match into stats."""
        stage  = m.get("stage", "")
        home   = normalise(m["homeTeam"]["name"])
        away   = normalise(m["awayTeam"]["name"])
        score  = m.get("score", {})
        ft     = score.get("fullTime", {})
        hg     = ft.get("home")
        ag     = ft.get("away")

        ensure(home)
        ensure(away)

        stats[home]["stages"].add(stage)
        stats[away]["stages"].add(stage)

        if hg is None or ag is None:
            return

        stats[home]["gf"] += hg
        stats[home]["ga"] += ag
        stats[away]["gf"] += ag
        stats[away]["ga"] += hg

        is_knockout = stage != "GROUP_STAGE"

        if hg > ag:
            stats[home]["matchPts"] += 3
        elif ag > hg:
            stats[away]["matchPts"] += 3
        else:
            if not is_knockout:
                stats[home]["matchPts"] += 1
                stats[away]["matchPts"] += 1
            winner = score.get("winner")
            if winner == "HOME_TEAM":
                stats[home]["matchPts"] += 3
            elif winner == "AWAY_TEAM":
                stats[away]["matchPts"] += 3

        if stage == "FINAL":
            winner = score.get("winner")
            if winner == "HOME_TEAM":
                stats[home]["won_final"] = True
            elif winner == "AWAY_TEAM":
                stats[away]["won_final"] = True

    # ── Phase 1: Apply locked confirmed results (permanent, never removed) ──
    for m in confirmed.values():
        apply_match(m)

    # ── Phase 2: Add GROUP_STAGE and LAST_32 appearances from API ───────────────
    # GROUP_STAGE: always safe to add from API.
    # LAST_32: safe to add if the fixture has REAL team names (not None) —
    #   this means the draw has been made and the team genuinely qualified.
    #   We skip placeholders (None names) to avoid premature bonuses.
    # LAST_16 and beyond: ONLY from confirmed results (Phase 1 above).
    for m in matches:
        stage = m.get("stage", "")
        if stage not in ("GROUP_STAGE", "LAST_32"):
            continue
        h = m["homeTeam"].get("name")
        a = m["awayTeam"].get("name")
        if not h or not a:
            continue  # skip placeholder fixtures with no team names yet
        home = normalise(h)
        away = normalise(a)
        ensure(home)
        ensure(away)
        stats[home]["stages"].add(stage)
        stats[away]["stages"].add(stage)
    return stats


def get_tournament_winner(matches: list) -> str:
    """Return the name of the team that won the Final."""
    for m in matches:
        if m.get("stage") == "FINAL" and m.get("status") == "FINISHED":
            winner = m.get("score", {}).get("winner")
            if winner == "HOME_TEAM":
                return normalise(m["homeTeam"]["name"])
            elif winner == "AWAY_TEAM":
                return normalise(m["awayTeam"]["name"])
    return ""


def get_total_goals(matches: list) -> int:
    """Return total goals scored across all finished matches."""
    total = 0
    for m in matches:
        if m.get("status") == "FINISHED":
            ft = m.get("score", {}).get("fullTime", {})
            total += (ft.get("home") or 0) + (ft.get("away") or 0)
    return total


def get_eliminated_teams(matches: list, confirmed: dict) -> list:
    """
    Returns teams that are definitively eliminated.

    Three sources:
      1. Lost a confirmed knockout match (LAST_32, LAST_16, QF, SF, Final)
      2. Finished 4th in their group (all 6 group matches confirmed)
      3. All 12 groups complete AND team not in any LAST_32 fixture with
         real team names — covers 3rd place teams that didn't qualify
    """
    KNOCKOUT_STAGES = {"LAST_32", "LAST_16", "QUARTER_FINALS",
                       "SEMI_FINALS", "FINAL", "THIRD_PLACE"}
    eliminated = set()

    # ── Rule 1: Lost a confirmed knockout match ───────────────────────
    for m in confirmed.values():
        if m.get("stage", "") not in KNOCKOUT_STAGES:
            continue
        score  = m.get("score", {})
        ft     = score.get("fullTime", {})
        hg, ag = ft.get("home"), ft.get("away")
        winner = score.get("winner")
        home   = normalise(m["homeTeam"]["name"])
        away   = normalise(m["awayTeam"]["name"])
        if hg is None or ag is None:
            continue
        if winner == "HOME_TEAM":
            eliminated.add(away)
        elif winner == "AWAY_TEAM":
            eliminated.add(home)

    # ── Rule 2 & 3: Group stage elimination ──────────────────────────
    groups = defaultdict(list)
    for m in confirmed.values():
        if m.get("stage") == "GROUP_STAGE" and m.get("group"):
            groups[m["group"]].append(m)

    all_groups_complete = len(groups) == 12 and all(
        len(ms) >= 6 for ms in groups.values()
    )

    group_complete_teams = set()

    for group_matches in groups.values():
        if len(group_matches) < 6:
            continue

        standings = defaultdict(lambda: {"pts": 0, "gd": 0, "gf": 0})
        for m in group_matches:
            h  = normalise(m["homeTeam"]["name"])
            a  = normalise(m["awayTeam"]["name"])
            ft = m["score"]["fullTime"]
            hg, ag = ft["home"], ft["away"]
            if hg is None or ag is None:
                continue
            group_complete_teams.add(h)
            group_complete_teams.add(a)
            standings[h]["gf"] += hg
            standings[h]["gd"] += hg - ag
            standings[a]["gf"] += ag
            standings[a]["gd"] += ag - hg
            w = m["score"].get("winner")
            if w == "HOME_TEAM":
                standings[h]["pts"] += 3
            elif w == "AWAY_TEAM":
                standings[a]["pts"] += 3
            else:
                standings[h]["pts"] += 1
                standings[a]["pts"] += 1

        ranked = sorted(standings.keys(),
                        key=lambda t: (standings[t]["pts"],
                                       standings[t]["gd"],
                                       standings[t]["gf"]),
                        reverse=True)
        # Rule 2: 4th place always eliminated
        if len(ranked) >= 4:
            eliminated.add(ranked[3])

    # Rule 3: Teams that finished group stage but not in any LAST_32 fixture
    # Only apply when ALL 12 groups are complete AND LAST_32 draw has started
    # (at least 1 fixture with real team names)
    if all_groups_complete:
        last32_teams = set()
        for m in list(matches) + list(confirmed.values()):
            if m.get("stage") == "LAST_32":
                h = m["homeTeam"].get("name")
                a = m["awayTeam"].get("name")
                if h: last32_teams.add(normalise(h))
                if a: last32_teams.add(normalise(a))

        if last32_teams:  # draw has been made
            for team in group_complete_teams:
                if team not in last32_teams and team not in eliminated:
                    eliminated.add(team)

    return sorted(eliminated)


def detect_current_stage(matches: list) -> str:
    """
    Determine the current tournament stage from match data.

    Logic:
      - No matches at all                    → PRE_TOURNAMENT
      - Highest stage with IN_PLAY matches   → that stage (live now)
      - No IN_PLAY, highest with SCHEDULED   → that stage (coming up)
      - All matches FINISHED                 → COMPLETE
    """
    if not matches:
        return "PRE_TOURNAMENT"

    in_play   = set()
    scheduled = set()
    finished  = set()

    for m in matches:
        stage  = m.get("stage", "")
        status = m.get("status", "")
        if stage not in STAGE_RANK:
            continue
        if status in ("IN_PLAY", "PAUSED"):
            in_play.add(stage)
        elif status in ("SCHEDULED", "TIMED"):
            scheduled.add(stage)
        elif status == "FINISHED":
            finished.add(stage)

    # Live match right now
    if in_play:
        return max(in_play, key=lambda s: STAGE_RANK[s])

    # Next upcoming stage
    if scheduled:
        return min(scheduled, key=lambda s: STAGE_RANK[s])

    # Everything done
    if finished and not scheduled and not in_play:
        return "COMPLETE"

    return "PRE_TOURNAMENT"


def calc_max_additional_per_team(team: str, matches: list, team_stats: dict) -> int:
    """
    Calculate the maximum additional points a team can still earn
    from remaining scheduled matches and future stage bonuses.
    """
    stats          = team_stats.get(team)
    stages_reached = stats["stages"] if stats else set()
    won_final      = stats["won_final"] if stats else False

    # Count remaining scheduled matches for this team
    remaining = sum(
        1 for m in matches
        if m.get("status") in ("SCHEDULED", "TIMED")
        and normalise(m["homeTeam"]["name"]) == team
        or m.get("status") in ("SCHEDULED", "TIMED")
        and normalise(m["awayTeam"]["name"]) == team
    )
    max_pts = remaining * 3

    # Stages that still have scheduled matches (stages not yet completed)
    stages_with_future = {
        m["stage"] for m in matches if m.get("status") in ("SCHEDULED", "TIMED")
    }

    # Add stage bonuses for stages not yet reached but still available
    for stage, bonus in STAGE_BONUS.items():
        if stage not in stages_reached and stage in stages_with_future:
            max_pts += bonus

    # Add winner bonus if the Final hasn't been played yet
    if not won_final and "FINAL" in stages_with_future:
        max_pts += WINNER_BONUS

    return max_pts


def calc_stage_points(team_stages: set, won_final: bool) -> int:
    """Calculate bonus points for stage progression."""
    pts = 0
    for stage, bonus in STAGE_BONUS.items():
        if stage in team_stages:
            pts += bonus
    if won_final:
        pts += WINNER_BONUS
    return pts


def calculate_participant_scores(team_stats: dict, matches: list) -> list:
    """Calculate total scores and max possible score for each participant."""
    results = []

    for p in PARTICIPANTS:
        total_match   = 0
        total_stage   = 0
        total_gf      = 0
        total_ga      = 0
        max_additional = 0

        seen = set()
        for team in p["teams"]:
            if team in seen:
                continue
            seen.add(team)

            s = team_stats.get(team)
            if s:
                total_match   += s["matchPts"]
                total_gf      += s["gf"]
                total_ga      += s["ga"]
                total_stage   += calc_stage_points(s["stages"], s["won_final"])

            # Always calculate max additional (works for unseen teams too)
            max_additional += calc_max_additional_per_team(team, matches, team_stats)

        current_total = total_match + total_stage
        results.append({
            "name":        p["name"],
            "matchPts":    total_match,
            "stagePts":    total_stage,
            "gf":          total_gf,
            "ga":          total_ga,
            "maxPossible": current_total + max_additional,
        })

    return results


def update_history(output: dict) -> None:
    """
    Append a snapshot to history.json whenever scores change.
    Each snapshot: { date, label, scores: [{name, total}] }
    The chart reads this file to draw the progression lines.
    """
    history_file = Path("history.json")
    history = json.loads(history_file.read_text()) if history_file.exists() else []

    current_scores = [
        {"name": p["name"], "total": p["matchPts"] + p["stagePts"]}
        for p in output["participants"]
    ]

    # Only append if scores changed since last snapshot
    if history and history[-1]["scores"] == current_scores:
        print(f"   📈 History unchanged — {len(history)} snapshot(s)")
        return

    snapshot = {
        "date":   output["lastUpdated"][:10],   # YYYY-MM-DD
        "label":  output["currentStage"]         # used as x-axis label in chart
                  .replace("⏳ ", "").replace("🟢 ", "").replace("⚽ ", "")
                  .replace("🏆 ", "").replace(" — In Progress", ""),
        "scores": current_scores,
    }
    history.append(snapshot)
    history_file.write_text(json.dumps(history, indent=2))
    print(f"   📈 History updated — {len(history)} snapshot(s)")


def main():
    print("⚽ Fetching WC 2026 match data...")
    try:
        matches = fetch_matches()
        print(f"   Found {len(matches)} matches")
    except requests.HTTPError as e:
        print(f"   ❌ API error: {e}")
        raise

    # Load locked confirmed results written by fetch_matches()
    results_file = Path("results.json")
    confirmed = json.loads(results_file.read_text()) if results_file.exists() else {}

    print("📊 Building team stats...")
    team_stats = build_team_stats(matches, confirmed)

    print("🗓️  Detecting current stage...")
    stage_key     = detect_current_stage(matches)
    current_stage = STAGE_DISPLAY.get(stage_key, "⏳ Tournament begins 11 June 2026")
    print(f"   Stage: {current_stage}")

    print("🏆 Detecting tournament winner & total goals...")
    actual_winner = get_tournament_winner(matches)
    actual_goals  = get_total_goals(matches)
    print(f"   Winner: {actual_winner or 'TBD'} · Total goals: {actual_goals}")

    print("🚫 Detecting eliminated teams...")
    eliminated = get_eliminated_teams(matches, confirmed)
    print(f"   Eliminated ({len(eliminated)}): {', '.join(eliminated) if eliminated else 'none yet'}")

    print("🧮 Calculating participant scores...")
    scores = calculate_participant_scores(team_stats, matches)

    output = {
        "lastUpdated":            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "currentStage":           current_stage,
        "actualTournamentWinner": actual_winner,
        "actualTotalGoals":       actual_goals,
        "eliminatedTeams":        eliminated,
        "participants":           scores
    }

    with open("scores.json", "w") as f:
        json.dump(output, f, indent=2)

    print("📈 Updating history...")
    update_history(output)

    print("✅ scores.json written:")
    for p in scores:
        total = p["matchPts"] + p["stagePts"]
        gd    = p["gf"] - p["ga"]
        print(f"   {p['name']:20s} | Match: {p['matchPts']:3d} | Stage: {p['stagePts']:3d} "
              f"| Total: {total:3d} | GD: {gd:+d}")


if __name__ == "__main__":
    main()
