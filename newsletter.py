import os
import json
import smtplib
import ssl
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from openai import OpenAI
from espn_api.baseball import League

# Load config
with open("config.json", "r") as f:
    CONFIG = json.load(f)

LEAGUE_ID = CONFIG["league"]["league_id"]
SEASON_ID = CONFIG["league"]["season_id"]

FROM_ADDRESS = CONFIG["email"]["from_address"]
SUBJECT_PREFIX = CONFIG["email"]["subject_prefix"]
TEST_MODE = CONFIG["email"].get("test_mode", False)
TEST_RECIPIENT = CONFIG["email"].get("test_recipient", FROM_ADDRESS)

TRASH_TALK_LEVEL = CONFIG["style"]["trash_talk_level"]
NO_SWEARING = CONFIG["style"]["no_swearing"]
SHANE_TEAM_NAME = CONFIG["style"]["shane_team_name"]
JIM_TEAM_NAME = CONFIG["style"]["jim_team_name"]

SCHEDULE = CONFIG["schedule"]
TEAMS = CONFIG["teams"]

import re

TEAM_NAMES = [t["team_name"] for t in TEAMS]

def underline_team_names(text: str) -> str:
    """Wrap known team names in <u>...</u> (case-insensitive)."""

    # Normalize curly apostrophes to straight apostrophes
    normalized_text = text.replace("’", "'")

    result = normalized_text

    for name in sorted(TEAM_NAMES, key=len, reverse=True):

        if not name:
            continue

        normalized_name = name.replace("’", "'")

        pattern = re.compile(
            re.escape(normalized_name),
            re.IGNORECASE
        )

        result = pattern.sub(
            lambda m: f"<u>{m.group(0)}</u>",
            result
        )

    return result


