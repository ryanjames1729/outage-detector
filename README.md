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
| The vendor's own status data | `statuspage`, `google_workspace`, `google_cloud`, `microsoft365`, `aws`, `page_text` | Services you don't run: Blackbaud, Finalsite, Google, Microsoft 365, AWS, your ISP, Canvas, Zoom. Vendors often post here before users notice. |
| Direct probes from the internet | `http`, `tcp` | Your own public-facing services: school website, VPN portal, mail server, and your campus internet connection itself. |

Downdetector itself blocks automated requests, so it can't be used as a data source.

## What's monitored out of the box

| Service | How | Notes |
|---|---|---|
| **Blackbaud** | `statuspage` (status.blackbaud.com) | Education Management (SIS, LMS, Enrollment, Billing, School Website System), Blackbaud ID, Raiser's Edge NXT, Financial Edge NXT, Tuition Management. Products the school doesn't use are ignored. |
| **Finalsite** | `statuspage` (status.finalsite.com) | School Websites, Communication Tools (Messages XR, Mass Notifications), Enrollment Management |
| **Google Workspace** | `google_workspace` | Gmail, Drive, Classroom, Meet, Calendar, Docs/Sheets/Slides/Forms, Chat, Admin console |
| **Microsoft 365** | `microsoft365` (Microsoft Graph) | Exchange, Teams, SharePoint, OneDrive, Entra ID, Intune, Microsoft 365 apps. **Needs a one-time app registration** (see below). |
| **AWS** | `aws` | us-east-1 / us-east-2. Many SaaS vendors run there, so this can be an early warning for problems at other vendors. |
| **ERC Broadband** | `page_text` (ercbroadband.com) | The NETWORK STATUS box on ERC's homepage. Any message other than "fully operational" sends an alert that quotes ERC's text. |
| **Campus internet** | `tcp` (template in config) | Recommended. See "Your internet connection" below. |
| Canvas, Zoom | `statuspage` | Remove these if you don't use them. |

## What gets checked

| `type` | Required | Optional |
|---|---|---|
| `statuspage` | `url` (e.g. `https://status.zoom.us`) | `components = ["..."]` to watch only some parts |
| `google_workspace` | — | `products = ["Gmail", "Google Classroom", ...]` |
| `http` | `url` | `keyword`, `expected_status`, `timeout`, `degraded_latency_ms`, `method`, `headers`, `verify_tls` |
| `tcp` | `host`, `port` | `timeout` |
| `google_cloud` | — | `products = [...]` |
| `microsoft365` | M365 credentials (below) | `services = ["Exchange Online", ...]`, `include_advisories` |
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
  such as an expired Microsoft client secret or a vendor redesigning their status page.

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
   | `M365_TENANT_ID`, `M365_CLIENT_ID`, `M365_CLIENT_SECRET` | for the Microsoft 365 check (below) |

3. **Test it**: *Actions → Outage monitor → Run workflow*. Look at the run log
   to see every check's result.
4. That's it. The workflow runs every 5 minutes on the default branch.

### Microsoft 365 setup

Microsoft doesn't publish a public status feed for Microsoft 365. The
accurate source is your own tenant's **Service health** (the same data as
admin.microsoft.com → Health → Service health), which you can read through
Microsoft Graph:

1. In the [Entra admin center](https://entra.microsoft.com), go to **App registrations → New registration**.
   Name it `Outage Detector`, keep "single tenant", and click Register.
2. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**.
   Add **`ServiceHealth.Read.All`**, then click **Grant admin consent**.
3. Go to **Certificates & secrets → New client secret** and copy the *Value*.
4. Add three repository secrets: `M365_TENANT_ID` (Directory/tenant ID on the
   app's Overview page), `M365_CLIENT_ID` (Application/client ID), and
   `M365_CLIENT_SECRET` (the value from step 3).

This permission can only read service health. It can't read mail, files, or
users. Client secrets expire (6–24 months). Set a calendar reminder to renew
it. If you forget, you'll get a "CAN'T CHECK" email.

### Your internet connection (ERC Broadband)

The ERC check reads ERC's own status message, but that only covers problems
ERC knows about and has posted. For your specific circuit, use the `tcp`
template in `config.toml`. GitHub's servers try to reach your firewall's
public IP from outside. If they can't, your internet is down. The alert is
sent from GitHub, so it reaches your phone over cellular while campus is
offline. A monitor running on campus couldn't do that.

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
