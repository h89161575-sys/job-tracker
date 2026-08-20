import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from job_tracker import (  # noqa: E402
    JOB_SNAPSHOT_NAME,
    compare_new_jobs,
    decode_response_body,
    dedupe_cross_source_jobs,
    filter_erste_bank_jobs,
    find_cross_source_duplicate,
    job_fingerprint,
    normalize_text,
    parse_lawfinder_jobs_from_html,
    parse_karriere_jobs_from_state,
    parse_jusjobs_jobs_from_html,
    parse_stepstone_jobs_from_html,
    parse_uniqa_rss_jobs,
    run_job_tracker,
    save_snapshot,
    slugify,
)
import notifier  # noqa: E402
import job_tracker as job_tracker_module  # noqa: E402


def assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(value, label):
    if not value:
        raise AssertionError(label)


def sample_job(job_id, title="Jurist:in", source="jusjobs"):
    return {
        "id": f"{source}:{job_id}",
        "source": source,
        "title": title,
        "company": "Test GmbH",
        "location": "Wien",
        "url": f"https://example.com/jobs/{job_id}",
        "first_seen": "2026-06-18T00:00:00Z",
    }


def test_normalization():
    assert_equal(normalize_text("<b> Legal&nbsp; Counsel </b>"), "Legal Counsel", "normalize_text")
    assert_equal(normalize_text("\u00c3\u201e\u0072ztekammer f\u00c3\u00bc\u0072 Wien \u00e2\u201a\u00ac 3.500"), "\u00c4rztekammer f\u00fcr Wien \u20ac 3.500", "mojibake repair")
    assert_equal(slugify("Jurist:in für Vertrags- & Gesellschaftsrecht"), "jurist-in-fuer-vertrags-gesellschaftsrecht", "slugify")
    assert_equal(
        decode_response_body("Rechtsanwaltsanw\u00e4rter:in \u2013 \u20ac".encode("cp1252"), "utf-8"),
        "Rechtsanwaltsanw\u00e4rter:in \u2013 \u20ac",
        "decode cp1252 fallback",
    )
    first = job_fingerprint(sample_job("1"))
    second = job_fingerprint(sample_job("1"))
    assert_equal(first, second, "stable fingerprint")


def test_jusjobs_parser():
    html = """
    <div id="jobSearchResults">
      <span id="number">2</span>
      <div class="row jobResult">
        <div class="job-card__content">
          <h4><a href="/job/1507775">Legal Counsel</a></h4>
          <h6 class="jobCompanyName"><a href="/arbeitgeber/test">Test GmbH</a></h6>
          <p class="jobInfo">Ein kurzer Beschreibungstext.</p>
          <span>€ 3.500</span><span>Wien</span><span>Vollzeit</span>
        </div>
      </div>
      <div class="row jobResult">
        <div class="job-card__content">
          <h4><a href="/job/1774919">Praktikum Recht</a></h4>
          <h6 class="jobCompanyName"><a href="/arbeitgeber/zweite">Zweite GmbH</a></h6>
        </div>
      </div>
    </div>
    <div class="col p-0 whiteBg"></div>
    """
    jobs, warnings = parse_jusjobs_jobs_from_html(html, "2026-06-18T00:00:00Z")
    assert_equal(warnings, [], "jusjobs warnings")
    assert_equal(len(jobs), 2, "jusjobs job count")
    assert_equal(jobs[0]["id"], "jusjobs:1507775", "jusjobs id")
    assert_equal(jobs[0]["salary"], "€ 3.500", "jusjobs salary")


def test_jusjobs_stale_advertised_count_is_only_a_warning():
    html = """
    <div id="jobSearchResults">
      <div class="jobAgentHint whiteBg">Job-Agent aktivieren</div>
      <span id="number">2</span>
      <div class="row jobResult" id="jobResult-1507775">
        <h4><a href="/job/1507775">Legal Counsel</a></h4>
      </div>
      <div class="row jobResult gjt-ctaRow">Weitere Treffer nach dem Login</div>
    </div>
    <div class="col p-0 whiteBg" id="ExtendedJobResultsFallBack"></div>
    """
    original_fetch = job_tracker_module.fetch_url
    calls = []
    try:
        job_tracker_module.fetch_url = lambda url: (calls.append(url) or html, None)
        jobs, errors = job_tracker_module.fetch_jusjobs_jobs()
    finally:
        job_tracker_module.fetch_url = original_fetch

    assert_equal(errors, [], "stale JusJobs result count is not a parser failure")
    assert_equal([job["id"] for job in jobs], ["jusjobs:1507775"], "rendered JusJobs card retained")
    assert_equal(len(calls), 2, "JusJobs pagination checks for the advertised missing result")


def test_jusjobs_unparsed_rendered_card_is_an_error():
    html = """
    <div id="jobSearchResults">
      <div class="jobAgentHint whiteBg">Job-Agent aktivieren</div>
      <span id="number">2</span>
      <div class="row jobResult" id="jobResult-1507775">
        <h4><a href="/job/1507775">Legal Counsel</a></h4>
      </div>
      <div class="row jobResult" id="jobResult-1507776"><h4>Broken result without a link</h4></div>
      <div class="row jobResult gjt-ctaRow">Weitere Treffer nach dem Login</div>
    </div>
    <div class="col p-0 whiteBg" id="ExtendedJobResultsFallBack"></div>
    """
    original_fetch = job_tracker_module.fetch_url
    try:
        job_tracker_module.fetch_url = lambda _url: (html, None)
        jobs, errors = job_tracker_module.fetch_jusjobs_jobs()
    finally:
        job_tracker_module.fetch_url = original_fetch

    assert_equal([job["id"] for job in jobs], [], "partial JusJobs page is not accepted")
    assert_true(any("rendered 2 result card" in error for error in errors), "missing parsed card reported")