# Secrets from GitHub Actions
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["SWID"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

client = OpenAI(api_key=OPENAI_API_KEY)


def parse_ymd(s):
    """Parse YYYY-MM-DD string to date object."""
    return date.fromisoformat(s)


def today_mdt():
    """Get today's date (MDT)."""
    return date.today()

def effective_today():
    """Allow GitHub Actions manual override date."""
    override = os.environ.get("TEST_DATE")

    if override:
        print(f"Using TEST_DATE override: {override}")
        return parse_ymd(override)

    return today_mdt()

def newsletter_mode_for_today(today):
    """Determine if today is a scheduled newsletter day and what type."""
    if today == parse_ymd(SCHEDULE["draft_issue_date"]):
        return "draft"
    if today == parse_ymd(SCHEDULE["finale_date"]):
        return "finale"
    if SCHEDULE.get("skip_dates") and today.isoformat() in SCHEDULE["skip_dates"]:
        return None
    if SCHEDULE.get("playoff_dates") and today.isoformat() in SCHEDULE["playoff_dates"]:
        return "playoff"
    if today >= parse_ymd(SCHEDULE["start_weekly_date"]) and today.weekday() == 0:
        return "weekly"
    return None


def get_league():
    """Create League object via espn-api."""
    return League(
        league_id=LEAGUE_ID,
        year=SEASON_ID,
        swid=SWID,
        espn_s2=ESPN_S2,
    )


def build_team_lookups(league):
    """Build ESPN and config team lookups."""
    espn_lookup = {}
    for team in league.teams:
        espn_lookup[team.team_id] = {
            "name": getattr(team, "team_name", "Team {tid}".format(tid=team.team_id)),
            "abbrev": getattr(team, "team_abbrev", ""),
        }

    config_lookup = {}
    for t in TEAMS:
        config_lookup[t["team_id"]] = t

    return espn_lookup, config_lookup

def extract_team_storylines(team, target_period):
    """
    Pull standout hitters and pitchers from a fantasy team.
    Returns a compact narrative-friendly summary.
    """

    hitters = []
    pitchers = []

    roster = getattr(team, "roster", [])

    for player in roster:

        lineup_slot = getattr(player, "lineupSlot", "")

        # Ignore bench/IL players
        if lineup_slot in {"BE", "IL"}:
            continue

        stats = getattr(player, "stats", {})

        weekly_points = 0.0

        period_stats = stats.get(target_period, {})

        if isinstance(period_stats, dict):
            weekly_points = period_stats.get("points", 0.0)

        position = getattr(player, "position", "")

        player_info = {
            "name": getattr(player, "name", "Unknown"),
            "points": round(weekly_points, 1),
            "position": position,
        }

        if position in {"SP", "RP"}:
            pitchers.append(player_info)
        else:
            hitters.append(player_info)

    hitters.sort(key=lambda x: x["points"], reverse=True)
    pitchers.sort(key=lambda x: x["points"], reverse=True)

    top_hitter = hitters[0] if hitters else None
    top_pitcher = pitchers[0] if pitchers else None

    return {
        "top_hitter": top_hitter,
        "top_pitcher": top_pitcher,
    }

def build_summary(league, mode):
    """Build compact data summary for LLM."""
    espn_lookup, config_lookup = build_team_lookups(league)
    data = league._fetch_league()
    schedule = data.get("schedule", [])
    
    lines = []

    # Debug team mapping
    lines.append("Teams in league (from ESPN):")
    for tid, info in espn_lookup.items():
        cfg = config_lookup.get(tid)
        cfg_name = cfg["team_name"] if cfg else "MISSING_FROM_CONFIG"
        lines.append(
            "- ID {tid}: '{ename}' -> config '{cfgname}'".format(
                tid=tid, ename=info["name"], cfgname=cfg_name
            )
        )

    # Mode context
    lines.append("")
    lines.append("Mode: {mode}".format(mode=mode))
    if mode == "draft":
        lines.append("- Post-draft kickoff issue.")
    elif mode == "playoff":
        lines.append("- Playoff bracket week.")
    elif mode == "finale":
        lines.append("- Championship + season wrap.")

    # Current matchups
    current_period = league.currentMatchupPeriod
    target_period = max(1, current_period - 1)
    box_scores = league.box_scores(matchup_period=target_period)

    print(f"Summary using matchup period: {target_period}")

    lines.append("")
    lines.append("Matchups:")
    has_matchups = False
    weekly_results = []
    for matchup in schedule:
        if matchup.get("matchupPeriodId") != target_period:
            continue
        home = matchup.get("home")
        away = matchup.get("away")
        if not (home and away):
            continue

        home_id = home.get("teamId")
        away_id = away.get("teamId")
        home_score = home.get("totalPoints", 0)
        away_score = away.get("totalPoints", 0)

        home_name = espn_lookup.get(home_id, {}).get(
            "name", "Team {tid}".format(tid=home_id)
        )
        away_name = espn_lookup.get(away_id, {}).get(
            "name", "Team {tid}".format(tid=away_id)
        )

        margin = round(abs(home_score - away_score), 1)

        winner = home_name if home_score > away_score else away_name

        loser = away_name if home_score > away_score else home_name
        winner_score = max(home_score, away_score)
        loser_score = min(home_score, away_score)

        lines.append(
            "- Final result: {winner} defeated {loser} by exactly {margin:.1f} points. "
            "Final score: {winner_score:.1f} to {loser_score:.1f}. "
            "{winner_score:.1f} is the winner's team score, NOT the margin of victory.".format(
                winner=winner,
                loser=loser,
                margin=margin,
                winner_score=winner_score,
                loser_score=loser_score,
            )
        )

        if margin < 15:
            lines.append("  - This matchup was extremely close.")

        elif margin > 80:
            lines.append("  - This was a major blowout.")

        try:

            for box in box_scores:

                box_home = box.home_team
                box_away = box.away_team

                box_home_name = getattr(box_home, "team_name", "")
                box_away_name = getattr(box_away, "team_name", "")

                if (
                    box_home_name == home_name
                    and box_away_name == away_name
                ):

                    lines.append("  Key performances:")

                    home_story = extract_team_storylines(box_home, target_period)
                    away_story = extract_team_storylines(box_away, target_period)

                    if home_story["top_hitter"]:

                        hitter = home_story["top_hitter"]

                        lines.append(
                            f"  - {home_name} top hitter: "
                            f"{hitter['name']} ({hitter['points']} pts)"
                        )

                    if home_story["top_pitcher"]:

                        pitcher = home_story["top_pitcher"]

                        lines.append(
                            f"  - {home_name} top pitcher: "
                            f"{pitcher['name']} ({pitcher['points']} pts)"
                        )

                    if away_story["top_hitter"]:

                        hitter = away_story["top_hitter"]

                        lines.append(
                            f"  - {away_name} top hitter: "
                            f"{hitter['name']} ({hitter['points']} pts)"
                        )

                    if away_story["top_pitcher"]:

                        pitcher = away_story["top_pitcher"]

                        lines.append(
                            f"  - {away_name} top pitcher: "
                            f"{pitcher['name']} ({pitcher['points']} pts)"
                        )

        except Exception as e:

            print("Could not build player storylines:", e)

        
        margin = abs(home_score - away_score)

        winner_name = home_name if home_score > away_score else away_name
        loser_name = away_name if home_score > away_score else home_name

        winner_score = max(home_score, away_score)
        loser_score = min(home_score, away_score)

        weekly_results.append({
            "winner": winner_name,
            "loser": loser_name,
            "winner_score": winner_score,
            "loser_score": loser_score,
            "margin": margin,
        })

        has_matchups = True

    if not has_matchups:
        lines.append("- No matchups this period")

    team_records = {}
    teams_list = []
    for team in league.teams:
        tid = getattr(team, "team_id", None)
        name = getattr(team, "team_name", "Team {tid}".format(tid=tid))

        wins_val = getattr(team, "wins", 0)
        losses_val = getattr(team, "losses", 0)
        ties_val = getattr(team, "ties", 0)

        wins = wins_val if wins_val is not None else 0
        losses = losses_val if losses_val is not None else 0
        ties = ties_val if ties_val is not None else 0

        if not wins and not losses:
            rec = getattr(team, "record", {})
            wins = rec.get("wins", 0)
            losses = rec.get("losses", 0)
            ties = rec.get("ties", 0)

        teams_list.append((tid or 0, name, wins, losses, ties))
        streak_type = getattr(team, "streak_type", "")
        streak_length = getattr(team, "streak_length", 0)

        team_records[name] = {
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "streak_type": streak_type,
            "streak_length": streak_length,
        }

    
    if weekly_results:
        highest = max(weekly_results, key=lambda x: x["winner_score"])
        closest = min(weekly_results, key=lambda x: x["margin"])
        blowout = max(weekly_results, key=lambda x: x["margin"])
        first_win_teams = []

        for result in weekly_results:
            winner = result["winner"]

            rec = team_records.get(winner)

            if rec and rec["wins"] == 1:
                first_win_teams.append(winner)

        lines.append("")
        lines.append("Weekly superlatives:")
        lines.append(
            f"- Highest score: {highest['winner']} ({highest['winner_score']:.1f})"
        )
        lines.append(
            f"- Closest matchup: {closest['winner']} beat "
            f"{closest['loser']} by {closest['margin']:.1f}"
        )
        lines.append(
            f"- Largest margin of victory: {blowout['winner']} defeated "
            f"{blowout['loser']} by exactly {blowout['margin']:.1f} points. "
            f"Final score: {blowout['winner_score']:.1f} to "
            f"{blowout['loser_score']:.1f}."
        )
        for team in first_win_teams:
            lines.append(
                f"- Milestone: {team} earned their first win of the season."
            )

        winning_streaks = []
        losing_streaks = []

        for name, rec in team_records.items():

            streak_type = rec.get("streak_type", "")
            streak_length = rec.get("streak_length", 0)

            if streak_type == "W" and streak_length >= 2:
                winning_streaks.append((name, streak_length))

            elif streak_type == "L" and streak_length >= 2:
                losing_streaks.append((name, streak_length))

        if winning_streaks:
            hottest = max(winning_streaks, key=lambda x: x[1])

            lines.append(
                f"- Hottest team: {hottest[0]} riding a W{hottest[1]} streak."
            )

        if losing_streaks:
            coldest = max(losing_streaks, key=lambda x: x[1])

            lines.append(
                f"- Cold streak: {coldest[0]} stuck on an L{coldest[1]} skid."
            )

        # Standings
        lines.append("")
        lines.append("Standings:")
    
    for tid, name, w, l, t in sorted(teams_list, key=lambda x: x[2], reverse=True):
        lines.append(
            "- {name}: {wins}-{losses}-{ties}".format(
                name=name, wins=w, losses=l, ties=t
            )
        )

    return "\n".join(lines)


def build_prompt(summary_text, mode):
    """Build a short, funny, league-wide fantasy baseball recap."""

    mode_rules = {
        "draft": [
            "This is the post-draft kickoff issue.",
            "Mention a few draft steals, reaches, and questionable life choices.",
        ],
        "playoff": [
            "This is a playoff issue.",
            "Emphasize pressure, elimination danger, upsets, and heartbreak.",
        ],
        "finale": [
            "This is the championship and season-wrap issue.",
            "Crown the champion and briefly recognize the season's best disasters.",
        ],
        "weekly": [
            "This is a regular-season weekly recap.",
            "Focus on what actually happened during the completed matchup week.",
        ],
    }

    rules = [
        "Write a short, funny fantasy baseball league recap.",
        "Target approximately 250–400 words.",
        "Keep the tone playful, mildly ruthless, PG, and friendly.",
        "Use only facts contained in the supplied league data.",
        "Spread attention across the league rather than repeating one team throughout.",
        "Do not feature the same team in more than two major sections unless absolutely necessary.",
        "If one team is the main story, choose different teams for the spotlight section.",
        "Celebrate meaningful moments such as a first win, an upset, or a streak ending.",
        "Mention specific players only when the supplied data clearly identifies them.",
        "Do not invent player statistics, roster weaknesses, transactions, injuries, or waiver options.",
        "Do not give generic advice such as 'work the waiver wire' unless supported by supplied facts.",
        "Do not repeat the standings in prose because the standings table appears below.",
        "Be numerically exact.",
        "A team's final score is not its margin of victory.",
        "Never describe a team score as a 'point win.'",
        "Only say 'won by X points' when X is the explicitly supplied margin of victory.",
        "Example: a 356–167 final score is a 189-point win, not a 356-point win.",
        "Do not use Markdown, tables, emojis, asterisks, or numbered lists.",
        "Use plain-text headings exactly as requested.",
        "Across the opening, awards, and team discussion sections, try to mention at least four different teams.",
        "Do not select the main-story team as one of the two team discussions unless another team lacks a meaningful storyline.",
    ]

    rules.extend(mode_rules.get(mode, mode_rules["weekly"]))

    system_msg = (
        "You write a funny, concise recap for a private ESPN fantasy baseball league. "
        "You are careful with numerical facts and never confuse a team's final score "
        "with its margin of victory."
    )

    user_msg = (
        "Completed-week league data:\n\n"
        "{summary}\n\n"
        "Rules:\n"
        "{rules}\n\n"
        "Write the recap using exactly this structure:\n\n"
        "The week in one sentence\n"
        "One funny sentence capturing the week's biggest development.\n\n"
        "Awards nobody asked for\n"
        "Use 3 or 4 short bullets. Draw from the supplied highest score, closest game, "
        "largest margin, first win, streaks, or another factual moment. "
        "Do not repeat the same team in every bullet.\n\n"
        "Two teams worth talking about\n"
        "Choose exactly two teams. Prefer teams not already heavily featured above. "
        "Give each team one short paragraph of 1–2 sentences.\n\n"
        "Next week’s nonsense\n"
        "Give one short sentence looking ahead. Do not invent specific matchups unless "
        "they are supplied in the data.\n\n"
        "Keep the entire recap short. The scores and division standings will be added "
        "automatically beneath the recap."
    ).format(
        summary=summary_text,
        rules="\n".join(f"- {rule}" for rule in rules),
    )

    return system_msg, user_msg

def generate_newsletter(summary_text, mode):
    """Generate newsletter via OpenAI."""
    system_msg, user_msg = build_prompt(summary_text, mode)

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.7,
        max_tokens=2000,
    )

    return response.choices[0].message.content


