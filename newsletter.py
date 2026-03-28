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
            "name": getattr(team, "team_name", f"Team {team.team_id}"),
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
        lines.append(f"- ID {tid}: '{info['name']}' → config '{cfg_name}'")

    # Mode context
    lines.append(f"\nMode: {mode}")
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
        
        home_name = espn_lookup.get(home_id, {}).get("name", f"Team {home_id}")
        away_name = espn_lookup.get(away_id, {}).get("name", f"Team {away_id}")

        lines.append(f"- {home_name} {home_score} vs {away_name} {away_score}")
        has_matchups = True

    if not has_matchups:
        lines.append("- No matchups this period")

    # Standings
    lines.append("\nStandings:")
    teams_list = []
    for team in league.teams:
        tid = getattr(team, "team_id", None)
        name = getattr(team, "team_name", f"Team {tid}")
        
        # Defensive record parsing
        wins = getattr(team, "wins", 0) or 0
        losses = getattr(team, "losses", 0) or 0
        ties = getattr(team, "ties", 0) or 0
        
        if not wins and not losses:
            rec = getattr(team, "record", {})
            wins = rec.get("wins", 0)
            losses = rec.get("losses", 0)
            ties = rec.get("ties", 0)
        
        teams_list.append((tid or 0, name, wins, losses, ties))

    for tid, name, w, l, t in sorted(teams_list, key=lambda x: x[2], reverse=True):
        lines.append(f"- {name}: {w}-{l}-{t}")

    return "\n".join(lines)


def build_prompt(summary_text, mode):
    """Build Axios-style newsletter prompt."""
    base_rules = [
        f"Trash talk: {TRASH_TALK_LEVEL}/10 - sharp but PG.",
        "No swearing. Funny, confident, snarky, never mean.",
        f"Call out {SHANE_TEAM_NAME} as defending champ.",
        f"Roast {JIM_TEAM_NAME} harder (league manager).",
        "Write like Axios: crisp, scannable, bold headers.",
        "Short paragraphs. 1-2 sentences max per idea.",
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
        "weekly": ["Regular week. Recaps + forward look."]
    }

    system_msg = (
        "You're a sharp fantasy baseball newsletter editor. "
        "Write like Axios: punchy, scannable, brutally honest but fun. "
        "Smart Brevity™ style - bold headers, tight prose."
    )

    user_msg = f"""League data:

{summary_text}

Voice & rules:
{chr(10).join('- ' + r for r in base_rules + mode_rules.get(mode, mode_rules['weekly']))}

Write a newsletter in **markdown**. Use:
- ## Headers
- **Bold phrases**
- Short paragraphs
- - Bullets for scannability

Format like this:
## 1 Big Thing
**Why it matters:** One sentence.
- Key detail 1
- Key detail 2

## Team Spotlights
**{Team Name}**  
Natural flow of performance, snark, tip.

## The Big Board
Standings snapshot.

## What's Next
Forward look.
"""

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
    
    subject = f"{SUBJECT_PREFIX} {subject_map.get(mode, 'Weekly Roundup')}"
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = FROM_ADDRESS
    msg["To"] = FROM_ADDRESS  # BCC others
    
    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)
    
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())


def main():
    """Main execution."""
    today = today_mdt()
    mode = newsletter_mode_for_today(today)
    
    if mode is None:
        print(f"No newsletter today ({today}). Next: check schedule.")
        return
    
    print(f"Generating {mode} newsletter for {today}...")
    
    league = get_league()
    summary = build_summary(league, mode)
    newsletter_md = generate_newsletter(summary, mode)
    
    recipients = [TEST_RECIPIENT] if TEST_MODE else [t["email"] for t in TEAMS]
    
    # Simple markdown → HTML
    html = "<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;'>"
    for line in newsletter_md.split("\n"):
        line = line.strip()
        if not line:
            html += "<br>"
        elif line.startswith("##"):
            html += f"<h2 style='color:#333;'>{line[2:].strip()}</h2>"
        elif line.startswith("###"):
            html += f"<h3 style='color:#666;'>{line[3:].strip()}</h3>"
        elif line.startswith("**"):
            html += f"<p><strong>{line[2:-2]}</strong></p>" if line.endswith("**") else f"<strong>{line[2:]}</strong>"
        elif line.startswith("- "):
            html += f"<li>{line[2:]}</li>"
        else:
            html += f"<p>{line}</p>"
    html += "</body></html>"
    
    send_email(html, recipients, mode)
    print(f"Newsletter sent to {len(recipients)} recipient(s) ({mode} mode)")


if __name__ == "__main__":
    main()