def test_erste_bank_filter():
    raw_jobs = [
        {
            "id": "1",
            "external_title": "Jurist:in",
            "legal_entity_name": "Erste Bank",
            "location": ["Wien"],
            "discipline_items": ["Legal / Compliance / Audit"],
            "posting_date": "18.06.2026",
        },
        {
            "id": "2",
            "external_title": "Jurist:in Salzburg",
            "legal_entity_name": "Erste Bank",
            "location": ["Salzburg"],
            "discipline_items": ["Legal / Compliance / Audit"],
        },
        {
            "id": "3",
            "external_title": "Audit in Wiener Neustadt",
            "legal_entity_name": "Erste Bank",
            "location": ["Wiener Neustadt"],
            "discipline_items": ["Legal / Compliance / Audit"],
        },
        {
            "id": "4",
            "external_title": "Group Audit Specialist",
            "legal_entity_name": "Erste Bank",
            "location": ["Wien"],
            "discipline_items": ["Audit"],
        },
        {
            "id": "5",
            "external_title": "IT Specialist",
            "legal_entity_name": "Erste Bank",
            "location": ["Wien"],
            "discipline_items": ["IT"],
        },
    ]
    jobs = filter_erste_bank_jobs(raw_jobs, "2026-06-18T00:00:00Z")
    assert_equal([job["id"] for job in jobs], ["erste_bank:1", "erste_bank:4"], "erste filter ids")


def test_erste_bank_catalog_validation():
    original_discovery = job_tracker_module.discover_sparkasse_joblist_api
    original_fetch = job_tracker_module.fetch_url
    try:
        job_tracker_module.discover_sparkasse_joblist_api = lambda: (
            "https://example.invalid/jobs",
            {},
            "/job-detail",
        )
        job_tracker_module.fetch_url = lambda _url, **_kwargs: ({"data": []}, None)
        _jobs, empty_errors = job_tracker_module.fetch_erste_bank_jobs()

        job_tracker_module.fetch_url = lambda _url, **_kwargs: (
            {"data": [{"id": "1", "external_title": "Unexpected schema"}]},
            None,
        )
        _jobs, schema_errors = job_tracker_module.fetch_erste_bank_jobs()

        job_tracker_module.fetch_url = lambda _url, **_kwargs: (
            {
                "data": [
                    {
                        "id": "2",
                        "external_title": "Internal Revision",
                        "location": ["Wien"],
                        "discipline_items": ["Internal Revision"],
                    },
                    {
                        "id": "3",
                        "external_title": "Engineering",
                        "location": ["Graz"],
                        "discipline_items": ["Engineering"],
                    },
                ]
            },
            None,
        )
        _jobs, taxonomy_errors = job_tracker_module.fetch_erste_bank_jobs()

        job_tracker_module.fetch_url = lambda _url, **_kwargs: (
            {
                "data": [
                    {
                        "external_title": "Audit Specialist without ID",
                        "location": ["Wien"],
                        "discipline_items": ["Audit"],
                    }
                ]
            },
            None,
        )
        _jobs, missing_id_errors = job_tracker_module.fetch_erste_bank_jobs()

        job_tracker_module.fetch_url = lambda _url, **_kwargs: (
            {
                "data": [
                    {
                        "id": "4",
                        "external_title": "IT Specialist",
                        "location": ["Wien"],
                        "discipline_items": ["IT"],
                    },
                    {
                        "id": "5",
                        "external_title": "Audit Specialist",
                        "location": ["Graz"],
                        "discipline": ["Audit"],
                    },
                ]
            },
            None,
        )
        legitimate_empty_jobs, legitimate_empty_errors = (
            job_tracker_module.fetch_erste_bank_jobs()
        )
    finally:
        job_tracker_module.discover_sparkasse_joblist_api = original_discovery
        job_tracker_module.fetch_url = original_fetch

    assert_true(any("no raw jobs" in error for error in empty_errors), "empty Erste catalog rejected")
    assert_true(any("missing location or discipline" in error for error in schema_errors), "Erste schema drift rejected")
    assert_true(
        any("no longer exposes" in error for error in taxonomy_errors),
        "Erste taxonomy drift rejected",
    )
    assert_true(
        any("without an ID" in error for error in missing_id_errors),
        "matching Erste row without stable ID rejected",
    )
    assert_equal(legitimate_empty_jobs, [], "valid empty Erste result has no jobs")
    assert_equal(legitimate_empty_errors, [], "valid empty Erste result is authoritative")


def test_uniqa_rss_filter():
    rss = """<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0"><channel>
      <item>
        <title><![CDATA[Schadenreferent:in/ Jurist:in Haftpflichtschaden (Salzburg, AT)]]></title>
        <link>https://careers.uniqagroup.com/job/Salzburg-Test/1274718701/</link>
        <description><![CDATA[Standort Salzburg]]></description>
      </item>
      <item>
        <title><![CDATA[Jurist:in Datenschutz (Wien, AT)]]></title>
        <link>https://careers.uniqagroup.com/job/Wien-Test/999/</link>
        <description><![CDATA[Standort Wien]]></description>
      </item>
    </channel></rss>
    """
    jobs = parse_uniqa_rss_jobs(rss, "2026-06-18T00:00:00Z")
    assert_equal(len(jobs), 1, "uniqa wien-only count")
    assert_equal(jobs[0]["id"], "uniqa:999", "uniqa id")


def test_karriere_at_state_filter():
    state = {
        "jobsSearchList": {
            "activeItems": {
                "items": [
                    {
                        "jobsItem": {
                            "id": "10021048",
                            "link": "https://www.karriere.at/jobs/10021048",
                            "title": "Jurist*in",
                            "company": {"name": "Test AG"},
                            "locations": [{"name": "Wien"}],
                            "employmentTypes": "Vollzeit",
                            "salary": "ab 43.400 € jährlich",
                            "date": "Heute veröffentlicht",
                            "snippet": "Legal role",
                        }
                    },
                    {
                        "jobsItem": {
                            "id": "999",
                            "link": "https://www.karriere.at/jobs/999",
                            "title": "Jurist*in Salzburg",
                            "company": {"name": "Andere AG"},
                            "locations": [{"name": "Salzburg"}],
                        }
                    },
                    {
                        "jobsItem": {
                            "id": "1000",
                            "link": "https://www.karriere.at/jobs/1000",
                            "title": "Jurist*in Wiener Neustadt",
                            "company": {"name": "Andere AG"},
                            "locations": [{"name": "Wiener Neustadt"}],
                        }
                    },
                ]
            }
        }
    }
    jobs = parse_karriere_jobs_from_state(state, "2026-06-18T00:00:00Z")
    assert_equal(len(jobs), 1, "karriere.at wien-only count")
    assert_equal(jobs[0]["id"], "karriere_at:10021048", "karriere.at id")
    assert_equal(jobs[0]["salary"], "ab 43.400 € jährlich", "karriere.at salary")


