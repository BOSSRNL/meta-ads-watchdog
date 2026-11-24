# Meta Ads Watchdog

**Auto-pauses overspending Meta ad campaigns, flags possible account hacks, and switches to a looser sale-day mode on paydays and double-day sales (11.11, 12.12). Alerts go to Discord. A daily reactivator turns the overspent campaigns back on.**

Meta lets a campaign spend past its daily budget. When you run dozens of campaigns across several ad accounts, nobody can watch them all. This watchdog runs every 30 minutes, checks every active campaign, and pauses the ones breaking a rule.

## What it checks

Every run, for each active campaign:

| Check | What happens | Discord keyword | Turned back on tomorrow? |
|---|---|---|---|
| Budget jumped **x10** since the last run | Paused, and the alert says who changed it | `security` | No, check it first |
| **New** campaign (under 24 h old) with a **1,000+** budget | Paused, and the alert says who created it | `security` | No, check it first |
| A single budget of **45,000+** (an extra zero?) | Paused | `anomaly` | No, check it first |
| Spend today reached **40,000** | Paused | `emergency pause` | No, check it first |
| Spend went past budget **+ tolerance** (normal day) | Paused | — | **Yes** |
| Spend reached **2x** the budget (sale day) | Paused | `escalation` | **Yes** |
| Overspent but under 2x (sale day) | Left running, alerted once | — | — |
| Ad account disabled or unpaid | Alerted once a day, account skipped | `issue` | — |

**Tolerance** gives small overspends some room before pausing:

| Daily budget | Paused at |
|---|---|
| under 100 | budget + 10 |
| 100 to 999 | budget + 30 |
| 1,000 to 9,999 | budget + 150 |
| 10,000 and up | budget + 250 |

All amounts are in the ad account's currency. The defaults were tuned for accounts in Philippine pesos, so **change them in the config if your currency is different**.

### Sale days

On sale days, overspending is expected and good, so the watchdog stops pausing at the budget. It alerts once per campaign and pauses only at 2x the budget. The security, anomaly and 40,000 checks stay on. Sale days are:

- **Paydays**: the 15th and the 30th (the last day of February).
- **Double-day sales**, also called double-digit sales: 1.1, 2.2, through 11.11 and 12.12.
- **Any extra dates** you add, like a flash sale.

The first run of a sale day posts a "Sale-day mode is ON" message.

### Budgets

- **Campaign budget (CBO)**: the campaign's own daily budget.
- **Ad set budgets (ABO)**: the sum of the *active* ad sets' daily budgets. The 45,000 anomaly check looks at the single largest one. Today's spend of an ad set that was paused earlier in the day still counts, so turning ad sets off mid-day makes the campaign look closer to its limit.
- **Lifetime budgets**: there is no daily limit to compare with, so only the budget-jump check and the 40,000 spend limit apply. The new-campaign check looks at daily budgets only.

## Safe by design

- **Dry run is the default.** A new install pauses nothing and turns nothing on; it only reports what it *would* do. You switch it to live yourself.
- **The reactivator only turns on overspend pauses from before today.** Security, anomaly and spend-limit pauses always wait for a person, even if the same campaign was on the list from an earlier overspend. A campaign paused this afternoon is not turned back on tonight.
- **The reactivator double-checks.** It only turns on a campaign that Meta says belongs to one of the team's ad accounts. An entry it cannot handle after 7 days moves to `needs-attention.jsonl` and is listed in Discord.
- **A failed pause is tried again.** If Meta refuses a pause, the next run tries again (the alert is sent once a day).
- **A person's decision sticks.** If you check a campaign paused for a budget jump or as a big new campaign and turn it back on, the watchdog does not pause it again for that reason. (Anomaly and spend-limit pauses come back if the budget or spend is still over the line.)
- **No lost alerts.** If Discord is down or the internet drops, alerts are queued and sent on the next run. If Discord refuses an alert for good (for example, the webhook was deleted), the run ends with exit code 1, so the scheduler shows a failure.
- **Trouble with files never stops a pause.** A damaged list or queue file (saved by another editor, say) is set aside as `*.damaged-<time>` and the watchdog carries on.
- **No overlapping runs.** Each team has a lock that the running scan keeps fresh. A lock left by a crashed run expires after 20 minutes.
- **Meta API limits are respected.** Throttled calls are retried with backoff, and every list is paged.
- **Secrets never reach the logs.** The token and the webhook URLs are removed from every log line, even inside error messages.
- **Campaign names are treated as untrusted.** A name cannot ping `@everyone`, add fake lines, show links, or hide text with invisible characters.

