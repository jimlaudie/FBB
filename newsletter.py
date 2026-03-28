import os
import json
import smtplib
import ssl
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from openai import OpenAI
from espn_api.baseball import League

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

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
ESPN_S2 = os.environ["ESPN_S2"]
SWID = os.environ["SWID"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

client = OpenAI(api_key=OPENAI_API_KEY)


def parse_ymd(s):
    return date.fromisoformat(s)


def today_mdt():
    return date.today()


def newsletter_mode_for_today(today):
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
    return League(
        league_id=LEAGUE_ID,
        year=SEASON_ID,
        swid=SWID,
        espn_s2=ESPN_S2
    )


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


def build_summary(league, mode):
    espn_lookup, config_lookup = build_team_lookups(league)
    data = league._fetch_league()
    schedule = data.get("schedule", [])

    lines = []

    lines.append("Teams in league (from ESPN):")
    for tid, info in espn_lookup.items():
        cfg = config_lookup.get(tid)
        cfg_name = cfg["team_name"] if cfg else "NO_CONFIG_NAME"
        lines.append(
            f"- ID {tid}: ESPN name '{info['name']}' (abbrev {info['abbrev']}), config name '{cfg_name}'"
        )

    if mode == "draft":
        lines.append("\nNewsletter context:")
        lines.append("- This is the draft-night / season kickoff issue.")
        lines.append("- Focus on draft reactions, roster construction, early favorites, and the defending champ.")
    elif mode == "playoff":
        lines.append("\nNewsletter context:")
        lines.append("- This is a playoff-week issue.")
        lines.append("- Focus on bracket implications, teams still alive, consolation games, and pressure.")
    elif mode == "finale":
        lines.append("\nNewsletter context:")
        lines.append("- This is the final season issue.")
        lines.append("- Focus on championship results, final standings, season awards, and full-season commentary.")
    else:
        lines.append("\nNewsletter context:")
        lines.append("- This is a regular weekly recap.")

    matchup_periods = [m.get("matchupPeriodId") for m in schedule if "matchupPeriodId" in m]
    current_period = max(matchup_periods) if matchup_periods else None

    lines.append("\nWeekly Matchups:")
    has_any_matchup = False
    for matchup in schedule:
        if current_period is not None and matchup.get("matchupPeriodId") != current_period:
            continue

        home = matchup.get("home")
        away = matchup.get("away")
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

    lines.append("\nStandings (rough):")
    teams_list = []
    for team in league.teams:
        tid = getattr(team, "team_id", None)
        name = getattr(team, "team_name", f"Team {tid}")

        wins = getattr(team, "wins", None)
        losses = getattr(team, "losses", None)
        ties = getattr(team, "ties", None)

        if wins is None or losses is None:
            rec = getattr(team, "record", None)
            if isinstance(rec, dict):
                wins = rec.get("wins", 0)
                losses = rec.get("losses", 0)
                ties = rec.get("ties", 0)
            else:
                wins, losses, ties = 0, 0, 0

        teams_list.append((tid, name, wins, losses, ties))

    teams_list.sort(key=lambda x: x[2], reverse=True)
    for tid, name, w, l, t in teams_list:
        lines.append(f"- {name} (ID {tid}): {w}-{l}-{t}")

    return "\n".join(lines)


def build_prompt(summary_text, mode):
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

    extra_mode_rules = []
    if mode == "draft":
        extra_mode_rules = [
            "This is the season kickoff issue after the draft.",
            "Talk about draft winners, reach picks, roster construction, and early expectations.",
            "Include a fun opening welcome to the new season."
        ]
    elif mode == "playoff":
        extra_mode_rules = [
            "This is a playoff-week issue.",
            "Comment on who is alive for the title and who is fighting in consolation rounds.",
            "Reference bracket pressure and every lineup decision mattering more than ever."
        ]
    elif mode == "finale":
        extra_mode_rules = [
            "This is the final issue of the season.",
            "Include championship results, final standings, and season-long takeaways.",
            "Mention season awards like biggest surprise, biggest disappointment, waiver gem, and collapse."
        ]

    system_msg = (
        "You are an AI writing weekly fantasy baseball newsletters for a private ESPN league. "
        "You specialize in playful trash talk, league lore, and practical fantasy advice."
    )

    user_msg = f"""Here is the league data for the current issue:

{summary_text}

League rules and tone:
{chr(10).join('- ' + r for r in rules + extra_mode_rules)}

Write an email-style newsletter. Use markdown headings for sections.
"""

    return system_msg, user_msg


def generate_newsletter(summary_text, mode):
    system_msg, user_msg = build_prompt(summary_text, mode)

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg}
        ]
    )

    return response.choices[0].message.content


def send_email(newsletter_html, recipients, mode):
    subject_suffix = {
        "draft": "Draft Night Special",
        "weekly": "Weekly Recap",
        "playoff": "Playoff Push",
        "finale": "Season Finale"
    }.get(mode, "Weekly Recap")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{SUBJECT_PREFIX} {subject_suffix}"
    msg["From"] = FROM_ADDRESS
    msg["To"] = FROM_ADDRESS

    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())


def main():
    today = today_mdt()
    mode = newsletter_mode_for_today(today)

    if mode is None:
        print("No newsletter scheduled for today.")
        return

    league = get_league()
    summary_text = build_summary(league, mode)
    newsletter_md = generate_newsletter(summary_text, mode)

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

    send_email(html, recipients, mode)


if __name__ == "__main__":
    main()
