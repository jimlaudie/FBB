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
    result = text
    for name in sorted(TEAM_NAMES, key=len, reverse=True):
        if not name:
            continue
        # Build a case-insensitive regex for the exact phrase
        pattern = re.compile(re.escape(name), re.IGNORECASE)
        result = pattern.sub(lambda m: f"<u>{m.group(0)}</u>", result)
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
    matchup_periods = [
        m.get("matchupPeriodId") for m in schedule if "matchupPeriodId" in m
    ]
    current_period = max(matchup_periods) if matchup_periods else None

    lines.append("")
    lines.append("Matchups:")
    has_matchups = False
    for matchup in schedule:
        if current_period and matchup.get("matchupPeriodId") != current_period:
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

        lines.append(
            "- {hname} {hscore} vs {aname} {ascore}".format(
                hname=home_name,
                hscore=home_score,
                aname=away_name,
                ascore=away_score,
            )
        )
        has_matchups = True

    if not has_matchups:
        lines.append("- No matchups this period")

    # Standings
    lines.append("")
    lines.append("Standings:")
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

    for tid, name, w, l, t in sorted(teams_list, key=lambda x: x[2], reverse=True):
        lines.append(
            "- {name}: {wins}-{losses}-{ties}".format(
                name=name, wins=w, losses=l, ties=t
            )
        )

    return "\n".join(lines)