## Setup

You need **Python 3.10+**, a **Meta access token**, and one or more **Discord webhooks**.

**1. Install**

```bash
git clone https://github.com/BOSSRNL/meta-ads-watchdog.git
cd meta-ads-watchdog
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install .
```

**2. Get a token.** In Meta Business Settings, create a **System User**, give it access to your ad accounts, and generate a token with the `ads_management` permission. A System User token does not expire like a personal one.

**3. Get Discord webhooks.** In your Discord channel: Edit Channel > Integrations > Webhooks > New Webhook > Copy URL. Use one channel per team, plus an optional "issues" channel that gets only the urgent alerts.

**4. Configure**

```bash
cp config.example.json watchdog.config.json
cp .env.example .env
```

Put your ad account IDs, team names and **time zone** (your ad accounts' time zone, for example `"Asia/Manila"`) in `watchdog.config.json`, and the token and webhook URLs in `.env`. Both files are in `.gitignore`.

**5. Check it.** This command only reads; it changes nothing on Meta:

```bash
meta-ads-watchdog check-config --online
```

It lists each ad account with its status, currency and time zone, and flags an account whose time zone differs from the config.

**6. Try a dry run**

```bash
meta-ads-watchdog scan --verbose
```

Read the output and the Discord messages (marked 🧪 DRY RUN). When they look right, set `"dry_run": false` in the config to go live.

## Run it on a schedule

The watchdog runs every 30 minutes. The reactivator only acts on campaigns paused before today, so it is safe at any hour: run it **every hour**, and a run that is skipped or fails is caught up by the next one.

**Windows (Task Scheduler).** Use a folder without spaces, for example `C:\watchdog`. `/RU` and `/RP *` make the tasks run even when nobody is logged in (you are asked for your Windows password once):

```bat
schtasks /Create /TN "Meta Ads Watchdog" /SC MINUTE /MO 30 /RU %USERDOMAIN%\%USERNAME% /RP * /TR "C:\watchdog\.venv\Scripts\meta-ads-watchdog.exe scan --config C:\watchdog\watchdog.config.json"
schtasks /Create /TN "Meta Ads Reactivator" /SC HOURLY /RU %USERDOMAIN%\%USERNAME% /RP * /TR "C:\watchdog\.venv\Scripts\meta-ads-watchdog.exe reactivate --config C:\watchdog\watchdog.config.json"
```

On a laptop, open each task in Task Scheduler and, under Conditions, untick "Start the task only if the computer is on AC power". Under Settings, tick "Run task as soon as possible after a scheduled start is missed".

**macOS / Linux (cron)**, with `crontab -e`:

```cron
*/30 * * * * cd /opt/watchdog && .venv/bin/meta-ads-watchdog scan
5 * * * *    cd /opt/watchdog && .venv/bin/meta-ads-watchdog reactivate
```

Many ad accounts? Run each team as its own task with `--team "Team A"` so the teams are checked in parallel. Each command and team writes its own log file.

## Commands

| Command | What it does |
|---|---|
| `meta-ads-watchdog scan` | Check every active campaign and pause the ones breaking a rule |
| `meta-ads-watchdog reactivate` | Turn back on the campaigns paused for overspending before today |
| `meta-ads-watchdog check-config` | Validate the config; add `--online` to test the token and each ad account (read-only) |

Options for every command: `--config PATH` (default `watchdog.config.json`), `--team NAME` (repeatable) and `--verbose`. `scan` and `reactivate` also take `--dry-run`, which forces a dry run even when the config says live.

Exit codes: `0` done, `1` something failed (details in the log), `2` the config is invalid, `3` a team was skipped because another run for it was still going.

## Config reference

| Key | Default | Meaning |
|---|---|---|
| `dry_run` | `true` | `false` pauses and turns on campaigns for real |
| `language` | `"en"` | Alert language: `"en"` English or `"tl"` Taglish. The search keywords stay English in both |
| `timezone` | required | Decides when a day (and a sale day) starts, e.g. `"Asia/Manila"`. Use your ad accounts' time zone |
| `state_dir` | `"state"` | Where the watchdog keeps its files, relative to the config file |
| `graph_version` | `"v25.0"` | Meta Graph API version |
| `teams[].name` | required | Shown in alerts; also the name of the team's folder |
| `teams[].ad_accounts` | required | IDs like `"act_1234567890"` |
| `teams[].discord_webhook_env` | none | Name of the `.env` variable holding the team's webhook URL |
| `teams[].discord_issues_webhook_env` | none | Same, for the urgent-only channel |
| `rules.overspend_tolerance` | see above | `[[min_budget, allowed_overspend], ...]`, starting at 0 |
| `rules.hard_spend_limit` | `40000` | Pause when today's spend reaches this |
| `rules.anomaly_budget` | `45000` | Pause when a single daily budget reaches this |
| `rules.spike_multiplier` | `10` | Pause when the budget grows this many times between runs |
| `rules.new_campaign_budget` | `1000` | Pause a new campaign with at least this budget |
| `rules.new_campaign_hours` | `24` | How long a campaign counts as new |
| `rules.sale_day_pause_multiplier` | `2` | On sale days, pause at this many times the budget |
| `sale_days.payday_days` | `[15, 30]` | Days past the month's end fall on its last day |
| `sale_days.double_days` | `true` | 1.1 through 12.12 are sale days |
| `sale_days.extra_dates` | `[]` | More sale days, as `"YYYY-MM-DD"` |
| `sale_days.announce` | `true` | Post "Sale-day mode is ON" once on each sale day |

Unknown keys are rejected, so a typo in the config cannot be silently ignored.

## Files it keeps

Inside `state/<team>/`:

| File | Purpose |
|---|---|
| `paused-campaigns.jsonl` | The campaigns the reactivator will turn back on |
| `needs-attention.jsonl` | Entries the reactivator will not handle: archived or deleted campaigns, foreign accounts, unreadable lines, failures older than 7 days. Look at them, then delete the file |
| `state.json` | Last seen budgets, campaigns held for a person, and what was already alerted today |
| `discord-queue.jsonl` | Alerts waiting to be sent (only while Discord is unreachable; dropped after 7 days) |
| `backup/` | A copy of the paused list before each reactivation that changes it (last 30 kept) |
| `*.damaged-<time>` | A file that could not be read (for example, saved in another encoding), kept for you to check. The log says when this happens |

Logs are in `state/`: `scan.log` and `reactivate.log`, or `scan-team-a.log` when you run one team with `--team`. Each rotates at 1 MB and keeps 5 files.

**To keep an overspent campaign off**, delete its line from `paused-campaigns.jsonl` before the reactivator runs (or archive the campaign in Ads Manager). Otherwise it is turned back on the next day.

A dry run still writes `state.json` (budgets seen and alerts already sent, so it does not repeat itself), but never the paused list.

## Limits

- **Meta's spend numbers lag behind real spend**, and the watchdog checks only every 30 minutes. A campaign can go somewhat past a threshold before it is paused. The 40,000 limit is a safety net, not an exact cap.
- **Meta only.** Google and TikTok ads are not covered.
- **It checks campaigns, not ads.** Pausing an overspending campaign pauses everything in it.
- **Only active ad accounts are checked.** An account in any other state (disabled, unpaid, in a grace period, pending settlement) gets one `issue` alert a day and is skipped until Meta reports it as active again.
- **It can only see what the token can see.** An ad account missing from the System User's assets fails its check and gets an `issue` alert.
- **One time zone per config.** Days, sale days and "paused before today" follow the config's `timezone`. If your ad accounts use different time zones, give each zone its own config file and state folder.

## Development

```bash
pip install -e ".[test]"
pytest --cov
```

The tests use a fake Meta API and never call the real one.

## License

MIT. Use it, fork it, adapt it. A link back is appreciated.

---

Built by [Rannelle](https://github.com/BOSSRNL) · Builder of e-commerce funnels & AI automations 🇵🇭
