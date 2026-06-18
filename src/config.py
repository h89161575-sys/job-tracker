import os

SNAPSHOTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "snapshots")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
JOB_DISCORD_WEBHOOK_URL = os.environ.get("JOB_DISCORD_WEBHOOK_URL") or DISCORD_WEBHOOK_URL
JOB_TRACKER_ENABLED = os.environ.get("JOB_TRACKER_ENABLED", "1") == "1"
JOB_TRACKER_SOURCES = os.environ.get(
    "JOB_TRACKER_SOURCES",
    "jusjobs,erste_bank,uniqa,lawfinder,derstandard",
)