def build_prompt(summary_text, mode):
    """Build Axios-style, nicely formatted fantasy newsletter prompt (plain text output)."""

    base_rules = [
        "Use Axios-style Smart Brevity: short sections, clear headings, tight bullets.",
        "Keep total length around 400–700 words.",
        "Tone: fun, witty, and clearly trash-talky (about {lvl}/10), but PG and friendly.".format(
            lvl=TRASH_TALK_LEVEL
        ),
        "No swearing.",
        "Focus on the league as a whole, not just the commissioner.",
        "You may occasionally poke fun at the commissioner, but do NOT make him the main character.",
        "Do NOT use any Markdown syntax: no asterisks for bold or italics, no '##' headings, no tables, no code blocks.",
        "Use simple plain text headings instead (for example: '1 big thing', 'Winners & losers', 'Team spotlights').",
        "Use blank lines to separate sections and bullets.",
    ]

    mode_rules = {
        "draft": [
            "This is a post-draft kickoff issue.",
            "Highlight draft steals, reaches, and overall vibes.",
            "Set expectations for the defending champ {champ} and a few key contenders.".format(
                champ=SHANE_TEAM_NAME
            ),
        ],
        "playoff": [
            "This is a playoff week.",
            "Lean into stakes, drama, and upsets.",
            "Highlight who is alive, who is out, and who is clinging to hope.",
        ],
        "finale": [
            "This is the season finale and wrap-up.",
            "Crown the champion, give a quick victory lap, and nod to heartbreaks.",
            "Include a short reflection on the season overall.",
        ],
        "weekly": [
            "This is a regular-season weekly recap.",
            "Focus on big swings, surprising scores, and shifts in the playoff picture.",
        ],
    }

    system_msg = (
        "You are writing an Axios-style fantasy baseball newsletter for a home ESPN "
        "head-to-head points league. Use Smart Brevity: short sections and scannable bullets. "
        "Be witty and lightly trash-talky, but keep it PG and fun."
    )

    mode_list = mode_rules.get(mode, mode_rules["weekly"])

    user_msg = (
        "Here is compact league data for this week:\n\n"
        "{summary}\n\n"
        "Voice & style rules:\n"
        "{rules}\n\n"
        "Write the newsletter in PLAIN TEXT ONLY (no Markdown, no asterisks, no '##', no tables, no emojis).\n"
        "Use blank lines to separate sections.\n\n"
        "Use this structure (you can tweak titles slightly but keep the spirit):\n"
        "1 big thing\n"
        "Why it matters: 1–2 sentences on the main storyline.\n"
        "- A couple of sharp bullets with key details.\n\n"
        "Winners & losers\n"
        "Short sections calling out a few teams that crushed it and a few that face-planted. "
        "1–2 sentences per bullet, with fun trash talk.\n\n"
        "Team spotlights\n"
        "Pick a handful of notable teams (not necessarily all 13). For each:\n"
        "- Start with the team name on its own line.\n"
        "- Give 2–3 sentences mixing performance, narrative, and a tiny bit of strategy or advice.\n\n"
        "Standings snapshot\n"
        "Summarize how the standings shifted. You can reference tiers (contenders, middle, basement) "
        "instead of listing every record.\n\n"
        "What’s next\n"
        "A short look ahead: key matchups, interesting storylines, or waiver-wire angles.\n\n"
        "Constraints:\n"
        "- Keep it tight and scannable.\n"
        "- Do NOT over-focus on the commissioner/league manager; he can be mentioned once in passing at most.\n"
        "- No profanity.\n"
        "- No emojis.\n"
    ).format(
        summary=summary_text,
        rules="\n".join("- {r}".format(r=r) for r in base_rules + mode_list),
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

    subject = "{prefix} {title}".format(
        prefix=SUBJECT_PREFIX, title=subject_map.get(mode, "Weekly Roundup")
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
    rows = []
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
        rows.append((name, wins or 0, losses or 0, ties or 0))

    # Sort by wins desc, then losses asc, then name
    rows.sort(key=lambda r: (-r[1], r[2], r[0]))

    html = "<h3 style='margin-top:24px;margin-bottom:8px;'>Standings</h3>"
    html += "<table style='border-collapse:collapse;width:100%;max-width:600px;font-size:14px;'>"
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
    """Build an HTML table of last week's scores (if available)."""
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
        for matchup in league.scoreboard():
            home = matchup.home_team
            away = matchup.away_team
            home_name = getattr(home, "team_name", "Home")
            away_name = getattr(away, "team_name", "Away")
            hs = getattr(matchup, "home_score", 0.0)
            as_ = getattr(matchup, "away_score", 0.0)
            label = f"{home_name} vs {away_name}"
            # no underline_team_names here; keep table clean
            score = f"{hs:.1f} – {as_:.1f}"
            html += (
                "<tr>"
                f"<td style='border-bottom:1px solid #eee;padding:4px;'>{label}</td>"
                f"<td style='border-bottom:1px solid #eee;padding:4px;'>{score}</td>"
                "</tr>"
            )
            has_row = True
    except Exception:
        pass

    if not has_row:
        html += (
            "<tr>"
            "<td colspan='2' style='padding:4px;'>Scores not available for this period.</td>"
            "</tr>"
        )

    html += "</table>"
    return html


def get_last_week_league(base_league):
    """Return a League object set to the previous scoring period, if possible."""
    try:
        current_period = base_league.scoringPeriodId
    except AttributeError:
        return base_league  # fallback if attribute not present

    last_period = max(1, current_period - 1)
    if last_period == current_period:
        return base_league

        return League(
        league_id=LEAGUE_ID,
        year=SEASON_ID,
        swid=SWID,
        espn_s2=ESPN_S2,
        scoringPeriod=last_period,   # <- use scoringPeriod, not scoringPeriodId
    )



def main():
    """Main execution."""
    today = today_mdt()

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
            "1 big thing",
            "winners & losers",
            "team spotlights",
            "standings snapshot",
            "what’s next",
            "whats next",
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
            if current_section == "team spotlights":
                text = underline_team_names(text)
            html += "<li>{text}</li>".format(text=text)

        # Normal paragraphs
        else:
            if in_ul:
                html += "</ul>"
                in_ul = False
            text = line.strip()
            if current_section == "team spotlights":
                text = underline_team_names(text)
            html += "<p style='margin:4px 0;'>{text}</p>".format(text=text)

    if in_ul:
        html += "</ul>"

    # Append visual tables for standings and last week's scores
    html += build_standings_table(league)

    last_week_league = get_last_week_league(league)
    html += build_matchups_table(last_week_league)

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

