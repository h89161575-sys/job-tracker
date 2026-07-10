import argparse
import gzip
import hashlib
import json
import os
import re
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from config import (
    JOB_DISCORD_WEBHOOK_URL,
    JOB_TRACKER_ENABLED,
    SNAPSHOTS_DIR,
)
from notifier import deliver_new_jobs_notification, send_new_jobs_notification


JOB_SNAPSHOT_NAME = "job_tracker"
MAX_PAGES_PER_SOURCE = 20
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT_LANGUAGE = "de-AT,de;q=0.9,en;q=0.5"

JUSJOBS_SEARCH_URL = (
    "https://www.jusjobs.at/jobs?"
    "region=Wien&"
    "jobType=Unternehmensjurist+/+In-House+Counsel,Praktikum/Trainee,wissenschaftl.+Arbeit&"
    "minSalary=3000&maxSalary=7000"
)
SPARKASSE_JOBLIST_PAGE_URL = (
    "https://www.sparkasse.at/erstebank/karriere/stellenangebote"
    "#/joblist/location/Wien/discipline_items/Legal%20%2F%20Compliance%20%2F%20Audit"
)
SPARKASSE_JOBLIST_API_FALLBACK_URL = (
    "https://erste-digital-gmbh-rec-at-production-t1cud7tl-at-job-bo4264c7d2."
    "cfapps.eu11.hana.ondemand.com/list?language=de_DE"
)
SPARKASSE_JOBLIST_FALLBACK_HEADERS = {
    "Referer": "https://www.sparkasse.at/",
}
UNIQA_RSS_URL = (
    "https://careers.uniqagroup.com/services/rss/job/?"
    + urllib.parse.urlencode(
        {"locale": "de_DE", "keywords": "(Jurist) AND locationSearch:(Wien)"}
    )
)
KARRIERE_SEARCH_URL = (
    "https://www.karriere.at/jobs/jus/wien?"
    "jobFields%5B%5D=4048&employmentTypes%5B%5D=3960&homeoffice=true&salary%5B%5D=10007"
)
KARRIERE_LOAD_MORE_BASE_URL = "https://www.karriere.at/jobs"
KARRIERE_LOAD_MORE_PARAMS = (
    ("keywords", "jus"),
    ("locations", "wien"),
    ("jobFields[]", "4048"),
    ("employmentTypes[]", "3960"),
    ("homeoffice", "true"),
    ("salary[]", "10007"),
)
STEPSTONE_BASE_URL = "https://www.stepstone.at/"
STEPSTONE_SEARCH_URL = "https://www.stepstone.at/jobs/jurist/in-wien?page=1"
STEPSTONE_TIMEOUT_SECONDS = 30
STEPSTONE_CURL_TIMEOUT_SECONDS = 75
STEPSTONE_FETCH_ATTEMPTS = 1

DEFAULT_SOURCE_NAMES = (
    "jusjobs",
    "erste_bank",
    "uniqa",
    "lawfinder",
    "derstandard",
    "karriere_at",
    "stepstone",
)
OPTIONAL_SOURCE_NAMES: Tuple[str, ...] = ()

FetchResult = Tuple[List[Dict[str, Any]], List[str]]
Fetcher = Callable[[], FetchResult]


def _sparkasse_fallback_headers() -> Dict[str, str]:
    headers = dict(SPARKASSE_JOBLIST_FALLBACK_HEADERS)
    authorization = normalize_text(os.environ.get("SPARKASSE_JOBLIST_AUTHORIZATION"))
    if authorization:
        headers["Authorization"] = authorization
    return headers


@dataclass(frozen=True)
class SourceConfig:
    name: str
    label: str
    fetcher: Fetcher
    default_enabled: bool = True