def test_stepstone_html_filter():
    html = """
    <html>
      <head>
        <link rel="next" href="https://www.stepstone.at/jobs/jurist/in-wien?page=2" />
      </head>
      <body>
        <article id="job-item-633580" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Unternehmensjuristen-Inhouse-Legal-m-w-D-Wien-Landstrasse--633580-inline.html">
            <span>Unternehmensjuristen/Inhouse Legal (m/w/d) | Wien</span>
          </a>
          <span data-at="job-item-company-name">LBG Österreich GmbH</span>
          <span data-at="job-item-location">Wien-Landstraße</span>
          <span data-at="job-item-work-from-home">Teilweise Home-Office</span>
          <span data-at="job-item-badge">Schnelle Bewerbung</span>
          <span data-at="job-item-timeago"><time>vor 20 Stunden</time></span>
          <p>Feste Anstellung, Vollzeit, EUR 4.000 brutto</p>
        </article>
        <article id="job-item-1030" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Jurist-in-Wien--1030-inline.html">
            <span>Jurist:in Datenschutz</span>
          </a>
          <span data-at="job-item-company-name">Test AG</span>
          <span data-at="job-item-location">Guglgasse 7-9, Wien, 1030</span>
          <span data-at="job-item-timeago">vor 1 Tag</span>
          <p>Teilzeit, Vollzeit</p>
        </article>
        <article id="job-item-2700" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Legal-Counsel-Wiener-Neustadt--2700-inline.html">
            <span>Legal Counsel</span>
          </a>
          <span data-at="job-item-company-name">Andere AG</span>
          <span data-at="job-item-location">Wiener Neustadt</span>
        </article>
        <article id="job-item-9999" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Legal-Counsel-Wien-Umgebung--9999-inline.html">
            <span>Legal Counsel</span>
          </a>
          <span data-at="job-item-company-name">Umland AG</span>
          <span data-at="job-item-location">Wien Umgebung</span>
        </article>
      </body>
    </html>
    """
    jobs, warnings = parse_stepstone_jobs_from_html(html, "2026-06-18T00:00:00Z")
    assert_equal([job["id"] for job in jobs], ["stepstone:633580", "stepstone:1030"], "stepstone wien-only ids")
    assert_equal(jobs[0]["title"], "Unternehmensjuristen/Inhouse Legal (m/w/d)", "stepstone title cleanup")
    assert_equal(jobs[0]["location"], "Wien-Landstraße", "stepstone district location")
    assert_equal(jobs[0]["salary"], "EUR 4.000 brutto", "stepstone salary")
    assert_true("Schnelle Bewerbung" in (jobs[0].get("department") or ""), "stepstone labels")
    assert_true(warnings and "non-Wien" in warnings[0], "stepstone non-wien warning")


def test_stepstone_api_filter():
    payload = {
        "items": [
            {
                "id": 1001,
                "section": "main",
                "title": "Legal Counsel | Wien",
                "companyName": "Erste Firma GmbH",
                "location": "Wien",
                "url": "/stellenangebote--Legal-Counsel-Wien--1001-inline.html?rltr=tracking",
                "datePosted": "2026-08-20T08:00:00+02:00",
                "salary": "EUR 4.000 brutto",
                "workFromHome": "2",
                "labels": [{"text": "Vollzeit"}],
                "topLabels": ["Schnelle Bewerbung"],
                "textSnippet": "Juristische Beratung",
            },
            {
                "id": 1002,
                "section": "semantic",
                "title": "Compliance Jurist:in",
                "companyName": "Zweite Firma AG",
                "location": "1030 Wien",
                "url": "/stellenangebote--Compliance-Jurist-Wien--1002-inline.html",
                "datePosted": "2026-08-19T08:00:00+02:00",
                "salary": None,
                "workFromHome": "0",
                "labels": [],
                "topLabels": [],
                "textSnippet": "Compliance",
            },
            {
                "id": 1003,
                "section": "regional",
                "title": "Legal Counsel",
                "companyName": "Dritte Firma KG",
                "location": "Wiener Neustadt",
                "url": "/stellenangebote--Legal-Counsel-Wiener-Neustadt--1003-inline.html",
                "datePosted": "2026-08-18T08:00:00+02:00",
                "salary": None,
                "workFromHome": "0",
                "labels": [],
                "topLabels": [],
                "textSnippet": "Legal",
            },
            {
                "id": 1004,
                "section": "recommended",
                "title": "Unrelated recommendation",
                "companyName": "Vierte Firma GmbH",
                "location": "Wien",
                "url": "/stellenangebote--Recommendation-Wien--1004-inline.html",
                "datePosted": "2026-08-18T08:00:00+02:00",
                "salary": None,
                "workFromHome": "0",
                "labels": [],
                "topLabels": [],
                "textSnippet": "Unrelated",
            },
        ]
    }
    jobs, warnings = job_tracker_module.parse_stepstone_jobs_from_api(
        payload,
        "2026-08-20T08:00:00Z",
    )
    assert_equal(
        [job["id"] for job in jobs],
        ["stepstone:1001", "stepstone:1002"],
        "StepStone API Wien IDs",
    )
    assert_equal(jobs[0]["title"], "Legal Counsel", "StepStone API title cleanup")
    assert_equal(
        jobs[0]["url"],
        "https://www.stepstone.at/stellenangebote--Legal-Counsel-Wien--1001-inline.html",
        "StepStone API tracking query removed",
    )
    assert_true("Home Office" in (jobs[0].get("department") or ""), "StepStone API home office")
    assert_true(warnings and "non-Wien" in warnings[0], "StepStone API non-Wien warning")
    assert_true(
        any("non-search-result" in warning for warning in warnings),
        "StepStone API recommendation warning",
    )


def test_stepstone_api_uses_second_first_party_host():
    calls = []
    valid_empty_page = {
        "items": [],
        "meta": {"total": 0},
        "unifiedPagination": {"links": {"next": ""}},
    }
    original_fetch = job_tracker_module.fetch_url
    try:
        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            if url == job_tracker_module.STEPSTONE_API_URLS[0]:
                return None, "TimeoutError: primary timed out"
            return valid_empty_page, None

        job_tracker_module.fetch_url = fake_fetch
        data, error = job_tracker_module.fetch_stepstone_api_page(
            job_tracker_module.STEPSTONE_SEARCH_URL,
            "2f4d4654-3e8d-4c29-84b1-42a6c4ac6e30",
        )
    finally:
        job_tracker_module.fetch_url = original_fetch

    assert_equal(error, None, "StepStone second API host error")
    assert_equal(data, valid_empty_page, "StepStone second API host response")
    assert_equal(
        [call[0] for call in calls],
        list(job_tracker_module.STEPSTONE_API_URLS),
        "StepStone API host order",
    )
    assert_equal(
        calls[1][1]["json_body"]["userData"]["userHashId"],
        "2f4d4654-3e8d-4c29-84b1-42a6c4ac6e30",
        "StepStone API UUID retained",
    )
    assert_true(
        all(call[1].get("accept") == "application/json" for call in calls),
        "StepStone API requests exact JSON responses",
    )


