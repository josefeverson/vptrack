# YveScanner

Local Python tool for watching public vote pages, estimating the server Vote Party counter, and timing a short Minecraft join window.

> Bringing plushie and roaming profits direct to your computer.

It does not automate voting, CAPTCHA solving, website form submission, chat spam, movement loops, or anti-AFK behavior. Minecraft actions are dry-run notifications by default.

## Quick Start

Use any Python 3.11+.

Install the local CLI:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Then initialize config, calibrate, and poll once:

```bash
vp-track --init-config
vp-track --calibrate 73
vp-track --once
```

Then run continuously:

```bash
vp-track --daemon
```

Run the native desktop dashboard:

```bash
vp-track --gui
```

Run tracking and dashboard together:

```bash
vp-track --daemon --gui
```

Useful commands:

```bash
# Show current estimate, confidence, recent snapshots
vp-track --status

# Reset local tracker history
vp-track --reset-db --calibrate 73 --once

# Print full analytics as JSON
vp-track --stats

# Machine-readable service health for monitors/systemd
vp-track --healthcheck
```

## How It Works

- Polls your configured public vote sources.
- Parses monthly/public counts where available.
- Parses recent voter lists as source-local vote events.
- Treats each vote website as an independent source; it does not globally deduplicate usernames across sites.
- Deduplicates recent-list entries only within the same source, so one player voting on multiple sites can still count as multiple valid votes.
- Supports per-source direct HTTP polling with an optional reader fallback for sites that block scripted direct requests.
- Supports per-source timestamp timezone settings for recent-voter lists that publish relative times such as `today at 2:37 AM`.
- Uses a SQLite poll lock so the daemon and dashboard cannot count the same source increment at the same time.
- Caps single-source reader-backed bursts by default, recording the raw jump while applying only the safer effective delta.
- Stores snapshots and events in `vp_tracker.sqlite3`.
- Stores per-source poll deltas, source reliability, and estimated Vote Party crossings.
- Applies vote deltas to the calibrated in-game VP count modulo 120.
- Raises polling frequency near the party:
  - normal: 15 seconds
  - near `100/120`: 10 seconds
  - trigger zone `116/120`: 5 seconds
- Computes confidence from calibration freshness, count-source success, resets, failures, and large jumps.
- Leaves stale count-source inference disabled by default; faster polling and calibration logs are safer than guessing delayed site updates.

## Dashboard And Statistics

`vp-track --gui` opens the YveScanner native desktop dashboard. It shows:

- compartmentalized native tabs for overview, source health, voters, forecasting, and audit/debug views
- current VP estimate, remaining votes, confidence, and calibration age
- time until the next poll, plus a live manual poll-interval override with dynamic-auto reset
- ETA consensus plus fast/slow ETA bands from rolling velocity windows
- 5m, 15m, 30m, 1h, 6h, 12h, 24h, and 7d vote counts
- long-range vote-flow chart with selectable time windows, time labels, per-poll deltas, VP position, and cumulative trend
- per-source vote contribution, skipped polls, reader fallback use, and reliability
- source diagnostics for long-running failures, skipped polls, reader fallback use, solo-source deltas, and catch-up bursts
- delta trace showing which source contributed each observed vote increase
- delayed source catch-up diagnostics and suppressed single-source burst visibility
- latest observed voters with local vote and detection time labels
- username frequency stats over recent windows
- peak/quiet voting hours and Vote Party probability by hour
- burst frequency, burst size, downtime impact, missed-vote estimate, and estimate drift/error stats
- trusted source ranking, website update delay, source overlap, likely full-site voters, and voting streaks
- no-vote streaks, failure rate, poll spacing, max deltas, and burstiness
- hourly vote-density heatmap
- estimated Vote Party crossing history and interval summaries
- pop-out windows for dense tables and the vote-flow chart
- visual plus optional sound notification when new vote activity is detected, including the detected vote amount
- runtime notification toggle for silencing those vote popups/sounds without restarting
- calibration mismatch logging with signed error and severity for debugging estimate drift

Optional browser fallback:

```bash
vp-track --web --gui-port 8766
vp-track --web --no-open
```

The browser API intentionally omits source URLs. Keep real server URLs in local `config.json`, which is ignored by git.

## Server Deployment Groundwork

The tracker can run headless on a small Linux server with the daemon command:

```bash
vp-track --config /etc/vptrack/config.json --daemon
```

Use the health check for uptime monitors, cron, or systemd watchdog wrappers:

```bash
vp-track --config /etc/vptrack/config.json --healthcheck
```

Example systemd unit and timer files live in `deploy/systemd/`. They are generic templates using `/opt/vptrack` and `/etc/vptrack/config.json`; edit those paths on the target host. Keep the real config private and outside git.

## Opsec Defaults

- Public defaults are generic and disabled.
- Real source URLs belong only in ignored local config.
- Runtime files, virtualenvs, databases, and downloaded tools are ignored.
- The browser fallback binds to loopback by default and uses a random per-run URL token.
- Auto-join is OFF by default. The default behavior is notification-only unless you deliberately enable the dashboard toggle or config flag.
- Avoid committing logs, screenshots, local paths, server names, usernames, or webhook URLs.

## Calibration

When you see the real in-game line:

```text
Someone just voted! VP-count; 73/120!
```

run:

```bash
vp-track --calibrate 73
```

If you configure `minecraft.latest_log_path`, the tool can also read `latest.log` and recalibrate from chat lines automatically.

## Minecraft Action Modes

`config.json` defaults to:

```json
"minecraft": {
  "mode": "notify"
}
```

In notify mode, the tracker prints and sends desktop notifications only. It will say when it would prelaunch, join, or disconnect.

To permit automatic joining, first enable the dashboard `Auto-Join` toggle or set:

```json
"auto_join_enabled": true
```

To run local commands after that, change:

```json
"mode": "commands"
```

and fill in:

```json
"prelaunch_command": "",
"join_command": "",
"disconnect_command": ""
```

The commands are deliberately user-supplied because different launchers handle Fabric/modpack instances differently. Hook commands receive environment variables:

- `VP_ESTIMATE`
- `VP_PARTY_SIZE`
- `VP_REMAINING`
- `VP_CONFIDENCE`

Keep these hooks simple: launch/open/join/disconnect only. Do not add movement loops, jump loops, chat spam, or anything AFK-like.

## Safety Behavior

- Low confidence: notify instead of auto-joining.
- High confidence: join around `118/120`.
- Medium confidence: join around `119/120`.
- Dynamic velocity can join earlier if the vote rate means the party may happen during your join latency.
- If chat confirms a VP count below `115/120` after joining, it disconnects.
- Target online window warning at 240 seconds.
- Hard disconnect at 300 seconds.
- Reward-like chat lines containing configured plushie patterns schedule a disconnect shortly afterward.

## Files

- `vp_tracker.py`: tracker CLI and daemon.
- `config.example.json`: generic config reference; copy/fill locally.
- `deploy/systemd/`: generic service and healthcheck examples for 24/7 hosts.
- `config.json`: local editable config, created by `--init-config` and ignored by git.
- `vp_tracker.sqlite3`: local state database, created automatically.
- `tests/test_vp_tracker.py`: parser and helper tests.
