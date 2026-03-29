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
TEST_MODE = CONFIG["email"].get("test_mode", True)
TEST_RECIPIENT = CONFIG["email"].get("test_recipient", FROM_ADDRESS)

TRASH_TALK_LEVEL = CONFIG["style"]["trash_talk_level"]
NO_SWEARING = CONFIG["style"]["no_swearing"]
SHANE_TEAM_NAME = CONFIG["style"]["shane_team_name"]
JIM_TEAM_NAME = CONFIG["style"]["jim_team_name"]

SCHEDULE = CONFIG["schedule"]
TEAMS = CONFIG["teams"]

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
        espn_s2=ESPN_S2
    )


def build_team_lookups(league):
    """Build ESPN and config team lookups."""
    espn_lookup = {}
    for team in league.teams:
        espn_lookup[team.team_id] = {
            "name": getattr(team, "team_name", "Team {tid}".format(tid=team.team_id)),
            "abbrev": getattr(team, "team_abbrev", "")
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
        lines.append("- ID {tid}: '{ename}' → config '{cfgname}'".format(
            tid=tid,
            ename=info["name"],
            cfgname=cfg_name
        ))

    # Mode context
    lines.append("\nMode: {mode}".format(mode=mode))
    if mode == "draft":
        lines.append("- Post-draft kickoff issue")
    elif mode == "playoff":
        lines.append("- Playoff bracket week")
    elif mode == "finale":
        lines.append("- Championship + season wrap")

    # Current matchups
    matchup_periods = [m.get("matchupPeriodId") for m in schedule if "matchupPeriodId" in m]
    current_period = max(matchup_periods) if matchup_periods else None

    lines.append("\nMatchups:")
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

        home_name = espn_lookup.get(home_id, {}).get("name", "Team {tid}".format(tid=home_id))
        away_name = espn_lookup.get(away_id, {}).get("name", "Team {tid}".format(tid=away_id))

        lines.append("- {hname} {hscore} vs {aname} {ascore}".format(
            hname=home_name,
            hscore=home_score,
            aname=away_name,
            ascore=away_score
        ))
        has_matchups = True

    if not has_matchups:
        lines.append("- No matchups this period")

    # Standings
    lines.append("\nStandings:")
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
        lines.append("- {name}: {wins}-{losses}-{ties}".format(
            name=name,
            wins=w,
            losses=l,
            ties=t
        ))

    return "\n".join(lines)


def build_prompt(summary_text, mode):
    """Build Axios-style newsletter prompt."""
    base_rules = [
        "Trash talk: {ttl}/10 - sharp but PG.".format(ttl=TRASH_TALK_LEVEL),
        "No swearing. Funny, confident, snarky, never mean.",
        "Call out {shane} as defending champ.".format(shane=SHANE_TEAM_NAME),
        "Roast {jim} harder (league manager).".format(jim=JIM_TEAM_NAME),
        "Write like Axios: crisp, scannable, bold headers.",
        "Short paragraphs. 1–2 sentences max per idea.",
        "Team spotlights blend: performance + snark + 1 tip. No labels.",
        "Structure: league news → team spotlights → close."
    ]

    mode_rules = {
        "draft": [
            "Post-draft kickoff. Hype the new season.",
            "Rank draft winners/losers. Call reaches.",
            "Set expectations for the defending champ."
        ],
        "playoff": [
            "Playoff stakes are real now.",
            "Who's alive? Who's consolation?",
            "Every move matters."
        ],
        "finale": [
            "Championship decided. Season awards.",
            "Biggest surprise, bust, waiver gem.",
            "Full-year victory lap."
        ],
        "weekly": [
            "Regular week. Recaps + forward look."
        ]
    }

    system_msg = (
        "You're a sharp fantasy baseball newsletter editor. "
        "Write like Axios: punchy, scannable, brutally honest but fun. "
        "Smart Brevity™ style - bold headers, tight prose."
    )

    mode_list = mode_rules.get(mode, mode_rules["weekly"])
    user_msg = (
        "League data:\n\n"
        "{summary}\n\n"
        "Voice & rules:\n"
        "{rules}\n\n"
        "Write a newsletter in **markdown**. Use:\n"
        "- ## Headers\n"
        "- **Bold phrases**\n"
        "- Short paragraphs\n"
        "- - Bullets for scannability\n\n"
        "Format like this:\n"
        "## 1 Big Thing\n"
        "**Why it matters:** One sentence.\n"
        "- Key detail 1\n"
        "- Key detail 2\n\n"
        "## Team Spotlights\n"
        "**{{Team Name}}**  \n"
        "Natural flow of performance, snark, tip.\n\n"
        "## The Big Board\n"
        "Standings snapshot.\n\n"
        "## What's Next\n"
        "Forward look."
    ).format(
        summary=summary_text,
        rules="\n".join("- {r}".format(r=r) for r in base_rules + mode_list)
    )

    return system_msg, user_msg


def generate_newsletter(summary_text, mode):
    """Generate newsletter via OpenAI."""
    system_msg, user_msg = build_prompt(summary_text, mode)

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ],
        temperature=0.7,
        max_tokens=2000
    )

    return response.choices[0].message.content


