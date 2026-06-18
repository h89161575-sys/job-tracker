import json
from datetime import datetime, timezone
from typing import Any, Dict, List
import urllib.error
import urllib.request


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def send_new_jobs_notification(webhook_url: str, new_jobs: List[Dict[str, Any]]) -> bool:
    if not webhook_url or not new_jobs:
        return False

    source_labels = {
        "jusjobs": "JusJobs",
        "erste_bank": "Erste Bank / Sparkasse",
        "uniqa": "UNIQA",
    }
    fields = []
    for job in new_jobs[:10]:
        details = [
            f"**Unternehmen:** {_truncate(job.get('company') or 'Unbekannt', 180)}",
            f"**Ort:** {_truncate(job.get('location') or 'Unbekannt', 180)}",
            f"**Quelle:** {source_labels.get(job.get('source'), job.get('source') or 'Unbekannt')}",
        ]
        if job.get("salary"):
            details.append(f"**Gehalt:** {_truncate(job.get('salary'), 180)}")
        if job.get("employment_type"):
            details.append(f"**Jobtyp:** {_truncate(job.get('employment_type'), 180)}")
        if job.get("published_at"):
            details.append(f"**Veröffentlicht:** {_truncate(job.get('published_at'), 180)}")
        if job.get("url"):
            details.append(f"**Link:** [Hier ansehen]({job['url']})")
        if job.get("snippet"):
            details.append("")
            details.append(_truncate(job["snippet"], 350))
        fields.append({
            "name": _truncate(f"Neu: {job.get('title') or 'Unbekannter Job'}", 256),
            "value": _truncate("\n".join(details), 1024),
            "inline": False,
        })

    description = f"Der Job-Tracker hat {len(new_jobs)} neue passende Stelle(n) gefunden."
    if len(new_jobs) > 10:
        description += f"\n\nEs werden nur die ersten 10 von {len(new_jobs)} Jobs angezeigt."

    payload = {
        "embeds": [{
            "title": "Neue Jobausschreibungen gefunden",
            "description": _truncate(description, 4096),
            "url": new_jobs[0].get("url") or "https://www.jusjobs.at/",
            "color": 0x00FF00,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "footer": {"text": "Job Tracker"},
        }]
    }

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "JobTracker/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            ok = response.status in (200, 204)
            print(f"[discord] notification status={response.status}")
            return ok
    except urllib.error.HTTPError as exc:
        print(f"[discord] HTTP error {exc.code}: {exc.reason}")
    except Exception as exc:
        print(f"[discord] error: {exc}")
    return False
