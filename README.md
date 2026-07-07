# Free Job Alerts Automation

This repository watches target companies for matching backend/AI/platform roles, scores them against Saurabh Kumar's profile, writes a daily report, and optionally sends notifications.

The automation is intentionally free-first:

- no paid scraping service
- no LLM API required
- no Python package install required
- GitHub Actions scheduled runs supported
- Telegram, Discord, SMTP email, and Adzuna are optional

## What It Watches

The company watchlist is in `config/job_watch_config.json` and includes all 100 companies shared in the request.

The matcher is tuned for:

- Java / Spring Boot backend roles
- SDE-2 / Backend Engineer / Platform Engineer roles
- Kafka / microservices / distributed systems
- AI backend / LLM integration / embeddings / recommendation systems
- fintech/payment backend roles
- India or remote-friendly openings

## How It Finds Jobs

The script uses free sources:

1. Public ATS job feeds:
   - Greenhouse
   - Lever
   - Ashby
   - Workable
   - Recruitee
   - Personio
2. Remote OK public API
3. Adzuna API, if free API keys are provided
4. Custom RSS feeds, if you add them in config

Not every company exposes a public feed. For those companies, use job-alert emails or add a custom RSS/source when available.

## Local Run

From this folder:

```bash
python3 job_alerts.py discover --force
python3 job_alerts.py run
```

The first command discovers public ATS feeds and writes:

```text
data/discovered_sources.json
```

The second command writes:

```text
outputs/latest_job_matches.md
outputs/latest_job_matches.json
data/seen_jobs.json
data/job_history.jsonl
```

To report only unseen jobs:

```bash
python3 job_alerts.py run --only-new
```

If your local macOS Python fails with certificate errors, use this only for local testing:

```bash
JOB_ALERTS_INSECURE_SSL=1 python3 job_alerts.py run
```

Do not set `JOB_ALERTS_INSECURE_SSL` in GitHub Actions; the hosted runner verifies certificates normally.

To refresh direct ATS discovery and notify:

```bash
python3 job_alerts.py run --only-new --force-discovery --notify
```

## Telegram Notification Setup

1. Open Telegram and message `@BotFather`.
2. Create a bot and copy the token.
3. Message your new bot once.
4. Get your chat ID from:

```text
https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```

5. Export local env vars:

```bash
export TELEGRAM_BOT_TOKEN="your-token"
export TELEGRAM_CHAT_ID="your-chat-id"
python3 job_alerts.py run --only-new --notify
```

For GitHub Actions, add the same values as repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Optional Adzuna Setup

Adzuna is free within their personal/research API limits.

1. Create an Adzuna developer account.
2. Get `app_id` and `app_key`.
3. Export locally:

```bash
export ADZUNA_APP_ID="your-app-id"
export ADZUNA_APP_KEY="your-app-key"
python3 job_alerts.py run --only-new
```

For GitHub Actions, add:

- `ADZUNA_APP_ID`
- `ADZUNA_APP_KEY`

## Optional Email Setup

Set these environment variables:

```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="your-email@gmail.com"
export SMTP_PASSWORD="your-app-password"
export SMTP_FROM="your-email@gmail.com"
export SMTP_TO="target-email@gmail.com"
python3 job_alerts.py run --only-new --notify
```

Use an app password, not your normal Gmail password.

## GitHub Actions Setup

1. Create a GitHub repository.
2. Push these files.
3. Go to repository Settings -> Secrets and variables -> Actions.
4. Add notification/API secrets if needed.
5. Open the Actions tab.
6. Run `Job Alerts` manually once.

The workflow also runs automatically:

- 09:00 IST, Monday-Friday
- 18:00 IST, Monday-Friday

It commits updated state files so repeated notifications are avoided.

## Adding RSS Feeds

Edit `config/job_watch_config.json`:

```json
"rss": {
  "enabled": true,
  "feeds": [
    {
      "name": "custom-backend-alert",
      "url": "https://example.com/jobs/rss"
    }
  ]
}
```

## Improving Company Coverage

Some companies use Workday, custom career sites, or logged-in portals that do not expose stable public feeds. For those:

1. Create job alerts on LinkedIn/Naukri/Instahyre manually.
2. Add Google Alerts for targeted queries.
3. Add any RSS feed URL into config.
4. If you discover a company ATS slug, add it as an alias in `config/job_watch_config.json`.
5. Re-run:

```bash
python3 job_alerts.py discover --force
```

## Recommended Google Alerts

Create alerts for:

```text
"Software Development Engineer II" "Java" "India"
"Backend Engineer" "Spring Boot" "Kafka" "India"
"Platform Engineer" "Java" "Distributed Systems" "India"
"AI Backend Engineer" "LLM" "India"
"Recommendation Systems Engineer" "India"
"Adobe" "Backend Engineer" "India"
"Microsoft" "Software Engineer II" "India"
```

Use these as a backup layer because Google Alerts can catch custom career pages that public ATS APIs miss.