def send_email(newsletter_html, recipients, mode):
    """Send HTML email."""
    subject_map = {
        "draft": "Draft Night Special",
        "weekly": "Weekly Beatdown",
        "playoff": "Playoff Bloodbath",
        "finale": "Championship Glory"
    }

    subject = "{prefix} {title}".format(
        prefix=SUBJECT_PREFIX,
        title=subject_map.get(mode, "Weekly Roundup")
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDRESS
    msg["To"] = FROM_ADDRESS  # header only; routing via `recipients` list

    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)

    print(f"About to send to {len(recipients)} recipients: {recipients}")
    print(f"From: {FROM_ADDRESS}, Subject: {subject}")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        print("Connecting to SMTP_SSL (smtp.gmail.com:465)...")
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        print("Logged in to Gmail SMTP")
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())
        print("Email sent via SMTP_SSL (no explicit error)")



def main():
    """Main execution."""
    today = today_mdt()  # or temporarily override it:

    # ----------- TEMPORARY TEST OVERRIDE (uncomment to test) -----------
    # today = date(2026, 4, 6)  # pick any Monday you like
    # ----------- END OVERRIDE -----------

    # Completely ignore schedule logic for testing
    mode = "weekly"

    # DEBUG: show who we plan to email
    recipients = [TEST_RECIPIENT] if TEST_MODE else [t["email"] for t in TEAMS]
    print("TEST MODE: sending {mode} newsletter to: {recipients}".format(
        mode=mode, recipients=recipients
    ))

    league = get_league()
    summary = build_summary(league, mode)
    newsletter_md = generate_newsletter(summary, mode)

    # Simple markdown → HTML
    html = (
        "<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;'>"
    )
    for line in newsletter_md.split("\\n"):
        line = line.strip()
        if not line:
            html += "<br>"
        elif line.startswith("## "):
            html += "<h2 style='color:#333;'>{text}</h2>".format(text=line[3:].strip())
        elif line.startswith("### "):
            html += "<h3 style='color:#666;'>{text}</h3>".format(text=line[4:].strip())
        elif line.startswith("**") and line.endswith("**"):
            html += "<p><strong>{text}</strong></p>".format(text=line[2:-2])
        elif line.startswith("**"):
            html += "<p><strong>{text}</strong>".format(text=line[2:])
        elif line.startswith("- "):
            html += "<li>{text}</li>".format(text=line[2:])
        else:
            html += "<p>{text}</p>".format(text=line)

    html += "</body></html>"

    send_email(html, recipients, mode)
    print(
        "TEST NEWSLETTER: sent to {n} recipient(s) ({mode} mode)".format(
            n=len(recipients), mode=mode
        )
    )
    print("First 300 chars of HTML body (for debugging):")
    print(html[:300])


if __name__ == "__main__":
    main()
