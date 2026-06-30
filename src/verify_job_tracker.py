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
    parse_karriere_jobs_from_state,
    parse_jusjobs_jobs_from_html,
    parse_stepstone_jobs_from_html,
    parse_uniqa_rss_jobs,
    run_job_tracker,
    save_snapshot,
    slugify,
)
import notifier  # noqa: E402


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
    ]
    jobs = filter_erste_bank_jobs(raw_jobs, "2026-06-18T00:00:00Z")
    assert_equal(len(jobs), 1, "erste filter count")
    assert_equal(jobs[0]["id"], "erste_bank:1", "erste id")


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


def run_tests():
    tests = [
        test_normalization,
        test_jusjobs_parser,
        test_erste_bank_filter,
        test_uniqa_rss_filter,
        test_karriere_at_state_filter,
        test_stepstone_html_filter,
        test_compare_new_jobs,
        test_cross_source_dedupe_same_position,
        test_run_job_tracker_snapshot_flow,
        test_cross_source_duplicate_old_snapshot_does_not_alert,
        test_new_source_is_baselined_without_alert,
        test_discord_notifier_sends_all_jobs_in_batches,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("All job tracker tests passed.")


if __name__ == "__main__":
    run_tests()