def send_email(newsletter_html, recipients, mode):
    """Send HTML email."""
    subject_map = {
        "draft": "Draft Night Special",
        "weekly": "Weekly Beatdown",
        "playoff": "Playoff Bloodbath",
        "finale": "Championship Glory",
    }

    test_tag = "[TEST] " if TEST_MODE else ""
    subject = "{test}{prefix} {title}".format(
        test=test_tag, prefix=SUBJECT_PREFIX, title=subject_map.get(mode, "Weekly Roundup")
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDRESS
    msg["To"] = FROM_ADDRESS  # header only; routing via recipients

    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)

    print(
        "About to send to {n} recipients: {recipients}".format(
            n=len(recipients), recipients=recipients
        )
    )
    print(
        "From: {from_addr}, Subject: {subject}".format(
            from_addr=FROM_ADDRESS, subject=subject
        )
    )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        print("Connecting to SMTP_SSL (smtp.gmail.com:465)...")
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        print("Logged in to Gmail SMTP")
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())
        print("Email sent via SMTP_SSL (no explicit error)")


def build_standings_table(league):
    """Build an HTML standings table."""
    divisions = {}
    for team in league.teams:
        name = getattr(
            team, "team_name", "Team {tid}".format(tid=getattr(team, "team_id", 0))
        )
        # Try wins/losses/ties attributes; fall back to record dict if needed
        wins = getattr(team, "wins", None)
        losses = getattr(team, "losses", None)
        ties = getattr(team, "ties", None)
        if wins is None or losses is None:
            rec = getattr(team, "record", {})
            wins = rec.get("wins", 0)
            losses = rec.get("losses", 0)
            ties = rec.get("ties", 0)
        division_id = getattr(team, "division_id", 0)

        if division_id not in divisions:
            divisions[division_id] = []

        divisions[division_id].append(
            (name, wins or 0, losses or 0, ties or 0)
        )

    # Sort by wins desc, then losses asc, then name
    html = "<h3 style='margin-top:24px;margin-bottom:8px;'>Division Standings</h3>"

    division_names = {
        0: "A",
        1: "B",
    }
    
    for division_id, rows in sorted(divisions.items()):

        rows.sort(key=lambda r: (-r[1], r[2], r[0]))

        html += (
            f"<h4 style='margin-top:16px;margin-bottom:6px;'>"
            f"Division {division_names.get(division_id, division_id)}"
            f"</h4>"
        )

        html += (
            "<table style='border-collapse:collapse;width:100%;"
            "max-width:600px;font-size:14px;margin-bottom:12px;'>"
        )

        html += (
            "<tr>"
            "<th style='border-bottom:1px solid #ccc;text-align:left;padding:4px;'>Team</th>"
            "<th style='border-bottom:1px solid #ccc;text-align:left;padding:4px;'>W-L-T</th>"
            "</tr>"
        )

        for name, w, l, t in rows:

            rec_str = f"{w}-{l}-{t}"

            html += (
                "<tr>"
                f"<td style='border-bottom:1px solid #eee;padding:4px;'>{name}</td>"
                f"<td style='border-bottom:1px solid #eee;padding:4px;'>{rec_str}</td>"
                "</tr>"
            )

        html += "</table>"

    return html


