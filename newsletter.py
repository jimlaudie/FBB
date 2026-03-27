import os
import json
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from openai import OpenAI

# Load config
with open("config.json", "r") as f:
    CONFIG = json.load(f)

LEAGUE_ID = CONFIG["league"]["league_id"]
SEASON_ID = CONFIG["league"]["season_id"]

FROM_ADDRESS = CONFIG["email"]["from_address"]
SUBJECT_PREFIX = CONFIG["email"]["subject_prefix"]

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


def get_espn_league_data():
    """Fetch basic scoreboard and standings from ESPN fantasy baseball."""
    cookies = {
        "SWID": SWID,
        "ESPN_S2": ESPN_S2
    }

    # Example URLs – may need tweaking to match ESPN's current endpoints.
    # Scoreboard for current scoring period
    scoreboard_url = (
        f"https://fantasy.espn.com/apis/v3/games/flb/seasons/{SEASON_ID}/segments/0/leagues/{LEAGUE_ID}"
        f"?view=mMatchupScore"
    )

    standings_url = (
        f"https://fantasy.espn.com/apis/v3/games/flb/seasons/{SEASON_ID}/segments/0/leagues/{LEAGUE_ID}"
        f"?view=mTeam"
    )

    scoreboard = requests.get(scoreboard_url, cookies=cookies).json()
    standings = requests.get(standings_url, cookies=cookies).json()

    return scoreboard, standings


def build_summary(scoreboard, standings):
    """Turn raw ESPN JSON into a compact text block for the LLM."""
    # This is deliberately simple; we’d refine it once we see real JSON.
    lines = []

    # Matchups (from scoreboard)
    lines.append("Weekly Matchups:")
    for matchup in scoreboard.get("schedule", []):
        home = matchup.get("home", {})
        away = matchup.get("away", {})
        home_team_id = home.get("teamId")
        away_team_id = away.get("teamId")
        home_score = home.get("totalPoints", 0)
        away_score = away.get("totalPoints", 0)

        lines.append(f"- Team {home_team_id} ({home_score}) vs Team {away_team_id} ({away_score})")

    # Standings (from standings)
    lines.append("\nStandings:")
    for team in standings.get("teams", []):
        tid = team.get("id")
        record = team.get("record", {}).get("overall", {})
        wins = record.get("wins", 0)
        losses = record.get("losses", 0)
        ties = record.get("ties", 0)
        lines.append(f"- Team {tid}: {wins}-{losses}-{ties}")

    return "\n".join(lines)


def build_prompt(summary_text):
    """Construct the LLM prompt with your league's personality and rules."""
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
    msg["To"] = FROM_ADDRESS  # To yourself; others BCC

    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())


def main():
    scoreboard, standings = get_espn_league_data()
    summary_text = build_summary(scoreboard, standings)
    newsletter_md = generate_newsletter(summary_text)

    # For now, just send the same email to everyone BCC.
    recipients = [team["email"] for team in TEAMS]

    # Convert markdown-ish text to basic HTML (very simple)
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