def test_stepstone_api_invalid_structure_uses_second_first_party_host():
    calls = []
    valid_empty_page = {
        "items": [],
        "meta": {"total": 0},
        "unifiedPagination": {"links": {"next": ""}},
    }
    original_fetch = job_tracker_module.fetch_url
    try:
        def fake_fetch(url, **kwargs):
            calls.append(url)
            if url == job_tracker_module.STEPSTONE_API_URLS[0]:
                return {"error": "temporary upstream response"}, None
            return valid_empty_page, None

        job_tracker_module.fetch_url = fake_fetch
        data, error = job_tracker_module.fetch_stepstone_api_page(
            job_tracker_module.STEPSTONE_SEARCH_URL,
            "2f4d4654-3e8d-4c29-84b1-42a6c4ac6e30",
        )
    finally:
        job_tracker_module.fetch_url = original_fetch

    assert_equal(error, None, "StepStone invalid primary structure fallback error")
    assert_equal(data, valid_empty_page, "StepStone invalid primary structure fallback")
    assert_equal(calls, list(job_tracker_module.STEPSTONE_API_URLS), "StepStone invalid primary host order")


def test_stepstone_api_paginates_and_retains_job_ids():
    first_page = {
        "items": [
            {
                "id": 1001,
                "title": "Legal Counsel",
                "companyName": "Erste Firma GmbH",
                "location": "Wien",
                "url": "/stellenangebote--Legal-Counsel-Wien--1001-inline.html",
            },
            {
                "id": 1003,
                "title": "Legal Counsel",
                "companyName": "Dritte Firma KG",
                "location": "Wiener Neustadt",
                "url": "/stellenangebote--Legal-Counsel-Wiener-Neustadt--1003-inline.html",
            },
        ],
        "meta": {"total": 3},
        "unifiedPagination": {
            "links": {"next": "https://www.stepstone.at/jobs/jurist/in-wien?page=2"}
        },
    }
    second_page = {
        "items": [
            {
                "id": 1002,
                "title": "Compliance Jurist:in",
                "companyName": "Zweite Firma AG",
                "location": "1030 Wien",
                "url": "/stellenangebote--Compliance-Jurist-Wien--1002-inline.html",
            }
        ],
        "meta": {"total": 3},
        "unifiedPagination": {"links": {"next": ""}},
    }
    calls = []
    original_fetch = job_tracker_module.fetch_url
    try:
        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            page_url = kwargs["json_body"]["url"]
            if page_url == job_tracker_module.STEPSTONE_SEARCH_URL:
                return first_page, None
            if page_url == "https://www.stepstone.at/jobs/jurist/in-wien?page=2":
                return second_page, None
            return None, f"unexpected page URL: {page_url}"

        job_tracker_module.fetch_url = fake_fetch
        jobs, errors = job_tracker_module._fetch_stepstone_jobs_from_api()
    finally:
        job_tracker_module.fetch_url = original_fetch

    assert_equal(errors, [], "StepStone API pagination errors")
    assert_equal(
        [job["id"] for job in jobs],
        ["stepstone:1001", "stepstone:1002"],
        "StepStone API paginated Wien IDs",
    )
    hashes = {
        call[1]["json_body"]["userData"]["userHashId"]
        for call in calls
    }
    assert_equal(len(hashes), 1, "StepStone API reuses one UUID across pages")
    user_hash = next(iter(hashes))
    assert_equal(str(job_tracker_module.uuid.UUID(user_hash)), user_hash, "StepStone API UUID is valid")


def test_stepstone_api_failure_uses_html_fallback():
    fallback_job = sample_job("1001", source="stepstone")
    original_api = job_tracker_module._fetch_stepstone_jobs_from_api
    original_html = job_tracker_module._fetch_stepstone_jobs_from_html
    try:
        job_tracker_module._fetch_stepstone_jobs_from_api = (
            lambda: ([], ["API timeout"])
        )
        job_tracker_module._fetch_stepstone_jobs_from_html = (
            lambda: ([fallback_job], [])
        )
        jobs, errors = job_tracker_module.fetch_stepstone_jobs()
    finally:
        job_tracker_module._fetch_stepstone_jobs_from_api = original_api
        job_tracker_module._fetch_stepstone_jobs_from_html = original_html

    assert_equal(errors, [], "StepStone HTML fallback errors")
    assert_equal([job["id"] for job in jobs], ["stepstone:1001"], "StepStone HTML fallback job")


def test_stepstone_fetch_uses_direct_fast_path():
    calls = []
    original_fetch = job_tracker_module.fetch_url
    try:
        def fake_fetch(url, **kwargs):
            calls.append((url, kwargs))
            return "<html>direct</html>", None

        job_tracker_module.fetch_url = fake_fetch
        html, error = job_tracker_module.fetch_stepstone_page(
            job_tracker_module.STEPSTONE_SEARCH_URL
        )
    finally:
        job_tracker_module.fetch_url = original_fetch

    assert_equal(html, "<html>direct</html>", "StepStone direct response")
    assert_equal(error, None, "StepStone direct error")
    assert_equal(len(calls), 1, "StepStone direct call count")
    assert_equal(calls[0][0], job_tracker_module.STEPSTONE_SEARCH_URL, "StepStone direct URL")
    assert_equal(
        calls[0][1].get("timeout_seconds"),
        job_tracker_module.STEPSTONE_DIRECT_TIMEOUT_SECONDS,
        "StepStone direct timeout",
    )


