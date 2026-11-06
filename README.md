# Outage Detector

Email alerts when the services your users depend on stop working — so IT
knows about an outage **before** the tickets start arriving.

It runs on a schedule in **GitHub Actions**, so there is no server to host.
Every 5 minutes it:

1. Checks each service in [`config.toml`](config.toml)
2. Compares the results with the previous run (saved in `state.json`)
3. Emails you only when something changes: **DOWN**, **DEGRADED**, **still down**
   reminders, and **RECOVERED** (with how long the outage lasted)

## Why not Downdetector itself?

Downdetector has no public API, and its terms don't allow scraping. Instead,
this tool combines two better signals:

| Signal | Check type | Good for |
|---|---|---|
| The vendor's own status page | `statuspage`, `google_workspace` | SaaS you don't run: Google Workspace, Canvas, Zoom, Clever, GitHub, etc. The vendor often posts here before users notice. |
| Direct probes from the internet | `http`, `tcp` | Your own public-facing services: school website, SIS login page, VPN portal, mail server. |

## What gets checked

| `type` | Required | Optional |
|---|---|---|
| `statuspage` | `url` (e.g. `https://status.zoom.us`) | `components = ["..."]` to watch only some parts |
| `google_workspace` | — | `products = ["Gmail", "Google Classroom", ...]` |
| `http` | `url` | `keyword`, `expected_status`, `timeout`, `degraded_latency_ms`, `method`, `headers`, `verify_tls` |
| `tcp` | `host`, `port` | `timeout` |

All types also accept: `description` (included in the alert, e.g. "Expect
login tickets"), `notify` (extra recipients for that service only),
`failure_threshold`, `recovery_threshold`, `reminder_minutes`, and
`alert_on_degraded`.

**Which vendors use `statuspage`?** Any status page with "Powered by Atlassian
Statuspage" at the bottom. To check, open `<status page URL>/api/v2/summary.json`
in a browser. If you see JSON, it works. Component names for `components = [...]`
are listed in that JSON.

### Avoiding false alarms

- A service has to fail **2 checks in a row** (`failure_threshold`) before you're alerted.
- It has to pass **2 checks in a row** (`recovery_threshold`) before you get "recovered".
- If a vendor's status page can't be reached, the result is `unknown`. Unknown results are
  logged but never alert, because a broken status page doesn't mean the service is down.
- While a service stays down you get a reminder every `reminder_minutes` (60 by default; 0 turns reminders off).

## Setup (GitHub Actions)

1. **Edit `config.toml`**: add your services and uncomment the templates you need.
   The file contains no secrets, so it's safe to commit.
2. **Add repository secrets** (*Settings → Secrets and variables → Actions → New repository secret*):

   | Secret | Example |
   |---|---|
   | `SMTP_USERNAME` | `alerts@yourschool.org` |
   | `SMTP_PASSWORD` | an app password (see below) |
   | `ALERT_TO` | `it-team@yourschool.org, you@yourschool.org` |
   | `ALERT_FROM` *(optional)* | defaults to `SMTP_USERNAME` |
   | `SMTP_HOST` / `SMTP_PORT` *(optional)* | override `config.toml` |

3. **Test it**: *Actions → Outage monitor → Run workflow*. Look at the run log
   to see every check's result.
4. That's it. The workflow runs every 5 minutes on the default branch.

### Email provider notes

- **Gmail / Google Workspace**: `smtp.gmail.com`, port 587. Turn on 2-Step
  Verification for the sending account, then create an
  [App Password](https://myaccount.google.com/apppasswords) to use as
  `SMTP_PASSWORD`. If your Workspace admin has disabled app passwords, use
  the [SMTP relay service](https://support.google.com/a/answer/2956491)
  (`smtp-relay.gmail.com`) instead.
- **Microsoft 365**: `smtp.office365.com`, port 587. SMTP AUTH must be enabled
  for the sending mailbox (it's off by default in many tenants).
- **SendGrid, Mailgun, etc.**: use the host and port they give you.
  `security = "ssl"` is for port 465.

### Things to know about GitHub Actions

- **Run frequency.** GitHub's scheduler can start runs a few minutes late, and
  sometimes skips a run under heavy load. That's fine for "get ahead of the
  tickets," but don't treat it as a to-the-second monitor.
- **Actions minutes.** Public repositories run for free. Private repositories
  bill each run as at least 1 minute. Every 5 minutes works out to about
  8,600 minutes a month, which is more than the free 2,000. For a private
  repo, change the cron in [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml)
  to `*/30 * * * *` (about 1,450 min/month), or use a self-hosted runner
  (which is free).
- **Internal services.** GitHub's runners are on the public internet, so they
  can't reach anything that is only on your school network (an on-prem
  server, internal IPs). To monitor those, install a
  [self-hosted runner](https://docs.github.com/actions/hosting-your-own-runners)
  on any machine inside the network and change `runs-on: ubuntu-latest` to
  `runs-on: self-hosted`. You can also run the tool directly on that machine
  (see below).
- **60-day pause.** In public repositories, GitHub disables scheduled workflows
  after 60 days with no commits. GitHub emails you first, and re-enabling
  takes one click.
- **State.** `state.json` is carried between runs with the Actions cache. If
  the cache is ever cleared, the next run starts fresh. The worst case is
  one duplicate alert.

## Running it locally or on a server instead

Requires Python 3.11+ and nothing else: no packages to install.

```bash
python -m outage_detector check
```

Runs every check once and prints the results. It doesn't send email or save state.

```bash
SMTP_USERNAME=alerts@yourschool.org SMTP_PASSWORD=... ALERT_TO=you@yourschool.org python -m outage_detector test-email
```

```bash
python -m outage_detector run --once
```

Does one monitoring pass. Use it from cron or Task Scheduler.

```bash
python -m outage_detector run
```

Loops forever, checking every `interval_seconds`.

```bash
python -m outage_detector status
```

Shows the last known state of each service.

Add `--dry-run` to `run` to log alerts without emailing. Use `-c path/to/config.toml` to point at a different config file.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests use a local mock HTTP server and a mocked SMTP connection, so they
don't need network access or real credentials.
