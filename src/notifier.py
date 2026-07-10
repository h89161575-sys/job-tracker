import json
from dataclasses import dataclass
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

DISCORD_EMBED_CHARACTER_LIMIT = 6000
DISCORD_FIELD_CHARACTER_BUDGET = 5300
DISCORD_MAX_JOB_FIELDS_PER_MESSAGE = 10


@dataclass(frozen=True)
class NotificationDeliveryResult:
    delivered_job_ids: List[str]
    failed_job_ids: List[str]

    @property
    def success(self) -> bool:
        return not self.failed_job_ids

    def __bool__(self) -> bool:
        return self.success


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


def _job_delivery_id(job: Dict[str, Any], fallback_index: int) -> str:
    return str(job.get("id") or job.get("url") or f"delivery:{fallback_index}")


def _embed_character_count(embed: Dict[str, Any]) -> int:
    parts = [
        str(embed.get("title") or ""),
        str(embed.get("description") or ""),
        str((embed.get("footer") or {}).get("text") or ""),
        str((embed.get("author") or {}).get("name") or ""),
    ]
    for field in embed.get("fields") or []:
        parts.append(str(field.get("name") or ""))
        parts.append(str(field.get("value") or ""))
    return sum(len(part) for part in parts)


def _build_job_batches(new_jobs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    batches: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_characters = 0

    for job in new_jobs:
        field = _build_job_field(job, add_spacing=True)
        field_characters = len(field["name"]) + len(field["value"])
        if current and (
            len(current) >= DISCORD_MAX_JOB_FIELDS_PER_MESSAGE
            or current_characters + field_characters > DISCORD_FIELD_CHARACTER_BUDGET
        ):
            batches.append(current)
            current = []
            current_characters = 0
        current.append(job)
        current_characters += field_characters

    if current:
        batches.append(current)
    return batches


def deliver_new_jobs_notification(
    webhook_url: str,
    new_jobs: List[Dict[str, Any]],
) -> NotificationDeliveryResult:
    if not new_jobs:
        return NotificationDeliveryResult([], [])
    if not webhook_url:
        return NotificationDeliveryResult(
            [],
            [_job_delivery_id(job, index) for index, job in enumerate(new_jobs)],
        )

    batches = _build_job_batches(new_jobs)
    delivered_job_ids: List[str] = []
    failed_job_ids: List[str] = []
    total = len(new_jobs)
    batch_start = 0

    for batch in batches:
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
        embed = payload["embeds"][0]
        if _embed_character_count(embed) > DISCORD_EMBED_CHARACTER_LIMIT:
            raise ValueError("Discord embed exceeds the 6000 character limit")

        batch_ids = [
            _job_delivery_id(job, batch_start + index)
            for index, job in enumerate(batch)
        ]
        if _post_discord_payload(webhook_url, payload):
            delivered_job_ids.extend(batch_ids)
        else:
            failed_job_ids.extend(batch_ids)
        batch_start = batch_end

    return NotificationDeliveryResult(delivered_job_ids, failed_job_ids)


def send_new_jobs_notification(webhook_url: str, new_jobs: List[Dict[str, Any]]) -> bool:
    return bool(deliver_new_jobs_notification(webhook_url, new_jobs))