def test_stepstone_html_fetch_falls_back_to_curl():
    direct_calls = []
    curl_calls = []
    original_fetch = job_tracker_module.fetch_url
    original_curl = job_tracker_module.fetch_url_with_curl
    try:
        def fake_fetch(url, **kwargs):
            direct_calls.append((url, kwargs))
            return None, "TimeoutError: direct timed out"

        def fake_curl(url, **kwargs):
            curl_calls.append((url, kwargs))
            return "<html>curl</html>", None

        job_tracker_module.fetch_url = fake_fetch
        job_tracker_module.fetch_url_with_curl = fake_curl
        html, error = job_tracker_module.fetch_stepstone_page(
            job_tracker_module.STEPSTONE_SEARCH_URL
        )
    finally:
        job_tracker_module.fetch_url = original_fetch
        job_tracker_module.fetch_url_with_curl = original_curl

    assert_equal(html, "<html>curl</html>", "StepStone curl response")
    assert_equal(error, None, "StepStone curl error")
    assert_equal(len(direct_calls), 1, "StepStone direct call count")
    assert_equal(len(curl_calls), 1, "StepStone curl call count")
    assert_equal(
        curl_calls[0][1].get("timeout_seconds"),
        job_tracker_module.STEPSTONE_CURL_TIMEOUT_SECONDS,
        "StepStone curl timeout",
    )


def test_stepstone_html_fetch_reports_direct_and_curl_errors():
    original_fetch = job_tracker_module.fetch_url
    original_curl = job_tracker_module.fetch_url_with_curl
    try:
        def fake_fetch(url, **kwargs):
            return None, "TimeoutError: direct timed out"

        job_tracker_module.fetch_url = fake_fetch
        job_tracker_module.fetch_url_with_curl = (
            lambda _url, **_kwargs: (None, "curl exit 28")
        )
        html, error = job_tracker_module.fetch_stepstone_page(
            job_tracker_module.STEPSTONE_SEARCH_URL
        )
    finally:
        job_tracker_module.fetch_url = original_fetch
        job_tracker_module.fetch_url_with_curl = original_curl

    assert_equal(html, None, "StepStone combined failure response")
    assert_true("direct timed out" in (error or ""), "StepStone direct failure retained")
    assert_true("curl exit 28" in (error or ""), "StepStone curl failure retained")


def test_stepstone_fetch_paginates_and_retains_job_ids():
    first_page = """
    <html>
      <head><link rel="next" href="/jobs/jurist/in-wien?page=2" /></head>
      <body>
        <p>3 Stellenangebote</p>
        <article id="job-item-1001" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Legal-Counsel-Wien--1001-inline.html">Legal Counsel</a>
          <span data-at="job-item-company-name">Erste Firma GmbH</span>
          <span data-at="job-item-location">Wien</span>
        </article>
      </body>
    </html>
    """
    second_page = """
    <html>
      <body>
        <article id="job-item-1002" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Compliance-Jurist-Wien--1002-inline.html">Compliance Jurist:in</a>
          <span data-at="job-item-company-name">Zweite Firma AG</span>
          <span data-at="job-item-location">1030 Wien</span>
        </article>
        <article id="job-item-1003" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Legal-Counsel-Wiener-Neustadt--1003-inline.html">Legal Counsel</a>
          <span data-at="job-item-company-name">Dritte Firma KG</span>
          <span data-at="job-item-location">Wiener Neustadt</span>
        </article>
      </body>
    </html>
    """
    calls = []
    original_fetch_page = job_tracker_module.fetch_stepstone_page
    try:
        def fake_fetch_page(url):
            calls.append(url)
            if url == job_tracker_module.STEPSTONE_SEARCH_URL:
                return first_page, None
            if url == "https://www.stepstone.at/jobs/jurist/in-wien?page=2":
                return second_page, None
            return None, f"unexpected URL: {url}"

        job_tracker_module.fetch_stepstone_page = fake_fetch_page
        jobs, errors = job_tracker_module._fetch_stepstone_jobs_from_html()
    finally:
        job_tracker_module.fetch_stepstone_page = original_fetch_page

    assert_equal(errors, [], "StepStone pagination errors")
    assert_equal(
        [job["id"] for job in jobs],
        ["stepstone:1001", "stepstone:1002"],
        "StepStone paginated Wien IDs",
    )
    assert_equal(
        calls,
        [
            job_tracker_module.STEPSTONE_SEARCH_URL,
            "https://www.stepstone.at/jobs/jurist/in-wien?page=2",
        ],
        "StepStone pagination URLs",
    )


def test_stepstone_html_rejects_incomplete_results():
    cards = "".join(
        f"""
        <article id="job-item-{1000 + index}" data-at="job-item">
          <a data-at="job-item-title" href="/stellenangebote--Legal-Counsel-Wien--{1000 + index}-inline.html">Legal Counsel</a>
          <span data-at="job-item-company-name">Firma {index}</span>
          <span data-at="job-item-location">Wien</span>
        </article>
        """
        for index in range(24)
    )
    incomplete_page = f"<html><body><p>30 Stellenangebote</p>{cards}</body></html>"
    original_fetch_page = job_tracker_module.fetch_stepstone_page
    try:
        job_tracker_module.fetch_stepstone_page = lambda _url: (incomplete_page, None)
        _jobs, errors = job_tracker_module._fetch_stepstone_jobs_from_html()
    finally:
        job_tracker_module.fetch_stepstone_page = original_fetch_page

    assert_true(
        any("appears incomplete" in error for error in errors),
        "StepStone incomplete HTML is rejected at the former 80 percent boundary",
    )


def test_lawfinder_next_flight_parser():
    raw_jobs = [{
        "id": "lf-1",
        "title": "Legal Counsel",
        "employer": {"title": "Test GmbH"},
        "jobLocations": [{"title": "Wien"}],
    }]
    decoded_payload = "1:" + json.dumps({"data": raw_jobs}, ensure_ascii=False) + "\n"
    escaped_payload = json.dumps(decoded_payload, ensure_ascii=False)[1:-1]
    html = f'<script>self.__next_f.push([1,"{escaped_payload}"])</script>'

    jobs, errors = parse_lawfinder_jobs_from_html(html, "2026-06-18T00:00:00Z")

    assert_equal(errors, [], "lawfinder parser errors")
    assert_equal([job["id"] for job in jobs], ["lawfinder:lf-1"], "lawfinder parsed ids")


def test_compare_new_jobs():
    old_jobs = [sample_job("1"), sample_job("2")]
    current_jobs = [sample_job("1"), sample_job("2"), sample_job("3")]
    new_jobs = compare_new_jobs(old_jobs, current_jobs)
    assert_equal([job["id"] for job in new_jobs], ["jusjobs:3"], "new job ids")