def build_matchups_table(league):
    """Build an HTML table of last week's scores."""
    html = "<h3 style='margin-top:24px;margin-bottom:8px;'>Last week&apos;s scores</h3>"
    html += "<table style='border-collapse:collapse;width:100%;max-width:600px;font-size:14px;'>"
    html += (
        "<tr>"
        "<th style='border-bottom:1px solid #ccc;text-align:left;padding:4px;'>Matchup</th>"
        "<th style='border-bottom:1px solid #ccc;text-align:left;padding:4px;'>Score</th>"
        "</tr>"
    )

    has_row = False

    try:
        current_period = league.currentMatchupPeriod
        last_period = max(1, current_period - 1)

        print(f"Current matchup period: {current_period}")
        print(f"Fetching box scores for week {last_period}")

        for matchup in league.box_scores(matchup_period=last_period):

            print(f"Retrieved matchup period: {last_period}")
            
            home = matchup.home_team
            away = matchup.away_team

            home_name = getattr(home, "team_name", "Home")
            away_name = getattr(away, "team_name", "Away")

            hs = getattr(matchup, "home_score", 0.0)
            as_ = getattr(matchup, "away_score", 0.0)

            print(
                f"Matchup found: {home_name} {hs:.1f} vs {away_name} {as_:.1f}"
            )

            label = f"{home_name} vs {away_name}"
            score = f"{hs:.1f} – {as_:.1f}"

            html += (
                "<tr>"
                f"<td style='border-bottom:1px solid #eee;padding:4px;'>{label}</td>"
                f"<td style='border-bottom:1px solid #eee;padding:4px;'>{score}</td>"
                "</tr>"
            )

            has_row = True

    except Exception as e:
        print(f"Error fetching previous week scores: {e}")

    if not has_row:
        html += (
            "<tr>"
            "<td colspan='2' style='padding:4px;'>Scores not available for this period.</td>"
            "</tr>"
        )

    html += "</table>"

    return html


