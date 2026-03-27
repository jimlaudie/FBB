import os
import json
import smtplib
import ssl
from email.mime_text import MIMEText
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


def get_espn_data():
    """Fetch scoreboard and team info from ESPN fantasy baseball."""
    cookies = {
        "SWID": SWID,
        "ESPN_S2": ESPN_S2
    }

    # Updated base URL host
    base_url = (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/flb/"
        f"seasons/{SEASON_ID}/segments/0/leagues/{LEAGUE_ID}"
    )

    scoreboard_url = f"{base_url}?view=mMatchupScore"
    teams_url = f"{base_url}?view=mTeam"

    scoreboard_resp = requests.get(scoreboard_url, cookies=cookies)
    teams_resp = requests.get(teams_url, cookies=cookies)

    scoreboard_resp.raise_for_status()
    teams_resp.raise_for_status()

    scoreboard = scoreboard_resp.json()
    teams = teams_resp.json()

    return scoreboard, teams


def build_team_lookup(teams_json):
    """Mapping from teamId -> ESPN team info."""
    lookup = {}
    for team in teams_json.get("teams", []):
        tid = team.get("id")
        name = (team.get("location", "") + " " + team.get("nickname", "")).strip()
        abbrev = team.get("abbrev", "")
        lookup[tid] = {
            "name": name or f"Team {tid}",
            "abbrev": abbrev
        }
    return lookup


def build_config_team_lookup():
    """Mapping from teamId -> config entry (manager name, email, etc.)."""
    lookup = {}
    for t in TEAMS:
        lookup[t["team_id"]] = t
    return lookup


def build_summary(scoreboard, teams_json):
    """Turn raw ESPN JSON into a compact text block for the LLM."""
    espn_team_lookup = build_team_lookup(teams_json)
    config_team_lookup = build_config_team_lookup()

    lines = []

    # Debug listing to help you map team IDs and names
    lines.append("Teams in league (from ESPN):")
    for tid, info in espn_team_lookup.items():
        cfg = config_team_lookup.get(tid)
        cfg_name = cfg["team_name"] if cfg else "NO_CONFIG_NAME"
        lines.append(
            f"- ID {tid}: ESPN name '{info['name']}' (abbrev {info['abbrev']}), config name '{cfg_name}'"
        )

    lines.append("\nWeekly Matchups:")
    for matchup in scoreboard.get("schedule", []):
        home = matchup.get("home", {})
        away = matchup.get("away", {})
        home_team_id = home.get("teamId")
        away_team_id = away.get("teamId")
        home_score = home.get("totalPoints", 0)
        away_score = away.get("totalPoints", 0)

        home_name = espn_team_lookup.get(home_team_id, {}).get("name", f"Team {home_
