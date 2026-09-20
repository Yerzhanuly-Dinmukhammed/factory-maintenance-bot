# Factory Maintenance Bot

**Maintenance requests, preventive maintenance and shift coordination — inside Telegram.**

Python · python-telegram-bot · APScheduler · Google Sheets · JSON · openpyxl · Matplotlib

[Русское описание](docs/README.ru.md) · [Security & data handling](SECURITY.md) · [All rights reserved](LICENSE)

> **Portfolio review only — not open source.** Reuse requires written permission
> from the relevant rights holder(s), except where applicable law or GitHub's terms
> provide otherwise. Attribution alone is not permission. See [LICENSE](LICENSE).

## Overview

A Telegram application for coordinating equipment maintenance between operators,
mechanics, foremen and administrators. It connects repair requests, scheduled
preventive maintenance (PPR), shift attendance and reporting in one interface.

This repository contains a **sanitized source snapshot**. It does not include
production credentials, employees, requests, equipment inventories or plant documents.
The interface and source comments are primarily in Russian.

## Request workflow

```text
Operator / foreman creates a request
                  ↓
Mechanic accepts and selects an ETA
                  ↓
Work is completed and recorded
                  ↓
Requester submits a quality rating
                  ↓
Google Sheets records and performance points are updated
```

Scheduled reminders cover unaccepted requests and ETA follow-up. PPR has a separate
workflow for scheduled tasks, mechanic responses, postponement reasons and foreman review.

## Features in the source

| Area | Implementation |
| --- | --- |
| Repair requests | Equipment selection, urgency, photos, assignment, ETA, completion and quality ratings |
| Preventive maintenance | Scheduled notifications, postponement reasons, foreman review and workshop-specific notification settings |
| Shift attendance | Separate day and 24-hour shifts, with attendance records in Google Sheets |
| Performance feedback | Points, rankings, morning leaderboard messages and weekly summaries |
| Reporting | Google Sheets records, Excel exports through openpyxl and Matplotlib chart generation |
| Administration | User roles, workshop/equipment management, PPR setup, exports and backups |
| Persistence | Local JSON state, atomic file replacement and Google API retry configuration |

The code falls back to CSV when openpyxl is unavailable. Google Sheets and local
JSON writes are not a single transaction; API failures can require reconciliation.

## Roles

| Role in code | Main workflow |
| --- | --- |
| `operator` | Submit repair requests and evaluate completed work |
| `mechanic` | Accept work, select an ETA, complete requests/PPR and record attendance |
| `brigadir` | Submit requests and review PPR for selected workshops |
| `admin` | Manage users, equipment, schedules, reports and maintenance settings |

QR/deep-link onboarding is implemented, but QR links are **not secret or single-use
credentials**. Review the access model in [SECURITY.md](SECURITY.md) before deployment.

## Technology

- **Python / python-telegram-bot:** asynchronous handlers, menus and callback routing.
- **JobQueue / APScheduler:** scheduled PPR notifications and recurring reminders.
- **gspread / Google Sheets:** operational records and reporting worksheets.
- **JSON:** local users, requests, attendance, PPR state and scores.
- **openpyxl / Matplotlib:** spreadsheet exports and chart images.

## Repository layout

```text
bot.py                       Sanitized application source
requirements.txt             Pinned application dependencies
.env.example                 Empty configuration template — no credentials
.gitignore                   Excludes secrets, runtime data and exports
SECURITY.md                  Deployment cautions and privacy guidance
LICENSE                      All-rights-reserved copyright and permissions notice
docs/README.ru.md             Russian project overview
tests/test_source_safety.py   Offline source/configuration checks
```

The application is intentionally kept as a single source file in this snapshot.
No production database, logs, photographs, keys or original Git history are included.

## Safe local setup

These instructions describe the configuration for rights holders and authorized
users. They do not grant a license to run or reuse the code; see [LICENSE](LICENSE).

Use **a separate test bot, a blank test spreadsheet and a new service-account key**.
Do not connect this snapshot to a live factory environment just to try it out.

### 1. Install dependencies

Create a virtual environment with a Python version supported by the pinned packages:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, create the environment with `py -m venv .venv`; in PowerShell,
activate it with `.\.venv\Scripts\Activate.ps1`.

### 2. Configure the environment

Set these variables in your terminal or IDE run configuration:

| Variable | Required | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | Yes | Token of your new test Telegram bot |
| `ADMIN_ID` | Yes | Telegram ID of the test administrator; a positive integer |
| `SPREADSHEET_NAME` | Yes | Name of a separate test Google spreadsheet |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Recommended | Path to your new Google service-account JSON key |
| `PHOTO_CHANNEL_ID` | No | Test photo channel ID; `0` disables channel delivery |

**`.env.example` is documentation only. The application does not automatically load
`.env` files.** Without `GOOGLE_SERVICE_ACCOUNT_FILE`, it looks for
`service_account.json` beside `bot.py`; that file is not included.

Enable the appropriate Google Sheets/Drive APIs for the test service account and
share only the test spreadsheet with it. Keep its key outside this repository.

### 3. Start your configured test copy

```bash
python bot.py
```

The startup guard requires a bot token, a positive administrator ID, a spreadsheet
name and an existing key file before the application loads its operational state.
It checks configuration presence, not credential validity.

Workshop and equipment dictionaries start empty. Add your own test workshops,
sections, equipment and PPR tasks through the administrator menus. Runtime JSON files
are created as needed; no production data needs to be copied in.

## Offline checks

```bash
python -m unittest discover -s tests -v
```

These standard-library checks parse/compile the source **without importing or
running the bot**. They check empty operational defaults, environment-based settings,
startup guard ordering and common secret patterns. They do not contact Telegram or Google.

## Scope and limitations

- This is a source snapshot, not a packaged service or a security-hardened release.
- Publication checks are static. The published copy has not been exercised end-to-end
  against Telegram or Google Sheets.
- Request/PPR interactions, reporting and role permissions should be tested in an
  isolated environment before any real deployment.
- Importing the application initializes its logging; use the offline checks instead
  of importing it for inspection.
- Existing retry handling is not proof of exactly-once delivery. Review retry and
  duplicate-record behavior before relying on automated records.
- No user-count, time-savings or reliability metrics are claimed here.

## Engineering focus

The project combines asynchronous conversation workflows, role-specific menus,
scheduled jobs, external API integration, persistent local state and reporting.
The next useful improvements are modularization, mocked workflow tests, stronger
onboarding permissions and an explicit synchronization/reconciliation mechanism.