def main():
    """Main execution."""
    today = effective_today()

    mode = newsletter_mode_for_today(today)
    if mode is None:
        print(
            "No newsletter today ({today}). Next: check schedule.".format(today=today)
        )
        return

    recipients = [TEST_RECIPIENT] if TEST_MODE else [t["email"] for t in TEAMS]

    print(
        "Sending {mode} newsletter for {today} to: {recipients}".format(
            mode=mode, today=today, recipients=recipients
        )
    )

    league = get_league()
    summary = build_summary(league, mode)
    newsletter_text = generate_newsletter(summary, mode)

    # Base HTML wrapper
    html = (
        "<html><body style='font-family:Arial,sans-serif;max-width:650px;"
        "margin:0 auto;padding:20px;line-height:1.5;font-size:15px;color:#222;'>"
    )

    # Turn plain text sections into HTML paragraphs and lists
    in_ul = False
    current_section = None

    for line in newsletter_text.splitlines():
        line = line.rstrip()

        if not line.strip():
            if in_ul:
                html += "</ul>"
                in_ul = False
            html += "<br>"
            continue

        heading_candidates = [
            "the week in one sentence",
            "awards nobody asked for",
            "two teams worth talking about",
            "next week’s nonsense",
            "next week's nonsense",
        ]

        # Headings
        if (
            not line.startswith("- ")
            and len(line.strip()) <= 40
            and line.strip().lower() in heading_candidates
        ):
            if in_ul:
                html += "</ul>"
                in_ul = False

            heading_text = line.strip().lower()
            current_section = heading_text

            if heading_text in ["whats next", "what’s next"]:
                display = "What’s next"
            else:
                display = heading_text.title()

            html += "<h2 style='color:#333;margin-top:18px;margin-bottom:6px;'>{text}</h2>".format(
                text=display
            )

        # Bullets
        elif line.lstrip().startswith("- "):
            if not in_ul:
                html += "<ul style='padding-left:20px;margin-top:4px;margin-bottom:4px;'>"
                in_ul = True
            text = line.lstrip()[2:].strip()
            underline_sections = {
                "the week in one sentence",
                "awards nobody asked for",
                "two teams worth talking about",
                "next week’s nonsense",
                "next week's nonsense",
            }

            if current_section in underline_sections:
                text = underline_team_names(text)
            html += "<li>{text}</li>".format(text=text)

        # Normal paragraphs
        else:
            if in_ul:
                html += "</ul>"
                in_ul = False
            text = line.strip()
            underline_sections = {
                "the week in one sentence",
                "awards nobody asked for",
                "two teams worth talking about",
                "next week’s nonsense",
                "next week's nonsense",
            }

            if current_section in underline_sections:
                text = underline_team_names(text)
            html += "<p style='margin:4px 0;'>{text}</p>".format(text=text)

    if in_ul:
        html += "</ul>"

    # Append visual tables for standings and last week's scores
    html += build_standings_table(league)

    html += build_matchups_table(league)

    html += "</body></html>"

    send_email(html, recipients, mode)

    print(
        "Newsletter sent to {n} recipient(s) ({mode} mode)".format(
            n=len(recipients), mode=mode
        )
    )
    print("First 300 chars of HTML body (for debugging):")
    print(html[:300])


if __name__ == "__main__":
    main()

