import json
from datetime import datetime, timezone
from typing import Any, Dict, List
import urllib.error
import urllib.request


SOURCE_LABELS = {
    "jusjobs": "JusJobs",
    "erste_bank": "Erste Bank / Sparkasse",
    "uniqa": "UNIQA",
    "lawfinder": "LawFinder",
    "derstandard": "DER STANDARD Jobs",
    "karriere_at": "karriere.at",
    "stepstone": "StepStone",
    "test": "Webhook-Test",
}


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _post_discord_payload(webhook_url: str, payload: Dict[str, Any]) -> bool:
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


def _build_job_field(job: Dict[str, Any], add_spacing: bool) -> Dict[str, Any]:
    details = [
        f"**Unternehmen:** {_truncate(job.get('company') or 'Unbekannt', 180)}",
        f"**Ort:** {_truncate(job.get('location') or 'Unbekannt', 180)}",
        f"**Quelle:** {SOURCE_LABELS.get(job.get('source'), job.get('source') or 'Unbekannt')}",
    ]
    if job.get("salary"):
        details.append(f"**Gehalt:** {_truncate(job.get('salary'), 180)}")
    if job.get("employment_type"):
        details.append(f"**Jobtyp:** {_truncate(job.get('employment_type'), 180)}")
    if job.get("published_at"):
        details.append(f"**Ver\u00f6ffentlicht:** {_truncate(job.get('published_at'), 180)}")
    if job.get("snippet"):
        details.append(_truncate(job["snippet"], 350))
    if job.get("url"):
        details.append(f"**Link:** [Hier ansehen]({job['url']})")

    field_value = "\n".join(details)
    if add_spacing:
        field_value += "\n\u200b"

    return {
        "name": _truncate(f"Neu: {job.get('title') or 'Unbekannter Job'}", 256),
        "value": _truncate(field_value, 1024),
        "inline": False,
    }


def send_new_jobs_notification(webhook_url: str, new_jobs: List[Dict[str, Any]]) -> bool:
    if not webhook_url or not new_jobs:
        return False

    batch_size = 10
    all_ok = True
    total = len(new_jobs)

    for batch_start in range(0, total, batch_size):
        batch = new_jobs[batch_start : batch_start + batch_size]
        batch_end = batch_start + len(batch)
        fields = [
            _build_job_field(job, add_spacing=len(batch) > 1)
            for job in batch
        ]

        description = (
            f"Der Job-Tracker hat {total} neue passende Stelle(n) gefunden."
            f"\nAnzeige {batch_start + 1}-{batch_end} von {total}."
        )
        payload = {
            "embeds": [{
                "title": "Neue Jobausschreibungen gefunden",
                "description": _truncate(description, 4096),
                "url": batch[0].get("url") or "https://www.jusjobs.at/",
                "color": 0x00FF00,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "footer": {"text": "Job Tracker"},
            }]
        }
        all_ok = _post_discord_payload(webhook_url, payload) and all_ok

    return all_ok