def test_cross_source_dedupe_same_position():
    derstandard_job = {
        "id": "derstandard:100",
        "source": "derstandard",
        "title": "Legal Counsel Arbeitsrecht (w/m/d)",
        "company": "Test GmbH",
        "location": "Wien",
        "url": "https://jobs.derstandard.at/job/100",
    }
    lawfinder_job = {
        "id": "lawfinder:200",
        "source": "lawfinder",
        "title": "Legal Counsel mit Schwerpunkt Arbeitsrecht",
        "company": "Test GmbH",
        "location": "Wien",
        "url": "https://www.lawfinder.at/jobs/200",
    }
    different_role = {
        "id": "lawfinder:201",
        "source": "lawfinder",
        "title": "Legal Counsel Datenschutz",
        "company": "Test GmbH",
        "location": "Wien",
        "url": "https://www.lawfinder.at/jobs/201",
    }

    deduped = dedupe_cross_source_jobs([derstandard_job, lawfinder_job, different_role])
    assert_equal([job["id"] for job in deduped], ["derstandard:100", "lawfinder:201"], "cross-source dedupe")
    assert_true(find_cross_source_duplicate(lawfinder_job, [derstandard_job]), "cross-source duplicate lookup")
    wiener_neustadt_job = dict(lawfinder_job, id="lawfinder:202", location="Wiener Neustadt")
    assert_true(
        not find_cross_source_duplicate(wiener_neustadt_job, [derstandard_job]),
        "wiener neustadt is not wien for dedupe",
    )


def test_run_job_tracker_snapshot_flow():
    with tempfile.TemporaryDirectory() as tmp:
        fetchers = {"jusjobs": lambda: ([sample_job("1")], [])}

        first = run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            dry_run=False,
            notify=False,
            fetchers=fetchers,
        )
        assert_true(first.first_run, "first run flag")
        assert_equal(first.new_jobs, [], "first run no alerts")
        assert_true(Path(tmp, f"{JOB_SNAPSHOT_NAME}.json").exists(), "snapshot created")

        second = run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            dry_run=False,
            notify=False,
            fetchers=fetchers,
        )
        assert_true(not second.first_run, "second run flag")
        assert_equal(second.new_jobs, [], "second run no new jobs")

        old_snapshot = {
            "timestamp": "2026-06-18T00:00:00Z",
            "data": {
                "schema_version": 2,
                "sources": {},
                "jobs": [sample_job("1")],
            },
        }
        save_snapshot(JOB_SNAPSHOT_NAME, old_snapshot, tmp)
        fetchers = {"jusjobs": lambda: ([sample_job("1"), sample_job("2")], [])}
        third = run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            dry_run=False,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal([job["id"] for job in third.new_jobs], ["jusjobs:2"], "detected reintroduced/new job")

        snapshot = json.loads(Path(tmp, f"{JOB_SNAPSHOT_NAME}.json").read_text(encoding="utf-8"))
        assert_equal(len(snapshot["data"]["jobs"]), 2, "snapshot updated in temp dir")


