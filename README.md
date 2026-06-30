# Job Tracker

Tracks selected legal job searches and sends Discord notifications when new jobs appear.

## Sources

- JusJobs: Wien, selected legal job types, salary filter 3000-7000
- Erste Bank / Sparkasse: Wien + Legal / Compliance / Audit
- UNIQA: Jurist + Wien RSS search
- LawFinder: Wien with company type filters
- DER STANDARD Jobs: Wien + Jurist + full time + home office + legal category
- karriere.at: JUS + Wien + Rechtswesen + Vollzeit + Homeoffice + salary filter
- StepStone: Jurist/in + Wien + 30 km radius, with local Wien filtering for district/address cases

## GitHub setup

Create a repository secret:

- `JOB_DISCORD_WEBHOOK_URL`: your Discord webhook URL

The workflow runs every 15 minutes at minute `7/15` and commits snapshot updates in `data/snapshots`.

## Local checks

```powershell
python -m py_compile src\job_tracker.py src\notifier.py src\config.py src\verify_job_tracker.py
python src\verify_job_tracker.py
python src\job_tracker.py --dry-run
```

On this Windows machine only, local TLS may require:

```powershell
$env:JOB_TRACKER_ALLOW_INSECURE_SSL='1'
python src\job_tracker.py --dry-run
```

Do not use `JOB_TRACKER_ALLOW_INSECURE_SSL=1` in GitHub Actions unless a runner actually needs it.
