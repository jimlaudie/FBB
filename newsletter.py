import os
import json
import smtplib
import ssl
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

TEAMS = CONFIG["teams"]

# Secrets from GitHub Actions
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["SWID"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

client = OpenAI(api_key=OPENAI_API_KEY)


def get_league():
    league = League(
        league_id=LEAGUE_ID,
        year=SEASON_ID,
        swid=SWID,
        espn_s2=ESPN_S2
    )
    return league


def build_team_lookups(league):
    espn_lookup = {}
    for team in league.teams:
        espn_lookup[team.team_id] = {
            "name": team.team_name,
            "abbrev": team.team_abbrev
        }

    config_lookup = {}
    for t in TEAMS:
        config_lookup[t["team_id"]] = t

    return espn_lookup, config_lookup


def build_summary(league):
    """Use raw league data instead of league.scoreboard() to avoid KeyError on empty/odd weeks."""
    espn_lookup, config_lookup = build_team_lookups(league)
    data = league._fetch_league()  # raw JSON dict
    schedule = data.get("schedule", [])

    lines = []

    # Debug listing
    lines.append("Teams in league (from ESPN):")
    for tid, info in espn_lookup.items():
        cfg = config_lookup.get(tid)
        cfg_name = cfg["team_name"] if cfg else "NO_CONFIG_NAME"
        lines.append(
            f"- ID {tid}: ESPN name '{info['name']}' (abbrev {info['abbrev']}), config name '{cfg_name}'"
        )

    # Try to infer current matchup period
    matchup_periods = [m.get("matchupPeriodId") for m in schedule if "matchupPeriodId" in m]
    current_period = max(matchup_periods) if matchup_periods else None

    lines.append("\nWeekly Matchups:")
    has_any_matchup = False
    for matchup in schedule:
        if current_period is not None and matchup.get("matchupPeriodId") != current_period:
            continue

        home = matchup.get("home")
        away = matchup.get("away")

        # Skip odd entries without both sides (bye weeks, placeholders, etc.)
        if not home or not away:
            continue

        home_team_id = home.get("teamId")
        away_team_id = away.get("teamId")
        home_score = home.get("totalPoints", 0)
        away_score = away.get("totalPoints", 0)

        home_name = espn_lookup.get(home_team_id, {}).get("name", f"Team {home_team_id}")
        away_name = espn_lookup.get(away_team_id, {}).get("name", f"Team {away_team_id}")

        lines.append(
            f"- {home_name} (ID {home_team_id}) scored {home_score} vs "
            f"{away_name} (ID {away_team_id}) scored {away_score}"
        )
        has_any_matchup = True

    if not has_any_matchup:
        lines.append("- No completed matchups found for the current scoring period yet.")

    # Standings
    lines.append("\nStandings (rough):")
    teams_list = []
    for team in league.teams:
        tid = team.team_id
        name = team.team_name
        record = team.record
        wins = record["wins"]
        losses = record["losses"]
        ties = record.get("ties", 0)
        teams_list.append((tid, name, wins, losses, ties))

    teams_list.sort(key=lambda x: x[2], reverse=True)
    for tid, name, w, l, t in teams_list:
        lines.append(f"- {name} (ID {tid}): {w}-{l}-{t}")

    return "\n".join(lines)


def build_prompt(summary_text):
    rules = [
        f"Trash talk level: {TRASH_TALK_LEVEL} (on a 1-10 scale).",
        "Do not use any swear words.",
        "Be funny, confident, and snarky, but not mean-spirited.",
        f"Shane is the returning champion and should be called out as such when relevant. Shane's team name: {SHANE_TEAM_NAME}.",
        f"Jim is the league manager and can be roasted more than others. Jim's team name: {JIM_TEAM_NAME}.",
        "Write a general league update first (1-3 short sections).",
        "Then write a personalized section for each team mentioned in the data.",
        "For each team section, include: last week's performance, one joke or comment, and one suggestion for improvement (waiver, roster, or lineup idea).",
        "Keep everything PG, no profanity, nothing off-color."
    ]

    system_msg = (
        "You are an AI writing weekly fantasy baseball newsletters for a private ESPN league. "
        "You specialize in playful trash talk, league lore, and practical fantasy advice."
    )

    user_msg = f"""Here is the league data for the last scoring period:

{summary_text}

League rules and tone:
{chr(10).join('- ' + r for r in rules)}

Write an email-style newsletter. Use markdown headings for sections.
"""

    return system_msg, user_msg


def generate_newsletter(summary_text):
    system_msg, user_msg = build_prompt(summary_text)

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ]
    )

    return response.choices[0].message.content


def send_email(newsletter_html, recipients):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{SUBJECT_PREFIX} Fantasy Baseball Weekly Recap"
    msg["From"] = FROM_ADDRESS
    msg["To"] = FROM_ADDRESS  # to yourself; others BCC

    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())


def main():
    league = get_league()
    summary_text = build_summary(league)
    newsletter_md = generate_newsletter(summary_text)

    if TEST_MODE:
        recipients = [TEST_RECIPIENT]
    else:
        recipients = [team["email"] for team in TEAMS]

    html = "<html><body>"
    for line in newsletter_md.split("\n"):
        if line.startswith("#"):
            html += f"<h3>{line.lstrip('# ').strip()}</h3>"
        else:
            html += f"<p>{line}</p>"
    html += "</body></html>"

    send_email(html, recipients)


if __name__ == "__main__":
    main()
