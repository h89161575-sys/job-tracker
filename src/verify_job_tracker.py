import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from job_tracker import (  # noqa: E402
    JOB_SNAPSHOT_NAME,
    compare_new_jobs,
    filter_erste_bank_jobs,
    job_fingerprint,
    normalize_text,
    parse_jusjobs_jobs_from_html,
    parse_uniqa_rss_jobs,
    run_job_tracker,
    save_snapshot,
    slugify,
)


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
    assert_equal(slugify("Jurist:in für Vertrags- & Gesellschaftsrecht"), "jurist-in-fuer-vertrags-gesellschaftsrecht", "slugify")
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


def test_compare_new_jobs():
    old_jobs = [sample_job("1"), sample_job("2")]
    current_jobs = [sample_job("1"), sample_job("2"), sample_job("3")]
    new_jobs = compare_new_jobs(old_jobs, current_jobs)
    assert_equal([job["id"] for job in new_jobs], ["jusjobs:3"], "new job ids")


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


def run_tests():
    tests = [
        test_normalization,
        test_jusjobs_parser,
        test_erste_bank_filter,
        test_uniqa_rss_filter,
        test_compare_new_jobs,
        test_run_job_tracker_snapshot_flow,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print("All job tracker tests passed.")


if __name__ == "__main__":
    run_tests()
