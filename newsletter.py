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
    """Build more entertaining, story‑driven newsletter prompt."""
    base_rules = [
        "Trash talk: {ttl}/10 - keep it PG but spicy.".format(ttl=TRASH_TALK_LEVEL),
        "No swearing. Funny, confident, and a bit cheeky, but never mean.",
        "Call out {shane} as defending champ.".format(shane=SHANE_TEAM_NAME),
        "Roast {jim} extra hard (league manager).".format(jim=JIM_TEAM_NAME),
        "Write like a lively sports column meets newsletter, not a dry recap.",
        "Paragraphs can be 2–4 sentences; you’re allowed to lean into narrative.",
        "Weave in storylines, rivalries, and personality for each team spotlight.",
        "Still include: 1 big thing, winners/losers, team spotlights, standings snapshot, what’s next."
    ]

    mode_rules = {
        "draft": [
            "Post‑draft kickoff. Celebrate the fresh start.",
            "Rank draft winners/losers and call out the reaches.",
            "Highlight the defending champ’s roster and the manager’s squad’s flaws.",
            "Set the tone for the season: hype, paranoia, and hope."
        ],
        "playoff": [
            "Playoff stakes are ramped up. This is the real season.",
            "Who’s got life? Who’s already in the consolation conversation?",
            "Emphasize the pressure, the momentum, and the must‑win matchups.",
            "Make it feel like the last stretch of a playoff race."
        ],
        "finale": [
            "Championship decided. Wrap up the season like a season‑ending recap.",
            "Award mini ‘titles’: biggest surprise, biggest bust, best waiver‑wire catch.",
            "Give a victory lap to the champ and a sympathetic roast to the rest.",
            "Add a 1‑2 paragraph closing reflection on the year."
        ],
        "weekly": [
            "Regular week recap and look‑ahead, but with extra flavor.",
            "Focus on storylines, rivalries, and juicy moments, not just numbers.",
            "Feel free to personify teams, mock bad decisions, and hype good ones.",
            "End with a punchy forward‑looking section that sets up the next week."
        ]
    }

    system_msg = (
        "You're a sharp, entertaining fantasy baseball newsletter writer. "
        "Write like a mix of sports columnist and modern newsletter: "
        "witty, opinionated, and a bit brash, but still readable and friendly for the whole league."
    )

    mode_list = mode_rules.get(mode, mode_rules["weekly"])
    user_msg = (
        "League data:\n\n"
        "{summary}\n\n"
        "Voice & rules:\n"
        "{rules}\n\n"
        "Write a full newsletter in **plain text** (no Markdown, no emojis, no code blocks).\n"
        "Use these sections as a guide, but feel free to lean into narrative and personality:\n"
        "- Open with a short, punchy intro that sets the tone.\n"
        "- 1 Big Thing: the main storyline of the week in 2–4 sentences.\n"
        "- Winners & Losers: 2–6 short blurbs, 2–4 sentences each, with some light trash talk.\n"
        "- Team Spotlights: 2–4 sentences per team, focusing on story, personality, and a hint of strategy.\n"
        "- Standings Snapshot: 1–2 paragraphs showing how the league shape changed.\n"
        "- What’s Next: 2–4 sentences teasing next week, upcoming matchups, or waiver‑wire targets.\n"
        "- Close with a 1–2 sentence sign‑off that matches the tone.\n"
        "Keep it under about 700–900 words total. Do not use any Markdown formatting."
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
    msg["To"] = FROM_ADDRESS  # header only; actual routing via `recipients` list

    part_html = MIMEText(newsletter_html, "html")
    msg.attach(part_html)

    print("About to send to {n} recipients: {recipients}".format(
        n=len(recipients), recipients=recipients))
    print("From: {from_addr}, Subject: {subject}".format(
        from_addr=FROM_ADDRESS, subject=subject))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        print("Connecting to SMTP_SSL (smtp.gmail.com:465)...")
        server.login(FROM_ADDRESS, GMAIL_APP_PASSWORD)
        print("Logged in to Gmail SMTP")
        server.sendmail(FROM_ADDRESS, recipients, msg.as_string())
        print("Email sent via SMTP_SSL (no explicit error)")


def main():
    """Main execution."""
    today = today_mdt()

    # ----------- TEMPORARY TEST OVERRIDE: remove when live -----------
    mode = "weekly"
    # ----------- END TEST OVERRIDE -----------

    # Uncomment to restore schedule logic (only for Mondays, etc.)
    # mode = newsletter_mode_for_today(today)
    # if mode is None:
    #     print("No newsletter today ({today}). Next: check schedule.".format(today=today))
    #     return

    recipients = [TEST_RECIPIENT] if TEST_MODE else [t["email"] for t in TEAMS]

    print("TEST MODE: sending {mode} newsletter to: {recipients}".format(
        mode=mode, recipients=recipients))

    league = get_league()
    summary = build_summary(league, mode)
    newsletter_md = generate_newsletter(summary, mode)

    # Markdown → HTML
    html = (
        "<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;'>"
    )

    in_ul = False
    for line in newsletter_md.splitlines():
        line = line.strip()

        if not line:
            if in_ul:
                html += "</ul>"
                in_ul = False
            html += "<br>"
            continue

        if line.startswith("## "):
            if in_ul:
                html += "</ul>"
                in_ul = False
            text = line[3:].strip()
            html += "<h2 style='color:#333;'>{text}</h2>".format(text=text)

        elif line.startswith("### "):
            if in_ul:
                html += "</ul>"
                in_ul = False
            text = line[4:].strip()
            html += "<h3 style='color:#666;'>{text}</h3>".format(text=text)

        elif line.startswith("**") and line.endswith("**"):
            if in_ul:
                html += "</ul>"
                in_ul = False
            text = line[2:-2].strip()
            html += "<p><strong>{text}</strong></p>".format(text=text)

        elif line.startswith("**"):
            if in_ul:
                html += "</ul>"
                in_ul = False
            text = line[2:].strip()
            html += "<p><strong>{text}</strong></p>".format(text=text)

        elif line.startswith("- "):
            if not in_ul:
                html += "<ul>"
                in_ul = True
            text = line[2:].strip()
            html += "<li>{text}</li>".format(text=text)

        else:
            if in_ul:
                html += "</ul>"
                in_ul = False
            html += "<p>{text}</p>".format(text=line)

    if in_ul:
        html += "</ul>"

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
