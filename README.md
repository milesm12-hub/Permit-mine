# Permit-mine

Air permit change detection and export tooling for the Edge Generation workflow.

## Files

- `permit_monitor.py` — fetches air permit snapshots from EPA ECHO, stores them in `permits.db`, and diffs the latest two snapshots.
- `export_changelog_xlsx.py` — reads the `changelog` table from `permits.db` and exports a formatted Excel workbook at `air_permit_changelog.xlsx`.
- `requirements.txt` — Python dependencies required for the Excel export.

## New signal type

- The tool now also detects `AUCTION_SCRAP_SIGNAL` when permit records contain auction or scrap disposition keywords in their equipment/facility/owner descriptions.
- `.github/workflows/air-permit-monitor.yml` — scheduled GitHub Actions workflow that fetches TX permits, exports the changelog, and commits updated artifacts.
- `.github/workflows/export_changelog.yml` — optional workflow to export the changelog workbook independently.

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run locally

- Fetch a new snapshot and generate a changelog:

```bash
python permit_monitor.py fetch --state TX
```

- Diff the last two stored snapshots without fetching:

```bash
python permit_monitor.py diff --state TX
```

- List non-renewal warnings:

```bash
python permit_monitor.py warnings --state TX --days 90
```

- Export the changelog to Excel:

```bash
python export_changelog_xlsx.py
```

### Notes

- The fetch step includes basic retry/backoff handling for HTTP 429 rate-limit responses from the EPA ECHO API.
- If the API returns repeated 429 responses, the script retries up to 4 times before failing.

## GitHub Actions

The primary workflow is `.github/workflows/air-permit-monitor.yml`:

- runs every Monday at 13:00 UTC
- installs dependencies from `requirements.txt`
- runs `permit_monitor.py fetch --state TX`
- runs `python export_changelog_xlsx.py`
- commits `permits.db` and `air_permit_changelog.xlsx` when changes are detected

If you want to change the tracked state or schedule, update the workflow YAML accordingly.