def test_cross_source_duplicate_old_snapshot_does_not_alert():
    with tempfile.TemporaryDirectory() as tmp:
        old_snapshot = {
            "timestamp": "2026-06-18T00:00:00Z",
            "data": {
                "schema_version": 2,
                "sources": {"derstandard": {"label": "DER STANDARD Jobs", "count": 1}},
                "jobs": [
                    {
                        "id": "derstandard:100",
                        "source": "derstandard",
                        "title": "Legal Counsel Arbeitsrecht (w/m/d)",
                        "company": "Test GmbH",
                        "location": "Wien",
                        "url": "https://jobs.derstandard.at/job/100",
                        "first_seen": "2026-06-18T00:00:00Z",
                    }
                ],
            },
        }
        save_snapshot(JOB_SNAPSHOT_NAME, old_snapshot, tmp)
        fetchers = {
            "lawfinder": lambda: (
                [
                    {
                        "id": "lawfinder:200",
                        "source": "lawfinder",
                        "title": "Legal Counsel mit Schwerpunkt Arbeitsrecht",
                        "company": "Test GmbH",
                        "location": "Wien",
                        "url": "https://www.lawfinder.at/jobs/200",
                    }
                ],
                [],
            )
        }
        result = run_job_tracker(
            source_names=["lawfinder"],
            snapshot_dir=tmp,
            dry_run=False,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal(result.new_jobs, [], "cross-source duplicate suppresses alert")
        assert_equal(result.all_jobs[0]["first_seen"], "2026-06-18T00:00:00Z", "cross-source first_seen preserved")


def test_new_source_is_baselined_without_alert():
    with tempfile.TemporaryDirectory() as tmp:
        old_snapshot = {
            "timestamp": "2026-06-18T00:00:00Z",
            "data": {
                "schema_version": 2,
                "sources": {"jusjobs": {"label": "JusJobs", "count": 1}},
                "jobs": [sample_job("1")],
            },
        }
        save_snapshot(JOB_SNAPSHOT_NAME, old_snapshot, tmp)
        fetchers = {
            "jusjobs": lambda: ([sample_job("1"), sample_job("2")], []),
            "karriere_at": lambda: ([sample_job("k1", title="Legal Counsel Datenschutz", source="karriere_at")], []),
        }
        result = run_job_tracker(
            source_names=["jusjobs", "karriere_at"],
            snapshot_dir=tmp,
            dry_run=False,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal([job["id"] for job in result.new_jobs], ["jusjobs:2"], "new source baseline alert suppression")
        assert_true(
            any(job["source"] == "karriere_at" for job in result.all_jobs),
            "new source persisted to snapshot",
        )


def test_failed_source_first_success_is_baselined_without_alert():
    with tempfile.TemporaryDirectory() as tmp:
        old_snapshot = {
            "timestamp": "2026-06-18T00:00:00Z",
            "data": {
                "schema_version": 2,
                "sources": {
                    "stepstone": {
                        "label": "StepStone",
                        "last_success": None,
                        "last_error": "TimeoutError",
                        "count": 0,
                    }
                },
                "jobs": [],
            },
        }
        save_snapshot(JOB_SNAPSHOT_NAME, old_snapshot, tmp)
        fetchers = {
            "stepstone": lambda: ([sample_job("s1", title="Jurist:in Datenschutz", source="stepstone")], []),
        }
        result = run_job_tracker(
            source_names=["stepstone"],
            snapshot_dir=tmp,
            dry_run=False,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal(result.new_jobs, [], "failed source first success baseline")
        assert_true(
            any(job["source"] == "stepstone" for job in result.all_jobs),
            "failed source first success persisted",
        )


def test_schema_migration_baselines_previously_empty_source():
    with tempfile.TemporaryDirectory() as tmp:
        old_snapshot = {
            "timestamp": "2026-06-18T00:00:00Z",
            "data": {
                "schema_version": 2,
                "sources": {
                    "lawfinder": {
                        "label": "LawFinder",
                        "last_success": "2026-06-18T00:00:00Z",
                        "last_error": None,
                        "count": 0,
                    }
                },
                "jobs": [],
            },
        }
        save_snapshot(JOB_SNAPSHOT_NAME, old_snapshot, tmp)
        current_jobs = [sample_job("lf1", title="Legal Counsel", source="lawfinder")]
        fetchers = {"lawfinder": lambda: (list(current_jobs), [])}

        migrated = run_job_tracker(
            source_names=["lawfinder"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal(migrated.new_jobs, [], "schema migration baselines repaired empty source")
        assert_equal(migrated.snapshot["data"]["schema_version"], 3, "schema migration version")

        current_jobs.append(sample_job("lf2", title="Compliance Counsel", source="lawfinder"))
        later = run_job_tracker(
            source_names=["lawfinder"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal([job["id"] for job in later.new_jobs], ["lawfinder:lf2"], "post-migration new job alerts")


def test_discord_notifier_sends_all_jobs_in_batches():
    payloads = []
    original_post = notifier._post_discord_payload

    def fake_post(_webhook_url, payload):
        payloads.append(payload)
        return True

    try:
        notifier._post_discord_payload = fake_post
        jobs = [sample_job(str(index)) for index in range(13)]
        ok = notifier.send_new_jobs_notification("https://example.invalid/webhook", jobs)
    finally:
        notifier._post_discord_payload = original_post

    assert_true(ok, "notifier returns success")
    assert_equal(len(payloads), 2, "notifier batch count")
    field_count = sum(len(payload["embeds"][0]["fields"]) for payload in payloads)
    assert_equal(field_count, 13, "all jobs included in discord payloads")
    descriptions = "\n".join(payload["embeds"][0]["description"] for payload in payloads)
    assert_true("1-10 von 13" in descriptions, "first batch range")
    assert_true("11-13 von 13" in descriptions, "second batch range")

    payloads.clear()
    long_jobs = []
    for index in range(13):
        job = sample_job(f"long-{index}", title="T" * 300)
        job.update({
            "company": "C" * 220,
            "location": "L" * 220,
            "salary": "S" * 220,
            "employment_type": "E" * 220,
            "published_at": "P" * 220,
            "snippet": "N" * 500,
        })
        long_jobs.append(job)

    try:
        notifier._post_discord_payload = fake_post
        delivery = notifier.deliver_new_jobs_notification(
            "https://example.invalid/webhook",
            long_jobs,
        )
    finally:
        notifier._post_discord_payload = original_post

    assert_true(delivery.success, "long notifier delivery success")
    assert_equal(len(delivery.delivered_job_ids), 13, "long notifier delivered ids")
    assert_true(
        all(
            notifier._embed_character_count(payload["embeds"][0])
            <= notifier.DISCORD_EMBED_CHARACTER_LIMIT
            for payload in payloads
        ),
        "all embeds stay under Discord total character limit",
    )


def test_failed_notification_is_retried_from_outbox():
    with tempfile.TemporaryDirectory() as tmp:
        current_jobs = [sample_job("1")]
        fetchers = {"jusjobs": lambda: (list(current_jobs), [])}
        run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )

        current_jobs.append(sample_job("2"))
        calls = []
        original_delivery = job_tracker_module.deliver_new_jobs_notification

        def fail_delivery(_webhook_url, jobs):
            calls.append([job["id"] for job in jobs])
            return notifier.NotificationDeliveryResult([], [job["id"] for job in jobs])

        def succeed_delivery(_webhook_url, jobs):
            calls.append([job["id"] for job in jobs])
            return notifier.NotificationDeliveryResult([job["id"] for job in jobs], [])

        try:
            job_tracker_module.deliver_new_jobs_notification = fail_delivery
            failed = run_job_tracker(
                source_names=["jusjobs"],
                snapshot_dir=tmp,
                notify=True,
                webhook_url="https://example.invalid/webhook",
                fetchers=fetchers,
            )
            assert_true(not failed.healthy, "failed notification marks run unhealthy")
            assert_equal([job["id"] for job in failed.pending_jobs], ["jusjobs:2"], "failed job queued")

            job_tracker_module.deliver_new_jobs_notification = succeed_delivery
            retried = run_job_tracker(
                source_names=["jusjobs"],
                snapshot_dir=tmp,
                notify=True,
                webhook_url="https://example.invalid/webhook",
                fetchers=fetchers,
            )
        finally:
            job_tracker_module.deliver_new_jobs_notification = original_delivery

        assert_true(retried.healthy, "successful retry restores healthy state")
        assert_equal(retried.pending_jobs, [], "successful retry clears outbox")
        assert_equal(calls, [["jusjobs:2"], ["jusjobs:2"]], "only failed job retried")


def test_subset_run_preserves_other_sources():
    with tempfile.TemporaryDirectory() as tmp:
        current = {
            "jusjobs": [sample_job("j1")],
            "uniqa": [sample_job("u1", title="Datenschutzjurist:in", source="uniqa")],
        }
        fetchers = {
            "jusjobs": lambda: (list(current["jusjobs"]), []),
            "uniqa": lambda: (list(current["uniqa"]), []),
        }
        run_job_tracker(
            source_names=["jusjobs", "uniqa"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        subset = run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal(
            sorted(job["id"] for job in subset.all_jobs),
            ["jusjobs:j1", "uniqa:u1"],
            "subset run keeps unselected jobs",
        )
        assert_true("uniqa" in subset.sources, "subset run keeps unselected source status")

        current["uniqa"].append(sample_job("u2", title="Compliance Jurist:in", source="uniqa"))
        full = run_job_tracker(
            source_names=["jusjobs", "uniqa"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal([job["id"] for job in full.new_jobs], ["uniqa:u2"], "only genuinely new job alerts")


def test_empty_success_preserves_previous_source_state():
    with tempfile.TemporaryDirectory() as tmp:
        run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={"jusjobs": lambda: ([sample_job("1")], [])},
        )
        failed = run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={"jusjobs": lambda: ([], [])},
        )
        assert_true(not failed.healthy, "the only selected source failing is fatal")
        assert_true(failed.degraded, "source outage is reported as degraded")
        assert_equal([job["id"] for job in failed.all_jobs], ["jusjobs:1"], "empty result preserves baseline")


def test_erste_bank_authoritative_empty_clears_active_job_but_keeps_seen_id():
    with tempfile.TemporaryDirectory() as tmp:
        first_job = sample_job("1", source="erste_bank")
        run_job_tracker(
            source_names=["erste_bank"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={"erste_bank": lambda: ([first_job], [])},
        )
        empty = run_job_tracker(
            source_names=["erste_bank"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={"erste_bank": lambda: ([], [])},
        )
        assert_true(empty.healthy, "authoritative empty Erste result is healthy")
        assert_equal(empty.all_jobs, [], "closed Erste job removed from active jobs")
        assert_equal(empty.sources["erste_bank"]["count"], 0, "Erste source records zero active jobs")
        assert_true("erste_bank:1" in empty.snapshot["data"]["seen_ids"], "closed Erste id remains seen")

        reappeared = run_job_tracker(
            source_names=["erste_bank"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={"erste_bank": lambda: ([first_job], [])},
        )
        assert_equal(reappeared.new_jobs, [], "reappearing Erste id is not alerted again")


def test_partial_source_failure_still_delivers_other_source_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        current = {
            "jusjobs": [sample_job("j1")],
            "uniqa": [sample_job("u1", title="Datenschutzjurist:in", source="uniqa")],
        }
        run_job_tracker(
            source_names=["jusjobs", "uniqa"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={
                "jusjobs": lambda: (list(current["jusjobs"]), []),
                "uniqa": lambda: (list(current["uniqa"]), []),
            },
        )

        current["uniqa"].append(sample_job("u2", title="Compliance Jurist:in", source="uniqa"))
        delivered = []
        original_delivery = job_tracker_module.deliver_new_jobs_notification

        def succeed_delivery(_webhook_url, jobs):
            delivered.extend(job["id"] for job in jobs)
            return notifier.NotificationDeliveryResult([job["id"] for job in jobs], [])

        try:
            job_tracker_module.deliver_new_jobs_notification = succeed_delivery
            result = run_job_tracker(
                source_names=["jusjobs", "uniqa"],
                snapshot_dir=tmp,
                notify=True,
                webhook_url="https://example.invalid/webhook",
                fetchers={
                    "jusjobs": lambda: ([], ["temporary timeout"]),
                    "uniqa": lambda: (list(current["uniqa"]), []),
                },
            )
        finally:
            job_tracker_module.deliver_new_jobs_notification = original_delivery

        assert_true(result.healthy, "partial source failure does not fail the whole run")
        assert_true(result.degraded, "partial source failure remains visible")
        assert_equal(delivered, ["uniqa:u2"], "new job from healthy source is delivered")
        assert_equal(result.pending_jobs, [], "successful delivery clears the outbox")
        assert_equal(
            sorted(job["id"] for job in result.all_jobs),
            ["jusjobs:j1", "uniqa:u1", "uniqa:u2"],
            "failed source baseline is preserved alongside healthy source updates",
        )


def test_all_sources_failed_is_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        result = run_job_tracker(
            source_names=["jusjobs", "uniqa"],
            snapshot_dir=tmp,
            notify=False,
            fetchers={
                "jusjobs": lambda: ([], ["timeout"]),
                "uniqa": lambda: ([], ["timeout"]),
            },
        )
        assert_true(not result.healthy, "all selected sources failing is fatal")
        assert_true(result.degraded, "all source failures remain visible")


def test_seen_ids_suppress_reappearing_jobs():
    with tempfile.TemporaryDirectory() as tmp:
        current_jobs = [sample_job("1")]
        fetchers = {"jusjobs": lambda: (list(current_jobs), [])}
        run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        current_jobs[:] = [sample_job("2")]
        run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        current_jobs[:] = [sample_job("1"), sample_job("2")]
        reappeared = run_job_tracker(
            source_names=["jusjobs"],
            snapshot_dir=tmp,
            notify=False,
            fetchers=fetchers,
        )
        assert_equal(reappeared.new_jobs, [], "previously seen id does not alert again")


def run_tests():
    tests = [
        test_normalization,
        test_jusjobs_parser,
        test_jusjobs_stale_advertised_count_is_only_a_warning,
        test_jusjobs_unparsed_rendered_card_is_an_error,
        test_erste_bank_filter,
        test_erste_bank_catalog_validation,
        test_uniqa_rss_filter,
        test_karriere_at_state_filter,
        test_stepstone_html_filter,
        test_stepstone_api_filter,
        test_stepstone_api_uses_second_first_party_host,
        test_stepstone_api_invalid_structure_uses_second_first_party_host,
        test_stepstone_api_paginates_and_retains_job_ids,
        test_stepstone_api_failure_uses_html_fallback,
        test_stepstone_fetch_uses_direct_fast_path,
        test_stepstone_html_fetch_falls_back_to_curl,
        test_stepstone_html_fetch_reports_direct_and_curl_errors,
        test_stepstone_fetch_paginates_and_retains_job_ids,
        test_stepstone_html_rejects_incomplete_results,
        test_lawfinder_next_flight_parser,
        test_compare_new_jobs,
        test_cross_source_dedupe_same_position,
        test_run_job_tracker_snapshot_flow,
        test_cross_source_duplicate_old_snapshot_does_not_alert,
        test_new_source_is_baselined_without_alert,
        test_failed_source_first_success_is_baselined_without_alert,
        test_schema_migration_baselines_previously_empty_source,
        test_discord_notifier_sends_all_jobs_in_batches,
        test_failed_notification_is_retried_from_outbox,
        test_subset_run_preserves_other_sources,
        test_empty_success_preserves_previous_source_state,
        test_erste_bank_authoritative_empty_clears_active_job_but_keeps_seen_id,
        test_partial_source_failure_still_delivers_other_source_jobs,
        test_all_sources_failed_is_fatal,
        test_seen_ids_suppress_reappearing_jobs,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("All job tracker tests passed.")


if __name__ == "__main__":
    run_tests()
