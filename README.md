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
| The vendor's own status data | `statuspage`, `google_workspace`, `google_cloud`, `aws`, `page_text` | Services you don't run: Blackbaud, Finalsite, Google, AWS, your ISP. Vendors often post here before users notice. |
| Direct probes from the internet | `http`, `tcp` | Your own public-facing services: school website, VPN portal, mail server. |

Downdetector itself blocks automated requests, so it can't be used as a data source.

## What's monitored out of the box

| Service | How | Notes |
|---|---|---|
| **Blackbaud** | `statuspage` (status.blackbaud.com) | Education Management (SIS, LMS, Enrollment, Billing, School Website System), Blackbaud ID, Raiser's Edge NXT, Financial Edge NXT, Tuition Management. Products the school doesn't use are ignored. |
| **Finalsite** | `statuspage` (status.finalsite.com) | School Websites, Communication Tools (Messages XR, Mass Notifications), Enrollment Management |
| **Google Workspace** | `google_workspace` | Gmail, Drive, Classroom, Meet, Calendar, Docs/Sheets/Slides/Forms, Chat, Admin console |
| **AWS** | `aws` | us-east-1 / us-east-2. Many SaaS vendors run there, so this can be an early warning for problems at other vendors. |
| **ERC Broadband** | `page_text` (ercbroadband.com) | The NETWORK STATUS box on ERC's homepage. Any message other than "fully operational" sends an alert that quotes ERC's text. |

## What gets checked

| `type` | Required | Optional |
|---|---|---|
| `statuspage` | `url` (e.g. `https://status.blackbaud.com`) | `components = ["..."]` to watch only some parts |
| `google_workspace` | — | `products = ["Gmail", "Google Classroom", ...]` |
| `http` | `url` | `keyword`, `expected_status`, `timeout`, `degraded_latency_ms`, `method`, `headers`, `verify_tls` |
| `tcp` | `host`, `port` | `timeout` |
| `google_cloud` | — | `products = [...]` |
| `aws` | — | `regions = ["us-east-1"]`, `services = ["Amazon CloudFront", "ec2"]` |
| `page_text` | `url`, `ok_text` | `section_class` (HTML class of the element holding the status message), `problem_status` (`degraded`/`down`) |

All types also accept: `description` (included in the alert, e.g. "Expect
login tickets"), `notify` (extra recipients for that service only),
`failure_threshold`, `recovery_threshold`, `reminder_minutes`, and
`alert_on_degraded`.

**Which vendors use `statuspage`?** Any status page with "Powered by Atlassian
Statuspage" at the bottom. To check, open `<status page URL>/api/v2/summary.json`
in a browser. If you see JSON, it works. Component names for `components = [...]`
are listed in that JSON. Matching ignores capitalization and the ™/®/© symbols.
Naming a group (for example "School Websites") watches every component in it,
and alerts name the specific component that's affected.

### Avoiding false alarms

- A service has to fail **2 checks in a row** (`failure_threshold`) before you're alerted.
- It has to pass **2 checks in a row** (`recovery_threshold`) before you get "recovered".
- If a vendor's status page can't be reached, the result is `unknown`. Unknown results are
  logged but never alert, because a broken status page doesn't mean the service is down.
- While a service stays down you get a reminder every `reminder_minutes` (60 by default; 0 turns reminders off).
- If a check stays `unknown` for `unknown_alert_minutes` (120 by default), you get one
  **CAN'T CHECK** email. This catches cases where monitoring has quietly stopped,
  such as a vendor redesigning their status page.

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

3. **Test it**: *Actions → Outage monitor → Run workflow*, tick **"Also send a
   test email"**, and run. You should get a test email within a minute, and
   the run log shows every check's result. Normal runs only email when
   something changes, so a quiet inbox means everything is up.
4. That's it. Checks run every 5 minutes, around the clock, from the default branch.

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

- **How it stays running.** GitHub starts scheduled workflows on a
  best-effort basis. On this repo a 5-minute schedule fired only every few
  hours. So each "continuous" run stays alive for about 5h40m, checking every
  `interval_seconds`. When it starts, it queues the next continuous run, which
  takes over as soon as it ends. The hourly schedule is a safety net that
  restarts the chain if it ever breaks. In the Actions tab you'll see one run
  *in progress* for hours, and occasionally a *cancelled* run. Both are normal:
  only one run can wait in line, so a newer queued run replaces an older one.
- **Pausing monitoring.** Actions → Outage monitor → "…" → **Disable workflow**,
  then cancel the run in progress. Re-enable it to resume.
- **Actions minutes.** Public repositories run for free. A private repository
  would use the runner around the clock (about 43,000 minutes a month), far
  more than any plan includes. Keep this repo public (`config.toml` contains no
  secrets), or use a self-hosted runner, which is free.
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