@dataclass
class JobRunResult:
    first_run: bool
    new_jobs: List[Dict[str, Any]]
    all_jobs: List[Dict[str, Any]]
    snapshot: Dict[str, Any]
    sources: Dict[str, Dict[str, Any]]
    pending_jobs: List[Dict[str, Any]]
    run_errors: List[str]

    @property
    def healthy(self) -> bool:
        return not self.run_errors


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_text(value: Any) -> str:
    """Strip HTML, decode entities, repair common mojibake, and collapse whitespace."""
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = unescape(text)
    text = _repair_common_utf8_mojibake(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _repair_common_utf8_mojibake(text: str) -> str:
    if not text:
        return text

    markers = (
        "Ã",
        "Â",
        "â",
        "Ã¢â‚¬â„¢",
        "Ã¢â‚¬Å“",
        "Ã¢â‚¬Â",
        "Ã¢â‚¬â€œ",
        "Ã¢â‚¬â€",
        "Ã¢â‚¬Â¦",
        "Ãƒ",
        "Ã‚",
    )
    original_hits = sum(text.count(marker) for marker in markers)
    if original_hits == 0:
        return text

    best_text = text
    best_hits = original_hits
    for source_encoding in ("cp1252", "latin-1"):
        try:
            repaired = text.encode(source_encoding).decode("utf-8")
        except Exception:
            continue

        repaired_hits = sum(repaired.count(marker) for marker in markers)
        if repaired_hits < best_hits:
            best_text = repaired
            best_hits = repaired_hits

    return best_text


def _text_decode_score(text: str) -> int:
    mojibake_markers = ("�", "Ã", "Â", "â")
    return sum(text.count(marker) for marker in mojibake_markers)


def decode_response_body(raw: bytes, charset: Optional[str]) -> str:
    encodings: List[str] = []
    for encoding in (charset, "utf-8", "cp1252", "latin-1"):
        if encoding and encoding.lower() not in [item.lower() for item in encodings]:
            encodings.append(encoding)

    candidates: List[Tuple[int, str]] = []
    for encoding in encodings:
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            decoded = raw.decode(encoding, errors="replace")
        candidates.append((_text_decode_score(decoded), decoded))

    if not candidates:
        return raw.decode("utf-8", errors="replace")
    return min(candidates, key=lambda item: item[0])[1]


def slugify(text: str) -> str:
    text = normalize_text(text).lower()
    for old, new in {
        "\u00e4": "ae",
        "\u00f6": "oe",
        "\u00fc": "ue",
        "\u00df": "ss",
        "\u00e1": "a",
        "\u00e0": "a",
        "\u00e9": "e",
        "\u00e8": "e",
        "\u00ed": "i",
        "\u00f3": "o",
        "\u00fa": "u",
    }.items():
        text = text.replace(old, new)
    for old, new in {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "á": "a",
        "à": "a",
        "é": "e",
        "è": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
    }.items():
        text = text.replace(old, new)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def job_fingerprint(job: Dict[str, Any]) -> str:
    relevant = {
        key: normalize_text(job.get(key))
        for key in (
            "source",
            "title",
            "company",
            "location",
            "url",
            "salary",
            "employment_type",
            "published_at",
            "department",
        )
        if job.get(key) is not None
    }
    payload = json.dumps(relevant, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_job_key(job: Dict[str, Any]) -> str:
    job_id = normalize_text(job.get("id"))
    if job_id:
        return f"id:{job_id}"

    url = normalize_text(job.get("url")).lower().rstrip("/")
    if url:
        return f"url:{url}"

    parts = [
        normalize_text(job.get("source")).lower(),
        normalize_text(job.get("title")).lower(),
        normalize_text(job.get("company")).lower(),
        normalize_text(job.get("location")).lower(),
    ]
    return "fields:" + "|".join(parts)


def finalize_job(job: Dict[str, Any], fetched_at: str) -> Dict[str, Any]:
    normalized = dict(job)
    normalized["source"] = normalize_text(normalized.get("source"))
    normalized["title"] = normalize_text(normalized.get("title")) or "Unbekannter Job"
    normalized["company"] = normalize_text(normalized.get("company")) or "Unbekannt"
    normalized["location"] = normalize_text(normalized.get("location"))
    normalized["url"] = normalize_text(normalized.get("url"))

    for key in ("salary", "employment_type", "published_at", "department", "snippet"):
        if key in normalized:
            value = normalize_text(normalized.get(key))
            normalized[key] = value or None

    if not normalize_text(normalized.get("id")):
        digest = hashlib.sha256(canonical_job_key(normalized).encode("utf-8")).hexdigest()[:16]
        normalized["id"] = f"{normalized['source']}:{digest}"

    normalized.setdefault("first_seen", fetched_at)
    normalized["fingerprint"] = job_fingerprint(normalized)
    return normalized


def dedupe_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_fallbacks: set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for job in jobs:
        job_id = normalize_text(job.get("id"))
        if job_id and job_id in seen_ids:
            continue

        url = normalize_text(job.get("url")).lower().rstrip("/")
        if url and url in seen_urls:
            continue

        fallback_key = (
            "|".join(
                [
                    normalize_text(job.get("source")).lower(),
                    normalize_text(job.get("title")).lower(),
                    normalize_text(job.get("company")).lower(),
                    normalize_text(job.get("location")).lower(),
                ]
            )
        )
        if not job_id and not url and fallback_key in seen_fallbacks:
            continue

        if job_id:
            seen_ids.add(job_id)
        if url:
            seen_urls.add(url)
        if fallback_key:
            seen_fallbacks.add(fallback_key)
        deduped.append(job)
    return deduped


TITLE_MATCH_STOPWORDS = {
    "all",
    "alle",
    "at",
    "d",
    "der",
    "die",
    "das",
    "fuer",
    "fur",
    "in",
    "im",
    "m",
    "mit",
    "schwerpunkt",
    "teilzeit",
    "und",
    "vollzeit",
    "w",
    "x",
}

COMPANY_MATCH_STOPWORDS = {
    "ag",
    "aktiengesellschaft",
    "co",
    "gesmbh",
    "gmbh",
    "group",
    "holding",
    "inc",
    "kg",
    "ltd",
    "mbh",
    "og",
}


def _tokenize_match_text(text: Any, stopwords: set[str]) -> List[str]:
    tokens = [
        token
        for token in slugify(normalize_text(text)).split("-")
        if len(token) > 1 and token not in stopwords
    ]
    return tokens


def _company_match_key(company: Any) -> str:
    return "-".join(_tokenize_match_text(company, COMPANY_MATCH_STOPWORDS))


def _is_wien_location_text(location: Any) -> bool:
    parts = slugify(normalize_text(location)).split("-")
    return "wien" in parts or "vienna" in parts


def _location_match_key(location: Any) -> str:
    location_slug = slugify(normalize_text(location))
    if _is_wien_location_text(location):
        return "wien"
    return location_slug


def _title_tokens(job: Dict[str, Any]) -> set[str]:
    return set(_tokenize_match_text(job.get("title"), TITLE_MATCH_STOPWORDS))


def _title_similarity(left: Dict[str, Any], right: Dict[str, Any]) -> float:
    left_tokens = _title_tokens(left)
    right_tokens = _title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    shared = left_tokens & right_tokens
    if not shared:
        return 0.0

    smaller = min(len(left_tokens), len(right_tokens))
    larger = max(len(left_tokens), len(right_tokens))
    if len(shared) == smaller and smaller <= 2:
        return len(shared) / larger
    return len(shared) / len(left_tokens | right_tokens)


def is_cross_source_duplicate(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    left_source = normalize_text(left.get("source"))
    right_source = normalize_text(right.get("source"))
    if left_source and right_source and left_source == right_source:
        return False

    left_company = _company_match_key(left.get("company"))
    right_company = _company_match_key(right.get("company"))
    if not left_company or left_company != right_company:
        return False

    left_location = _location_match_key(left.get("location"))
    right_location = _location_match_key(right.get("location"))
    if left_location and right_location and left_location != right_location:
        return False

    return _title_similarity(left, right) >= 0.72


def find_cross_source_duplicate(
    job: Dict[str, Any],
    candidates: Iterable[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if is_cross_source_duplicate(job, candidate):
            return candidate
    return None


def dedupe_cross_source_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if find_cross_source_duplicate(job, deduped):
            continue
        deduped.append(job)
    return deduped


def _ssl_context() -> Optional[ssl.SSLContext]:
    if os.environ.get("JOB_TRACKER_ALLOW_INSECURE_SSL") == "1":
        return ssl._create_unverified_context()
    return None


def fetch_url(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    is_json: bool = False,
    accept: Optional[str] = None,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
) -> Tuple[Optional[Any], Optional[str]]:
    request_headers = {
        "User-Agent": REQUEST_USER_AGENT,
        "Accept": accept
        or ("application/json,text/plain,*/*" if is_json else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        request_headers.update(headers)

    try:
        req = urllib.request.Request(url, headers=request_headers)
        context = _ssl_context()
        if context is None:
            response_cm = urllib.request.urlopen(req, timeout=timeout_seconds)
        else:
            response_cm = urllib.request.urlopen(req, timeout=timeout_seconds, context=context)

        with response_cm as response:
            raw = response.read()
            content_encoding = (response.headers.get("Content-Encoding") or "").lower()
            if "gzip" in content_encoding or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif "deflate" in content_encoding:
                try:
                    raw = zlib.decompress(raw)
                except Exception:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)

            charset = response.headers.get_content_charset() or "utf-8"
            decoded = decode_response_body(raw, charset)
            if is_json:
                return json.loads(decoded), None
            return decoded, None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return None, f"HTTP {exc.code}: {body[:200] or exc.reason}"
    except urllib.error.URLError as exc:
        return None, f"URL error: {exc.reason}"
    except json.JSONDecodeError as exc:
        return None, f"JSON decode error: {exc}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def fetch_url_with_curl(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    is_json: bool = False,
    accept: Optional[str] = None,
    timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
) -> Tuple[Optional[Any], Optional[str]]:
    request_headers = {
        "Accept": accept
        or ("application/json,text/plain,*/*" if is_json else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": DEFAULT_ACCEPT_LANGUAGE,
    }
    if headers:
        request_headers.update(headers)

    curl_binary = "curl.exe" if os.name == "nt" else "curl"
    args = [curl_binary]
    if os.name == "nt":
        args.append("--ssl-no-revoke")
    args.extend(
        [
            "-L",
            "--compressed",
            "--fail",
            "-sS",
            "--http1.1",
            "--max-time",
            str(timeout_seconds),
            "-A",
            REQUEST_USER_AGENT,
        ]
    )
    for key, value in request_headers.items():
        args.extend(["-H", f"{key}: {value}"])
    args.append(url)

    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout_seconds + 10,
            check=False,
        )
    except FileNotFoundError:
        return None, f"{curl_binary} not found"
    except subprocess.TimeoutExpired:
        return None, f"{curl_binary} timed out after {timeout_seconds}s"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if completed.returncode != 0:
        stderr = decode_response_body(completed.stderr or b"", "utf-8").strip()
        return None, f"{curl_binary} exit {completed.returncode}: {stderr[:200] or 'request failed'}"

    decoded = decode_response_body(completed.stdout or b"", "utf-8")
    if is_json:
        try:
            return json.loads(decoded), None
        except json.JSONDecodeError as exc:
            return None, f"JSON decode error: {exc}"
    return decoded, None


def _absolute_url(base: str, href: str) -> str:
    return urllib.parse.urljoin(base, unescape(href or ""))


def _extract_between_markers(text: str, start_pattern: str, end_pattern: str) -> str:
    start_match = re.search(start_pattern, text, re.IGNORECASE | re.DOTALL)
    if not start_match:
        return text

    start = start_match.start()
    end_match = re.search(end_pattern, text[start_match.end() :], re.IGNORECASE | re.DOTALL)
    if not end_match:
        return text[start:]
    return text[start : start_match.end() + end_match.start()]


def _extract_first(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return normalize_text(match.group(1)) if match else default


def _extract_jusjobs_expected_count(html: str) -> Optional[int]:
    cleaned = html.replace("<!-- -->", "")
    patterns = [
        r'<span[^>]*id=["\']number["\'][^>]*>\s*(\d+)\s*</span>',
        r"<b>\s*\d+\s+von\s+(\d+)\s*</b>",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE | re.DOTALL)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _count_jusjobs_result_cards(html: str) -> int:
    result_html = _extract_between_markers(
        html.replace("<!-- -->", ""),
        r'id=["\']jobSearchResults["\']',
        r'<div[^>]+class=["\'][^"\']*\bwhiteBg\b',
    )
    return len(
        re.findall(
            r'<div[^>]+class=["\'][^"\']*\bjobResult\b[^"\']*["\']',
            result_html,
            re.IGNORECASE,
        )
    )


def parse_jusjobs_jobs_from_html(html: str, fetched_at: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    fetched_at = fetched_at or utc_now()
    warnings: List[str] = []
    result_html = _extract_between_markers(
        html.replace("<!-- -->", ""),
        r'id=["\']jobSearchResults["\']',
        r'<div[^>]+class=["\'][^"\']*\bwhiteBg\b',
    )

    link_matches = list(re.finditer(r'href=["\'](/job/(\d+))["\']', result_html, re.IGNORECASE))
    jobs: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    for match in link_matches:
        url_path, raw_id = match.groups()
        job_id = f"jusjobs:{raw_id}"
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        card_start = result_html.rfind('<div class="row jobResult', 0, match.start())
        if card_start == -1:
            card_start = result_html.rfind("<div", 0, match.start())
        if card_start == -1:
            card_start = max(0, match.start() - 1000)

        next_card = result_html.find('<div class="row jobResult', match.end())
        card_end = next_card if next_card != -1 else min(len(result_html), match.end() + 2500)
        card_html = result_html[card_start:card_end]
        card_text = normalize_text(card_html)

        title = _extract_first(
            r'href=["\']' + re.escape(url_path) + r'["\'][^>]*>(.*?)</a>',
            card_html,
            "Unbekannter Job",
        )
        company = _extract_first(
            r'class=["\'][^"\']*\bjobCompanyName\b[^"\']*["\'][\s\S]*?<a[^>]*>(.*?)</a>',
            card_html,
            "Unbekannt",
        )
        snippet = _extract_first(
            r'class=["\'][^"\']*\bjobInfo\b[^"\']*["\'][^>]*>(.*?)</p>',
            card_html,
        )
        salary_match = re.search(
            r"(€\s*[0-9][0-9.,]*(?:\s*(?:bis|-|–)\s*€?\s*[0-9][0-9.,]*)?)",
            card_text,
        )
        if not salary_match:
            salary_match = re.search(
                r"(\u20ac\s*[0-9][0-9.,]*(?:\s*(?:bis|-|\u2013)\s*\u20ac?\s*[0-9][0-9.,]*)?)",
                card_text,
            )
        employment_match = re.search(r"\b(Vollzeit|Teilzeit|Praktikum|Trainee)\b", card_text, re.IGNORECASE)

        jobs.append(
            finalize_job(
                {
                    "id": job_id,
                    "source": "jusjobs",
                    "title": title,
                    "company": company,
                    "location": "Wien",
                    "url": f"https://www.jusjobs.at{url_path}",
                    "salary": salary_match.group(1) if salary_match else None,
                    "employment_type": employment_match.group(1) if employment_match else None,
                    "snippet": snippet,
                    "first_seen": fetched_at,
                },
                fetched_at,
            )
        )

    expected_count = _extract_jusjobs_expected_count(html)
    if expected_count is not None and expected_count != len(jobs):
        warnings.append(
            f"JusJobs expected {expected_count} result(s) but extracted {len(jobs)} unique job link(s)"
        )

    return jobs, warnings


def fetch_jusjobs_jobs() -> FetchResult:
    """Fetch the filtered JusJobs result HTML and follow the offset-based pagination."""
    fetched_at = utc_now()
    jobs: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_count: Optional[int] = None

    for _page in range(MAX_PAGES_PER_SOURCE):
        offset = len(jobs)
        url = JUSJOBS_SEARCH_URL if offset == 0 else f"{JUSJOBS_SEARCH_URL}&offset={offset}"
        html, err = fetch_url(url)
        if err:
            if jobs:
                return jobs, [f"JusJobs pagination incomplete at offset {offset}: {err}"]
            return [], [f"JusJobs fetch error: {err}"]
        if not html:
            return jobs, [f"JusJobs returned an empty page at offset {offset}"]

        if expected_count is None:
            expected_count = _extract_jusjobs_expected_count(html)

        page_jobs, _warnings = parse_jusjobs_jobs_from_html(html, fetched_at)
        rendered_card_count = _count_jusjobs_result_cards(html)
        if rendered_card_count != len(page_jobs):
            return jobs, [
                f"JusJobs rendered {rendered_card_count} result card(s) at offset {offset} "
                f"but parsed {len(page_jobs)} unique job link(s)"
            ]
        added = 0
        for job in page_jobs:
            job_id = normalize_text(job.get("id"))
            if not job_id or job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            jobs.append(job)
            added += 1

        if expected_count is not None and len(jobs) >= expected_count:
            break
        if added == 0:
            break

    if expected_count is not None and expected_count != len(jobs):
        print(
            f"[jobs][warn] JusJobs advertises {expected_count} result(s), "
            f"but its rendered result cards contain {len(jobs)} unique job(s)"
        )
    if expected_count is None and not jobs:
        return [], ["JusJobs response contained no recognizable result count or job links"]
    return jobs, []


def _parse_gem_json_configs(html: str) -> List[Dict[str, Any]]:
    configs: List[Dict[str, Any]] = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/gem\+json["\'][^>]*>(.*?)</script>',
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        body = unescape(match.group(1)).strip()
        try:
            configs.append(json.loads(body))
        except json.JSONDecodeError:
            continue
    return configs


def discover_sparkasse_joblist_api() -> Tuple[str, Dict[str, str], str]:
    html, err = fetch_url(SPARKASSE_JOBLIST_PAGE_URL)
    if err or not html:
        print(f"[jobs][warn] Sparkasse page config discovery failed, using documented fallback API: {err}")
        return (
            SPARKASSE_JOBLIST_API_FALLBACK_URL,
            _sparkasse_fallback_headers(),
            "/erstebank/karriere-spk/job-detail",
        )

    for config in _parse_gem_json_configs(html):
        api_config = config.get("apiConfiguration")
        if not isinstance(api_config, dict):
            continue
        if config.get("cId") != "joblist" and api_config.get("name") != "Joblist":
            continue
        api_url = normalize_text(api_config.get("url"))
        if not api_url:
            continue
        headers = dict(api_config.get("headers") or {})
        headers.setdefault("Referer", "https://www.sparkasse.at/")
        detail_path = normalize_text(config.get("path")) or "/erstebank/karriere-spk/job-detail"
        return api_url, headers, detail_path

    print("[jobs][warn] Sparkasse joblist config not found, using documented fallback API")
    return (
        SPARKASSE_JOBLIST_API_FALLBACK_URL,
        _sparkasse_fallback_headers(),
        "/erstebank/karriere-spk/job-detail",
    )


def _location_is_wien(locations: Any) -> bool:
    if isinstance(locations, str):
        locations = [locations]
    if not isinstance(locations, list):
        return False
    return any(normalize_text(location).lower() in {"wien", "vienna"} for location in locations)


def _discipline_is_legal_compliance_audit(disciplines: Any) -> bool:
    if isinstance(disciplines, str):
        disciplines = [disciplines]
    if not isinstance(disciplines, list):
        return False
    return any(normalize_text(item).casefold() == "legal / compliance / audit" for item in disciplines)


def filter_erste_bank_jobs(
    raw_jobs: Iterable[Dict[str, Any]],
    fetched_at: Optional[str] = None,
    detail_path: str = "/erstebank/karriere-spk/job-detail",
) -> List[Dict[str, Any]]:
    fetched_at = fetched_at or utc_now()
    jobs: List[Dict[str, Any]] = []
    for raw in raw_jobs:
        if not _location_is_wien(raw.get("location")):
            continue
        if not _discipline_is_legal_compliance_audit(raw.get("discipline_items")):
            continue

        raw_id = normalize_text(raw.get("id"))
        if not raw_id:
            continue
        title = normalize_text(raw.get("external_title") or raw.get("job_title") or "Job")
        job_detail_path = f"{detail_path.rstrip('/')}/{slugify(title)}/{raw_id}"
        jobs.append(
            finalize_job(
                {
                    "id": f"erste_bank:{raw_id}",
                    "source": "erste_bank",
                    "title": title,
                    "company": raw.get("legal_entity_name") or "Erste Bank / Sparkasse",
                    "location": "Wien",
                    "url": f"https://www.sparkasse.at{job_detail_path}",
                    "employment_type": raw.get("employment_level"),
                    "department": raw.get("department") or "Legal / Compliance / Audit",
                    "published_at": raw.get("posting_date"),
                    "first_seen": fetched_at,
                },
                fetched_at,
            )
        )
    return jobs


def fetch_erste_bank_jobs() -> FetchResult:
    api_url, headers, detail_path = discover_sparkasse_joblist_api()
    data, err = fetch_url(api_url, headers=headers, is_json=True)
    if err:
        return [], [f"Erste Bank API error: {err}"]
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return [], ["Erste Bank API returned an unexpected response structure"]
    return filter_erste_bank_jobs(data["data"], detail_path=detail_path), []


def _find_xml_text(item: ET.Element, name: str) -> str:
    found = item.find(name)
    return found.text if found is not None and found.text else ""


def _extract_uniqa_job_id(link: str, guid: str) -> str:
    match = re.search(r"/(\d+)/?$", link)
    if match:
        return match.group(1)
    raw = normalize_text(guid or link)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return digest


def _contains_wien(text: str) -> bool:
    return re.search(r"\b(wien|vienna)\b", normalize_text(text), re.IGNORECASE) is not None


def parse_uniqa_rss_jobs(rss_xml: str, fetched_at: Optional[str] = None) -> List[Dict[str, Any]]:
    fetched_at = fetched_at or utc_now()
    root = ET.fromstring(rss_xml)
    jobs: List[Dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        title = normalize_text(_find_xml_text(item, "title"))
        link = normalize_text(_find_xml_text(item, "link"))
        guid = normalize_text(_find_xml_text(item, "guid"))
        description = normalize_text(_find_xml_text(item, "description"))
        published_at = normalize_text(_find_xml_text(item, "pubDate"))

        combined = " ".join([title, link, description])
        if not _contains_wien(combined):
            continue

        raw_id = _extract_uniqa_job_id(link, guid)
        location_match = re.search(r"\(([^)]*(?:Wien|Vienna)[^)]*)\)", title, re.IGNORECASE)
        location = location_match.group(1) if location_match else "Wien"
        clean_title = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip() or title

        jobs.append(
            finalize_job(
                {
                    "id": f"uniqa:{raw_id}",
                    "source": "uniqa",
                    "title": clean_title,
                    "company": "UNIQA",
                    "location": location,
                    "url": link,
                    "published_at": published_at,
                    "snippet": description[:700],
                    "first_seen": fetched_at,
                },
                fetched_at,
            )
        )
    return jobs


def fetch_uniqa_jobs() -> FetchResult:
    rss_xml, err = fetch_url(
        UNIQA_RSS_URL,
        accept="application/rss+xml,application/xml,text/xml,*/*",
    )
    if err:
        return [], [f"UNIQA RSS error: {err}"]
    if not rss_xml:
        return [], ["UNIQA RSS returned an empty response"]
    try:
        return parse_uniqa_rss_jobs(rss_xml), []
    except ET.ParseError as exc:
        return [], [f"UNIQA RSS parse error: {exc}"]


def parse_lawfinder_jobs_from_html(
    html: str,
    fetched_at: Optional[str] = None,
) -> FetchResult:
    jobs: List[Dict[str, Any]] = []
    fetched_at = fetched_at or utc_now()
    flight_chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\s*\]\)', html, re.DOTALL)
    decoded_job_lists = 0
    parse_errors: List[str] = []
    for push in flight_chunks:
        try:
            payload = json.loads(f'"{push}"')
            colon_idx = payload.find(":")
            if colon_idx == -1 or colon_idx >= 10:
                continue
            json_part = payload[colon_idx + 1 :].strip()
            if not json_part.startswith("{") or not json_part.endswith("}"):
                continue
            data = json.loads(json_part)
            raw_jobs = data.get("data")
            if not isinstance(raw_jobs, list):
                continue
            decoded_job_lists += 1
            for raw in raw_jobs:
                raw_id = normalize_text(raw.get("id"))
                if not raw_id:
                    continue
                employer = raw.get("employer") or {}
                locations = raw.get("jobLocations") or []
                jobs.append(
                    finalize_job(
                        {
                            "id": f"lawfinder:{raw_id}",
                            "source": "lawfinder",
                            "title": raw.get("title"),
                            "company": employer.get("title") if isinstance(employer, dict) else None,
                            "location": ", ".join(
                                normalize_text(location.get("title"))
                                for location in locations
                                if isinstance(location, dict)
                            )
                            or "Wien",
                            "url": f"https://www.lawfinder.at/jobs/{raw_id}",
                            "first_seen": fetched_at,
                        },
                        fetched_at,
                    )
                )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            parse_errors.append(str(exc))
            continue
    if decoded_job_lists == 0:
        detail = f"; last parse error: {parse_errors[-1]}" if parse_errors else ""
        return [], [
            f"LawFinder found {len(flight_chunks)} Next.js flight chunk(s) "
            f"but no parseable job list{detail}"
        ]
    return dedupe_jobs(jobs), []


def fetch_lawfinder_jobs() -> FetchResult:
    url = (
        "https://www.lawfinder.at/jobs?bundesland=wien&firmentyp=versicherung&"
        "firmentyp=unternehmen&firmentyp=universitaet&firmentyp=oeffentlicher-dienst&firmentyp=verein"
    )
    html, err = fetch_url(url)
    if err:
        return [], [f"LawFinder error: {err}"]
    if not html:
        return [], ["LawFinder returned an empty response"]
    return parse_lawfinder_jobs_from_html(html)


def fetch_derstandard_jobs() -> FetchResult:
    jobs: List[Dict[str, Any]] = []
    url = "https://jobs.derstandard.at/suche/wien/Jurist?benefits=home_office&employmentTypes=full_time&occupationalCategories=legal"
    html, err = fetch_url(url)
    if err:
        return [], [f"Der Standard error: {err}"]
    if not html:
        return [], ["Der Standard returned an empty response"]

    script = ""
    for _attrs, body in re.findall(r"<script([^>]*)>(.*?)</script>", html, re.DOTALL):
        if "ApolloSSRDataTransport" in body:
            script = body
            break
    if not script:
        return [], ["Der Standard ApolloSSRDataTransport script not found"]

    first_brace = script.find("{")
    last_brace = script.rfind("}")
    if first_brace == -1 or last_brace == -1:
        return [], ["Der Standard Apollo payload JSON not found"]

    fetched_at = utc_now()
    try:
        payload = re.sub(r":\s*undefined\b", ": null", script[first_brace : last_brace + 1])
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        return [], [f"Der Standard Apollo payload parse error: {exc}"]

    for value in (data.get("rehydrate") or {}).values():
        value_data = (value.get("data") or {}) if isinstance(value, dict) else {}
        edges = ((value_data.get("jobListings") or {}).get("edges") or [])
        for edge in edges:
            node = (edge.get("node") or {}) if isinstance(edge, dict) else {}
            raw_id = normalize_text(node.get("id"))
            if not raw_id:
                continue
            hiring_org = node.get("hiringOrganization") or {}
            jobs.append(
                finalize_job(
                    {
                        "id": f"derstandard:{raw_id}",
                        "source": "derstandard",
                        "title": node.get("title"),
                        "company": hiring_org.get("name") if isinstance(hiring_org, dict) else None,
                        "location": ", ".join(node.get("jobLocationsRaw") or []) or "Wien",
                        "url": f"https://jobs.derstandard.at/job/{raw_id}",
                        "first_seen": fetched_at,
                    },
                    fetched_at,
                )
            )
    return dedupe_jobs(jobs), []


def _extract_karriere_initial_state(html: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    match = re.search(
        r"window\.VUE_INITIAL_STATE\s*=\s*(\{.*?\})\s*;\s*</script>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None, "karriere.at VUE_INITIAL_STATE not found"
    try:
        return json.loads(match.group(1)), None
    except json.JSONDecodeError as exc:
        return None, f"karriere.at VUE_INITIAL_STATE parse error: {exc}"


def parse_karriere_jobs_from_state(state: Dict[str, Any], fetched_at: Optional[str] = None) -> List[Dict[str, Any]]:
    fetched_at = fetched_at or utc_now()
    jobs_search_list = state.get("jobsSearchList") if isinstance(state, dict) else {}
    items = ((jobs_search_list or {}).get("activeItems") or {}).get("items") or []
    jobs: List[Dict[str, Any]] = []

    for item in items:
        raw = (item or {}).get("jobsItem") if isinstance(item, dict) else {}
        if not isinstance(raw, dict):
            continue

        raw_id = normalize_text(raw.get("id"))
        if not raw_id:
            continue

        locations = []
        for location in raw.get("locations") or []:
            if isinstance(location, dict):
                location_name = normalize_text(location.get("name"))
                if location_name:
                    locations.append(location_name)
        location_text = ", ".join(locations)
        if location_text and not _is_wien_location_text(location_text):
            continue

        company = raw.get("company") or {}
        jobs.append(
            finalize_job(
                {
                    "id": f"karriere_at:{raw_id}",
                    "source": "karriere_at",
                    "title": raw.get("title"),
                    "company": company.get("name") if isinstance(company, dict) else None,
                    "location": location_text or "Wien",
                    "url": _absolute_url("https://www.karriere.at/", raw.get("link") or f"/jobs/{raw_id}"),
                    "salary": raw.get("salary"),
                    "employment_type": raw.get("employmentTypes"),
                    "published_at": raw.get("date"),
                    "snippet": raw.get("snippet") or raw.get("summary"),
                    "first_seen": fetched_at,
                },
                fetched_at,
            )
        )

    return dedupe_jobs(jobs)


def _karriere_load_more_url(page: int) -> str:
    params = list(KARRIERE_LOAD_MORE_PARAMS) + [("page", str(page))]
    return f"{KARRIERE_LOAD_MORE_BASE_URL}?{urllib.parse.urlencode(params)}"


def fetch_karriere_at_jobs() -> FetchResult:
    warnings: List[str] = []
    all_jobs: List[Dict[str, Any]] = []
    fetched_at = utc_now()

    html, err = fetch_url(KARRIERE_SEARCH_URL)
    if err:
        return [], [f"karriere.at error: {err}"]
    if not html:
        return [], ["karriere.at returned an empty response"]

    state, state_error = _extract_karriere_initial_state(html)
    if state_error or not state:
        return [], [state_error or "karriere.at state missing"]

    all_jobs.extend(parse_karriere_jobs_from_state(state, fetched_at))
    load_more = ((state.get("jobsSearchList") or {}).get("loadMoreJobsButton") or {})
    next_page = load_more.get("next") if load_more.get("active") else None

    pages_seen = 1
    while next_page and pages_seen < MAX_PAGES_PER_SOURCE:
        try:
            page_number = int(next_page)
        except (TypeError, ValueError):
            warnings.append(f"karriere.at invalid next page marker: {next_page!r}")
            break

        data, page_err = fetch_url(
            _karriere_load_more_url(page_number),
            is_json=True,
            accept="application/json,text/plain,*/*",
            headers={
                "Referer": KARRIERE_SEARCH_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if page_err:
            warnings.append(f"karriere.at page {page_number} error: {page_err}")
            break

        page_state = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        if not isinstance(page_state, dict):
            warnings.append(f"karriere.at page {page_number} returned an unexpected payload")
            break

        page_jobs = parse_karriere_jobs_from_state(page_state, fetched_at)
        if not page_jobs:
            warnings.append(f"karriere.at page {page_number} contained no parseable jobs")
            break

        before_count = len(dedupe_jobs(all_jobs))
        all_jobs.extend(page_jobs)
        all_jobs = dedupe_jobs(all_jobs)
        after_count = len(all_jobs)

        load_more = ((page_state.get("jobsSearchList") or {}).get("loadMoreJobsButton") or {})
        next_page = load_more.get("next") if load_more.get("active") else None
        pages_seen += 1

        if after_count == before_count:
            warnings.append(f"karriere.at page {page_number} did not add new job IDs")
            break

    return dedupe_jobs(all_jobs), warnings


STEPSTONE_CARD_FIELDS = {
    "job-item-title",
    "job-item-company-name",
    "job-item-location",
    "job-item-timeago",
    "job-item-work-from-home",
    "job-item-badge",
    "job-item-top-label",
}

HTML_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class StepstoneJobCardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None
        self.stack: List[Tuple[str, Optional[str]]] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        data_at = attrs_dict.get("data-at")

        if tag in {"script", "style"}:
            self.skip_depth += 1

        if tag == "article" and data_at == "job-item":
            if self.current:
                self.cards.append(self.current)
            raw_id = ""
            id_match = re.search(r"job-item-(\d+)", attrs_dict.get("id", ""))
            if id_match:
                raw_id = id_match.group(1)
            self.current = {"id": raw_id, "fields": {}, "text": [], "url": ""}
            self.stack = []

        if not self.current:
            return

        if tag == "a":
            href = attrs_dict.get("href", "")
            if data_at == "job-item-title" or "/stellenangebote--" in href:
                self.current["url"] = href

        field = data_at if data_at in STEPSTONE_CARD_FIELDS else None
        if tag not in HTML_VOID_TAGS:
            self.stack.append((tag, field))

    def handle_endtag(self, tag: str) -> None:
        if self.current:
            match_index: Optional[int] = None
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == tag:
                    match_index = index
                    break
            if match_index is not None:
                del self.stack[match_index:]

            if tag == "article":
                self.cards.append(self.current)
                self.current = None
                self.stack = []

        if tag in {"script", "style"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.current or self.skip_depth:
            return
        text = data.strip()
        if not text:
            return

        self.current["text"].append(text)
        for _tag, field in reversed(self.stack):
            if field:
                self.current["fields"].setdefault(field, []).append(text)
                break


def _stepstone_field(card: Dict[str, Any], field_name: str) -> str:
    fields = card.get("fields") if isinstance(card, dict) else {}
    values = fields.get(field_name, []) if isinstance(fields, dict) else []
    if isinstance(values, list):
        return normalize_text(" ".join(str(value) for value in values))
    return normalize_text(values)


def _clean_stepstone_title(title: str) -> str:
    title = normalize_text(title)
    title = re.sub(r"\s+[|–-]\s*(?:wien|vienna|1[0-2]\d{2}\s+wien)\s*$", "", title, flags=re.IGNORECASE)
    return normalize_text(title)


def _has_vienna_postcode(text: Any) -> bool:
    for match in re.finditer(r"\b(1[0-2]\d{2})\b", normalize_text(text)):
        try:
            postcode = int(match.group(1))
        except ValueError:
            continue
        if 1010 <= postcode <= 1230 and postcode % 10 == 0:
            return True
    return False


def _is_stepstone_wien_location(location: Any) -> bool:
    location_text = normalize_text(location)
    if not location_text:
        return False
    if _has_vienna_postcode(location_text):
        return True

    parts = set(slugify(location_text).split("-"))
    if "vienna" in parts:
        return True
    if "wien" not in parts:
        return False
    if parts & {"umgebung", "umland", "neustadt", "neudorf"}:
        return False
    return True


def _extract_stepstone_salary(text: str) -> Optional[str]:
    match = re.search(
        r"((?:€|EUR)\s*[0-9][0-9.\s,]*(?:\s*(?:bis|-|–)\s*(?:€|EUR)?\s*[0-9][0-9.\s,]*)?"
        r"(?:\s*(?:brutto|jährlich|monatlich|p\.a\.|pro\s+jahr))?)",
        normalize_text(text),
        re.IGNORECASE,
    )
    return normalize_text(match.group(1)) if match else None


def _extract_stepstone_employment_type(text: str) -> Optional[str]:
    types = []
    normalized = normalize_text(text)
    for pattern, label in (
        (r"\bVollzeit\b", "Vollzeit"),
        (r"\bTeilzeit\b", "Teilzeit"),
        (r"\bHome Office\b|\bHome-Office\b", "Home Office"),
        (r"\bFeste Anstellung\b", "Feste Anstellung"),
        (r"\bBefristeter Vertrag\b", "Befristeter Vertrag"),
        (r"\bPraktikum\b", "Praktikum"),
    ):
        if re.search(pattern, normalized, re.IGNORECASE) and label not in types:
            types.append(label)
    return ", ".join(types) or None


def _extract_stepstone_expected_count(html: str) -> Optional[int]:
    text = normalize_text(html)
    patterns = (
        r"Aktuell\s+gibt\s+es\s+.*?\b(\d+)\s+offene\s+Stellenanzeigen",
        r"\b(\d+)\s+offene\s+Stellenanzeigen",
        r"\b(\d+)\s+Stellenangebote",
        r"\b(\d+)\s+Jobs?\s+in\s+Wien",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def _extract_stepstone_next_url(html: str) -> Optional[str]:
    for link_match in re.finditer(r"<link\b[^>]*>", html, re.IGNORECASE):
        tag = link_match.group(0)
        if not re.search(r'rel=["\']next["\']', tag, re.IGNORECASE):
            continue
        href_match = re.search(r'href=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if href_match:
            return _absolute_url(STEPSTONE_BASE_URL, href_match.group(1))
    return None


def parse_stepstone_jobs_from_html(html: str, fetched_at: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    fetched_at = fetched_at or utc_now()
    parser = StepstoneJobCardParser()
    parser.feed(html or "")

    warnings: List[str] = []
    jobs: List[Dict[str, Any]] = []
    skipped_non_wien = 0
    for card in parser.cards:
        raw_id = normalize_text(card.get("id"))
        url = _absolute_url(STEPSTONE_BASE_URL, normalize_text(card.get("url")))
        if not raw_id and not url:
            continue

        title = _clean_stepstone_title(_stepstone_field(card, "job-item-title"))
        company = _stepstone_field(card, "job-item-company-name")
        location = _stepstone_field(card, "job-item-location")
        if location and not _is_stepstone_wien_location(location):
            skipped_non_wien += 1
            continue

        all_text = normalize_text(" ".join(str(item) for item in card.get("text", [])))
        homeoffice = _stepstone_field(card, "job-item-work-from-home")
        badge = _stepstone_field(card, "job-item-badge")
        top_label = _stepstone_field(card, "job-item-top-label")
        labels = ", ".join(label for label in (homeoffice, badge, top_label) if label)

        jobs.append(
            finalize_job(
                {
                    "id": f"stepstone:{raw_id}" if raw_id else "",
                    "source": "stepstone",
                    "title": title,
                    "company": company,
                    "location": location or "Wien",
                    "url": url,
                    "salary": _extract_stepstone_salary(all_text),
                    "employment_type": _extract_stepstone_employment_type(all_text),
                    "published_at": _stepstone_field(card, "job-item-timeago"),
                    "department": labels or None,
                    "snippet": all_text,
                    "first_seen": fetched_at,
                },
                fetched_at,
            )
        )

    if skipped_non_wien:
        warnings.append(f"StepStone skipped {skipped_non_wien} non-Wien job card(s)")
    return dedupe_jobs(jobs), warnings


def fetch_stepstone_page(url: str) -> Tuple[Optional[str], Optional[str]]:
    last_error: Optional[str] = None
    for attempt in range(1, STEPSTONE_FETCH_ATTEMPTS + 1):
        html, err = fetch_url(url, timeout_seconds=STEPSTONE_TIMEOUT_SECONDS)
        if not err and html:
            return html, None
        last_error = err or "empty response"
        if attempt < STEPSTONE_FETCH_ATTEMPTS:
            print(f"[jobs][warn] StepStone fetch attempt {attempt} failed, retrying: {last_error}")

    print(f"[jobs][warn] StepStone urllib fetch failed, trying curl fallback: {last_error}")
    html, curl_err = fetch_url_with_curl(
        url,
        timeout_seconds=STEPSTONE_CURL_TIMEOUT_SECONDS,
    )
    if not curl_err and html:
        return html, None
    return None, f"{last_error}; curl fallback: {curl_err or 'empty response'}"


def fetch_stepstone_jobs() -> FetchResult:
    fetched_at = utc_now()
    warnings: List[str] = []
    jobs: List[Dict[str, Any]] = []
    seen_page_urls: set[str] = set()
    url: Optional[str] = STEPSTONE_SEARCH_URL
    expected_count: Optional[int] = None

    for _page in range(MAX_PAGES_PER_SOURCE):
        if not url or url in seen_page_urls:
            break
        seen_page_urls.add(url)

        html, err = fetch_stepstone_page(url)
        if err:
            if jobs:
                return jobs, [f"StepStone pagination incomplete after {len(jobs)} job(s): {err}"]
            return [], [f"StepStone fetch error: {err}"]
        if not html:
            return jobs, [f"StepStone returned an empty page after {len(jobs)} job(s)"]

        if expected_count is None:
            expected_count = _extract_stepstone_expected_count(html)

        page_jobs, page_warnings = parse_stepstone_jobs_from_html(html, fetched_at)
        warnings.extend(page_warnings)

        before_count = len(dedupe_jobs(jobs))
        jobs.extend(page_jobs)
        jobs = dedupe_jobs(jobs)
        after_count = len(jobs)

        next_url = _extract_stepstone_next_url(html)
        if not next_url:
            break
        if after_count == before_count:
            return jobs, [f"StepStone pagination page did not add new job IDs: {url}"]
        url = next_url

    if expected_count is not None and len(jobs) != expected_count:
        warnings.append(
            f"StepStone expected {expected_count} result(s) but retained {len(jobs)} Wien job card(s)"
        )
    for warning in warnings:
        print(f"[jobs][warn] {warning}")
    if expected_count and not jobs:
        return [], [f"StepStone expected {expected_count} result(s) but parsed none"]
    return dedupe_jobs(jobs), []


def build_source_registry(fetchers: Optional[Dict[str, Fetcher]] = None) -> Dict[str, SourceConfig]:
    registry = {
        "jusjobs": SourceConfig("jusjobs", "JusJobs", fetch_jusjobs_jobs, True),
        "erste_bank": SourceConfig("erste_bank", "Erste Bank / Sparkasse", fetch_erste_bank_jobs, True),
        "uniqa": SourceConfig("uniqa", "UNIQA", fetch_uniqa_jobs, True),
        "lawfinder": SourceConfig("lawfinder", "LawFinder", fetch_lawfinder_jobs, True),
        "derstandard": SourceConfig("derstandard", "DER STANDARD Jobs", fetch_derstandard_jobs, True),
        "karriere_at": SourceConfig("karriere_at", "karriere.at", fetch_karriere_at_jobs, True),
        "stepstone": SourceConfig("stepstone", "StepStone", fetch_stepstone_jobs, True),
    }
    if fetchers:
        for name, fetcher in fetchers.items():
            current = registry.get(name)
            registry[name] = SourceConfig(
                name=name,
                label=current.label if current else name,
                fetcher=fetcher,
                default_enabled=current.default_enabled if current else True,
            )
    return registry


def get_enabled_source_names(explicit: Optional[Iterable[str]] = None) -> List[str]:
    if explicit:
        return [normalize_text(name) for name in explicit if normalize_text(name)]

    raw = os.environ.get("JOB_TRACKER_SOURCES")
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]

    return list(DEFAULT_SOURCE_NAMES)


def get_snapshot_path(name: str = JOB_SNAPSHOT_NAME, snapshot_dir: str = SNAPSHOTS_DIR) -> str:
    return os.path.join(snapshot_dir, f"{name}.json")


def load_snapshot(name: str = JOB_SNAPSHOT_NAME, snapshot_dir: str = SNAPSHOTS_DIR) -> Optional[Dict[str, Any]]:
    path = get_snapshot_path(name, snapshot_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"Could not load snapshot {path}: {exc}") from exc


def save_snapshot(
    name: str,
    data: Dict[str, Any],
    snapshot_dir: str = SNAPSHOTS_DIR,
) -> None:
    os.makedirs(snapshot_dir, exist_ok=True)
    path = get_snapshot_path(name, snapshot_dir)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary_path, path)
    print(f"[jobs] Saved snapshot: {path}")


def compare_new_jobs(old_jobs: Iterable[Dict[str, Any]], current_jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    old_ids = {normalize_text(job.get("id")) for job in old_jobs if normalize_text(job.get("id"))}
    return [job for job in current_jobs if normalize_text(job.get("id")) not in old_ids]


def build_snapshot_data(
    jobs: List[Dict[str, Any]],
    sources_status: Dict[str, Dict[str, Any]],
    timestamp: str,
    *,
    seen_ids: Iterable[str],
    pending_jobs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "timestamp": timestamp,
        "data": {
            "schema_version": 3,
            "sources": sources_status,
            "jobs": jobs,
            "seen_ids": sorted({normalize_text(job_id) for job_id in seen_ids if normalize_text(job_id)}),
            "pending_jobs": pending_jobs,
        },
    }


def run_job_tracker(
    *,
    source_names: Optional[Iterable[str]] = None,
    snapshot_dir: str = SNAPSHOTS_DIR,
    dry_run: bool = False,
    notify: bool = True,
    webhook_url: Optional[str] = None,
    fetchers: Optional[Dict[str, Fetcher]] = None,
) -> JobRunResult:
    registry = build_source_registry(fetchers)
    enabled_names = get_enabled_source_names(source_names)
    unknown_names = [name for name in enabled_names if name not in registry]
    if unknown_names:
        raise ValueError(f"Unknown job tracker source(s): {', '.join(unknown_names)}")

    old_snapshot = load_snapshot(JOB_SNAPSHOT_NAME, snapshot_dir)
    first_run = old_snapshot is None
    old_data = old_snapshot.get("data", {}) if isinstance(old_snapshot, dict) else {}
    old_jobs_value = old_data.get("jobs", []) if isinstance(old_data, dict) else []
    old_jobs = [job for job in old_jobs_value if isinstance(job, dict)] if isinstance(old_jobs_value, list) else []
    old_sources_value = old_data.get("sources", {}) if isinstance(old_data, dict) else {}
    old_sources = old_sources_value if isinstance(old_sources_value, dict) else {}
    old_pending_value = old_data.get("pending_jobs", []) if isinstance(old_data, dict) else []
    old_pending_jobs = [
        job for job in old_pending_value if isinstance(job, dict)
    ] if isinstance(old_pending_value, list) else []
    old_seen_value = old_data.get("seen_ids", []) if isinstance(old_data, dict) else []
    try:
        old_schema_version = int(old_data.get("schema_version") or 0) if isinstance(old_data, dict) else 0
    except (TypeError, ValueError):
        old_schema_version = 0
    seen_ids = {
        normalize_text(job_id)
        for job_id in old_seen_value
        if normalize_text(job_id)
    } if isinstance(old_seen_value, list) else set()
    seen_ids.update(
        normalize_text(job.get("id"))
        for job in old_jobs
        if normalize_text(job.get("id"))
    )
    seen_ids.update(
        normalize_text(job.get("id"))
        for job in old_pending_jobs
        if normalize_text(job.get("id"))
    )

    timestamp = utc_now()
    enabled_set = set(enabled_names)
    all_jobs: List[Dict[str, Any]] = [
        dict(job)
        for job in old_jobs
        if normalize_text(job.get("source")) not in enabled_set
    ]
    sources_status: Dict[str, Dict[str, Any]] = {
        normalize_text(source_name): dict(status)
        for source_name, status in old_sources.items()
        if normalize_text(source_name) not in enabled_set and isinstance(status, dict)
    }
    run_errors: List[str] = []

    for source_name in enabled_names:
        source = registry[source_name]
        old_source_jobs = [job for job in old_jobs if job.get("source") == source_name]
        old_source_status = old_sources.get(source_name) or {}
        print(f"[jobs] Fetching source: {source.label} ({source.name})")
        try:
            jobs, errors = source.fetcher()
        except Exception as exc:
            jobs, errors = [], [f"{type(exc).__name__}: {exc}"]

        previous_count = old_source_status.get("count") if isinstance(old_source_status, dict) else 0
        try:
            previous_count = int(previous_count or 0)
        except (TypeError, ValueError):
            previous_count = len(old_source_jobs)
        if not errors and not jobs and (old_source_jobs or previous_count > 0):
            errors = [
                f"{source.label} returned 0 jobs after previously returning "
                f"{max(previous_count, len(old_source_jobs))}; preserving the prior baseline"
            ]

        if errors:
            all_jobs.extend(old_source_jobs)
            sources_status[source_name] = {
                "label": source.label,
                "last_success": old_source_status.get("last_success") if isinstance(old_source_status, dict) else None,
                "last_error": errors[0],
                "count": len(old_source_jobs),
            }
            run_errors.append(f"{source.label}: {errors[0]}")
            print(f"[jobs] {source.label} failed: {errors[0]}")
            continue

        normalized_jobs = [
            finalize_job(job, timestamp)
            for job in jobs
            if isinstance(job, dict)
        ]
        all_jobs.extend(normalized_jobs)
        sources_status[source_name] = {
            "label": source.label,
            "last_success": timestamp,
            "last_error": None,
            "count": len(normalized_jobs),
        }
        print(f"[jobs] {source.label}: {len(normalized_jobs)} job(s)")

    all_jobs = dedupe_cross_source_jobs(dedupe_jobs(all_jobs))
    old_by_id = {
        normalize_text(job.get("id")): job
        for job in old_jobs
        if normalize_text(job.get("id"))
    }
    for job in all_jobs:
        old_job = old_by_id.get(normalize_text(job.get("id")))
        if not old_job:
            old_job = find_cross_source_duplicate(job, old_jobs + old_pending_jobs)
        if old_job:
            job["first_seen"] = old_job.get("first_seen") or job.get("first_seen") or timestamp

    if first_run:
        new_jobs = []
        seen_ids.update(
            normalize_text(job.get("id"))
            for job in all_jobs
            if normalize_text(job.get("id"))
        )
    else:
        known_sources = {
            normalize_text(source_name)
            for source_name, status in old_sources.items()
            if isinstance(status, dict) and normalize_text(status.get("last_success"))
            if normalize_text(source_name)
        }
        known_sources.update(
            normalize_text(job.get("source"))
            for job in old_jobs
            if normalize_text(job.get("source"))
        )
        migration_baseline_sources = {
            normalize_text(source_name)
            for source_name, status in old_sources.items()
            if old_schema_version < 3
            and isinstance(status, dict)
            and not status.get("count")
            and not any(job.get("source") == source_name for job in old_jobs)
            and normalize_text(source_name)
        }
        unseen_jobs = [
            job
            for job in all_jobs
            if normalize_text(job.get("id"))
            and normalize_text(job.get("id")) not in seen_ids
        ]
        detected_new_jobs = [
            job
            for job in unseen_jobs
            if not find_cross_source_duplicate(job, old_jobs + old_pending_jobs)
        ]
        new_jobs = [
            job
            for job in detected_new_jobs
            if normalize_text(job.get("source")) in known_sources
            and normalize_text(job.get("source")) not in migration_baseline_sources
        ]
        suppressed_new_source_jobs = len(detected_new_jobs) - len(new_jobs)
        if suppressed_new_source_jobs:
            print(
                "[jobs] Baseline only for newly added source(s): "
                f"{suppressed_new_source_jobs} existing job(s) not alerted"
            )
        seen_ids.update(
            normalize_text(job.get("id"))
            for job in unseen_jobs
            if normalize_text(job.get("id"))
        )

    pending_by_id: Dict[str, Dict[str, Any]] = {}
    for index, job in enumerate(old_pending_jobs + new_jobs):
        pending_id = normalize_text(job.get("id")) or normalize_text(job.get("url")) or f"pending:{index}"
        pending_by_id[pending_id] = dict(job)
    pending_jobs = list(pending_by_id.values())

    if first_run:
        print("[jobs] First run: baseline only, no alerts sent")
        pending_jobs = []
    else:
        if new_jobs:
            print(f"[jobs] New jobs detected: {len(new_jobs)}")
        if pending_jobs and notify and not dry_run:
            print(f"[jobs] Delivering {len(pending_jobs)} pending/new job notification(s)")
            target_webhook = webhook_url or JOB_DISCORD_WEBHOOK_URL
            if target_webhook:
                try:
                    delivery = deliver_new_jobs_notification(target_webhook, pending_jobs)
                    failed_ids = set(delivery.failed_job_ids)
                    pending_jobs = [
                        job
                        for index, job in enumerate(pending_jobs)
                        if normalize_text(job.get("id")) in failed_ids
                        or (
                            not normalize_text(job.get("id"))
                            and normalize_text(job.get("url")) in failed_ids
                        )
                        or (
                            not normalize_text(job.get("id"))
                            and not normalize_text(job.get("url"))
                            and f"delivery:{index}" in failed_ids
                        )
                    ]
                    if pending_jobs:
                        run_errors.append(
                            f"Discord delivery failed for {len(pending_jobs)} job notification(s)"
                        )
                except Exception as exc:
                    run_errors.append(f"Discord delivery error: {type(exc).__name__}: {exc}")
            else:
                run_errors.append(
                    f"No job Discord webhook configured; {len(pending_jobs)} notification(s) remain pending"
                )
        elif pending_jobs and not notify and not dry_run:
            print(f"[jobs] Notifications disabled; queued {len(pending_jobs)} job(s) for a later run")
    snapshot = build_snapshot_data(
        all_jobs,
        sources_status,
        timestamp,
        seen_ids=seen_ids,
        pending_jobs=pending_jobs,
    )

    if dry_run:
        print("[jobs] Dry run: snapshot not saved and Discord not notified")
    else:
        save_snapshot(JOB_SNAPSHOT_NAME, snapshot, snapshot_dir)

    if not first_run and not new_jobs and not pending_jobs:
        print("[jobs] No new jobs found")

    return JobRunResult(
        first_run=first_run,
        new_jobs=new_jobs,
        all_jobs=all_jobs,
        snapshot=snapshot,
        sources=sources_status,
        pending_jobs=pending_jobs,
        run_errors=run_errors,
    )


def track_jobs() -> bool:
    if not JOB_TRACKER_ENABLED:
        print("[jobs] Job tracker is disabled. Set JOB_TRACKER_ENABLED=1 to enable it.")
        return False
    result = run_job_tracker(dry_run=False, notify=True)
    if not result.healthy:
        raise RuntimeError("; ".join(result.run_errors))
    return bool(result.new_jobs)


def _print_run_summary(result: JobRunResult) -> None:
    print("\n[jobs] Summary")
    for source_name, status in result.sources.items():
        label = status.get("label") or source_name
        error = status.get("last_error")
        suffix = f" error={error}" if error else ""
        print(f"  - {label}: {status.get('count', 0)} job(s){suffix}")
    print(f"  Total jobs: {len(result.all_jobs)}")
    print(f"  New jobs: {len(result.new_jobs)}")
    print(f"  Pending notifications: {len(result.pending_jobs)}")
    if result.run_errors:
        print("  Health errors:")
        for error in result.run_errors:
            print(f"    ! {error}")
    for job in result.new_jobs[:10]:
        print(f"    + {job.get('title')} | {job.get('company')} | {job.get('url')}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the legal job tracker")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and compare without saving snapshots or notifying Discord")
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Save snapshots and queue new notifications for a later normal run",
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=sorted(DEFAULT_SOURCE_NAMES + OPTIONAL_SOURCE_NAMES),
        help="Run only the named source. Can be passed multiple times.",
    )
    parser.add_argument("--test-notify", action="store_true", help="Send a fake job notification to the configured webhook")
    args = parser.parse_args(argv)

    if args.test_notify:
        target_webhook = JOB_DISCORD_WEBHOOK_URL
        if not target_webhook:
            print("[jobs] JOB_DISCORD_WEBHOOK_URL or DISCORD_WEBHOOK_URL is not configured")
            return 1
        fake_job = finalize_job(
            {
                "id": "test:job",
                "source": "test",
                "title": "Test Job Notification",
                "company": "Codex",
                "location": "Wien",
                "url": "https://example.com/job",
                "salary": "Test",
            },
            utc_now(),
        )
        return 0 if send_new_jobs_notification(target_webhook, [fake_job]) else 1

    result = run_job_tracker(
        source_names=args.source,
        dry_run=args.dry_run,
        notify=not args.no_notify,
    )
    _print_run_summary(result)
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
