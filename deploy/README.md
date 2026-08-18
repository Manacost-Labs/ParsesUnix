# Deploying ParserUnix on debian-151

The production target is a single Debian server driven by a systemd timer. State
(SQLite queue, dataset, freshness, snapshots) is local to the server. No paid
providers are used by the free core.

## One-time setup

```bash
# On the server (user: debian)
cd ~
git clone https://github.com/Manacost-Labs/ParsesUnix.git
cd ParsesUnix
python3 -m pip install --user -e .          # installs ws-probe / ws-triage / ws-profile / ws-budget / ws-run

# Enable the L2 browser level (JS rendering + CSR API reconnaissance):
python3 -m pip install --user -e '.[browser]'
python3 -m playwright install --with-deps chromium   # --with-deps needs sudo for OS libs
# Without this the core still runs; L2 routes are simply reported as skipped.

mkdir -p ~/.config/web-scraper ~/ParsesUnix/state
cp deploy/run.example.json ~/.config/web-scraper/run.json
cp deploy/env.example ~/.config/web-scraper/env && chmod 600 ~/.config/web-scraper/env
# Put your validated Site Profile(s) somewhere the run.json 'profile' path resolves to,
# e.g. ~/ParsesUnix/profiles/<domain>.yaml, and edit run.json accordingly.

# Validate the profile before the first run (no network):
ws-profile validate ~/ParsesUnix/profiles/<domain>.yaml

# Install the timers (user units):
mkdir -p ~/.config/systemd/user
cp deploy/web-scraper*.service deploy/web-scraper*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now web-scraper.timer web-scraper-review.timer
loginctl enable-linger debian     # let user timers run while logged out
```

## Deploying an update

```bash
cd ~/ParsesUnix
git pull
python3 -m pip install --user -e .   # picks up any new console entry points
systemctl --user daemon-reload       # only if unit files changed
```

The next timer firing uses the new code. State is preserved across deploys, and a
run interrupted by a deploy resumes from the queue (no duplicates).

## Operating

```bash
ws-run ~/.config/web-scraper/run.json --verbose          # run once by hand
systemctl --user start web-scraper.service               # trigger the unit now
journalctl --user -u web-scraper.service -n 100          # logs / ALERT lines
cat ~/ParsesUnix/state/last-report.json                  # coverage + unresolved + dead zones
```

`ALERT` lines in the journal cover circuit-breaker trips, extractor quorum
conflicts, promote rejections, and dead zones.

## What the run does each window

1. Resume: return any crashed IN_PROGRESS rows to the queue.
2. Freshness re-crawl: re-open DONE urls whose interval elapsed.
3. Process each URL through the free routes (L0 → L1 → L2), triaging every
   response; 404/410 → quarantine, unresolved-by-anything → dead zone.
4. Extract fields (JSON-LD → app-state → meta → CSS → heuristic) with a quorum
   on critical fields, stage the rows.
5. Validate staging as a whole and atomically promote to the clean dataset, or
   reject and keep the last-known-good version (with an alert).
6. Write `state/last-report.json`.

## Security notes

- Secrets live only in `~/.config/web-scraper/env` (0600), never in the repo,
  profiles, snapshots, or reports.
- The units run unprivileged with `ProtectSystem=strict` and a narrow
  `ReadWritePaths`.
- SSRF protection blocks private/loopback/metadata targets on every hop; keep it
  that way (do not pass `allow_private`).
