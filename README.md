# sigen-modbus

Monitor, log and carefully control a **Sigenergy SigenStor** home battery / hybrid inverter over
your local network, using its built-in Modbus TCP server. No cloud account needed.

- **Read** everything the system reports: solar per string, battery state of charge and
  temperatures, grid and house power, smart-load circuits, alarms and settings.
- **Log** it to PostgreSQL every few seconds, along with the weather at your site.
- **Look** at it in a web dashboard you can open from your phone, or query it with SQL or pandas.
- **Get alerts** on your phone (via [ntfy](https://ntfy.sh)) when something's wrong: the grid is
  down, the battery is too cold to charge, it dropped below its backup reserve.
- **Change** a small allowlist of settings (backup reserve, EMS mode), with every write read back
  and verified, and an undo.

This is an independent project, not affiliated with or endorsed by Sigenergy. Reading is
harmless; writing changes how your system behaves, so use `sigen set` with care.

## Before you start

On the SigenStor (through your installer or the mySigen app):
- Set the "Energy Management Settings" to **Remote EMS Mode**
- Set "Modbus TCP Server Settings" to enabled, and record the listed IP address.

It is recommended to give the inverter a fixed IP address (a DHCP reservation in your router),
so `sigen` always finds it at the same IP.

## Install

Python 3.9 or newer. Clone the repository:
```
git clone https://github.com/maludwig/sigen-modbus.git
cd sigen-modbus
```
then, in a virtual environment:

**Windows** (PowerShell):
```
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

**Ubuntu**:
```
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

That gives you the `sigen` command. `pip install -e ".[dev]"` adds pytest and the tools for
development (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)).

Following the inverter by MAC address (`sigen mac-lookup`, and `poll` finding a device whose IP
changed) works on Windows, and on Linux with the `ip` command (iproute2, standard on Ubuntu and
Raspberry Pi OS). Elsewhere `poll` warns and carries on without it.

Try it straight away, no database needed:
```
sigen scan                          # find SigenStors on your network
sigen read --ip 192.168.1.100       # a summary of the whole system
```

## Set up the database

Logging, the dashboard and alerts need PostgreSQL. Create a role and a database it owns:

```sql
CREATE ROLE sigen LOGIN PASSWORD 'PASSWORD';
CREATE DATABASE sigen OWNER sigen;
```

Copy `config.example.json` to `config.json` in the repository's root and fill in your own
values. At least the database URL, the inverter's IP, and your site's location:

```
"database": {"url": "postgresql://sigen:PASSWORD@localhost:5432/sigen",
             "test_url": "postgresql://sigen_test:PASSWORD@localhost:5432/sigen_test"},
```

(`test_url` is optional: a separate, throwaway database for the test suite.)

Create the tables:
```
sigen db migrate
sigen db status
```

`migrate` is safe to re-run; it applies whatever is new and does nothing otherwise. Run it as the
role in your URL, so that role owns the tables it writes to. Run it again after updating
`sigen`: `poll` won't start until the database is up to date. `db status` should end with every
table listed and exit 0.

Then start logging:
```
sigen poll --interval 15 --weather --radar --alerts
```
(or put those settings in the config's `"poll"` section, as `config.example.json` does, and run plain `sigen poll`).

and, in another terminal, the dashboard, remember to activate the venv again:
```
sigen dashboard
```

## Devices and unit IDs

Everything is reached through the inverter's one IP address on port 502, addressed by Modbus
unit ID:

| Unit | What |
|---|---|
| **247** | the **plant**: the whole system (grid sensor, smart loads, EMS, settings) |
| **1** (set in the app) | the **inverter** (Sigenergy's "Energy Controller"): PV strings, battery cells, grid per leg |

Commands read the plant only, unless the config lists inverter units
(`devices.inverter.units: [1]`); then `read`, `capture`, `read-json` and `poll` cover both. A
unit ID with nothing behind it simply doesn't answer: the read times out (exit 3). The Sigen
LoadHub (the North American backup gateway) has its own IP but no Modbus.

## Commands

Every command has `--help`. Connection options shared by the device commands: `--port`
(default 502), `--unit-id` (the plant's, default 247), `--timeout` (seconds per reply, default
3). Commands that use the config find `config.json` by themselves (see "Config file");
`--config FILE` picks another one, `--no-config` uses none.

### Reading

```
sigen read [--ip 192.168.1.100] [--json]
```
Reads every register (read-only) and prints a summary: EMS mode, alarms, battery (available /
total kWh, temperature), solar, battery and grid power, house load, smart loads by name, and,
with inverter units in the config, an inverter section (model, firmware, per-string PV, grid
voltage per leg, battery cell temperatures, its alarms). `--json` prints every register instead
(value, unit, raw words). Without `--ip` it uses `devices.inverter.ip` from the config, or scans
this computer's /24 (`--ip-range`, `--scan-timeout` to control the scan).

```
sigen capture [--ip …] [--out-dir captures] [--name-prefix my_system]
```
Same as `read --json`, saved to `captures/my_system_<UTC time>Z.json`. Never overwrites a file.

```
sigen read-json --data-file captures/….json [--summary-only]
```
Pretty-prints a saved capture with no device: the summary, then a table of every register.

```
sigen scan [--ip-range 192.168.1.170-180]
```
Prints the IPs on the network that answer like a SigenStor. Ranges: `START-END`, short
`192.168.1.170-180`, or CIDR.

```
sigen registers [--writable]
```
Lists the register map: device, address, type, unit, name; `W` marks the ones `set` can write.

### Writing settings

```
sigen set --ip 192.168.1.100 --backup-soc 20                       # dry run: shows what would change
sigen set --ip 192.168.1.100 --backup-soc 20 --apply               # write
sigen set --ip 192.168.1.100 --remote-ems-mode max-self-consumption --apply
sigen set --ip … --backup-soc 20 --expect-current 0 --apply        # only if it's currently 0
sigen undo --ip 192.168.1.100 [--apply]                            # revert the last write
```
Only two settings are writable: `--backup-soc` (the backup reserve, 0–100 %) and
`--remote-ems-mode` (`pcs-remote-control` or `max-self-consumption`). Start/stop, grid-code
settings and export/import limits are deliberately left out. Every write is read back and
verified, and logged to a journal (`~/.local/state/sigen-modbus/journal.jsonl`) that `undo` uses;
`undo` refuses if the setting was changed by something else since. `set` and `undo` need an
explicit `--ip` and never read the config, so a write never goes to a scanned or configured
address by accident. `--json` prints the result as JSON.

### Database and logging

```
sigen db migrate
```
Creates or upgrades the tables. Safe to re-run.

```
sigen db status
```
Read-only: which config file it used, migrations applied or pending, reading counts, the last
reading, the weather and alert tables, and **each table's size** with its growth over the last
24 hours (per day, and × 365 per year). Exit 0 if `poll` can run against this database, 7 if
not.

```
sigen db daily [--from DATE] [--to DATE]
```
Stores each finished day's energy in `daily_energy` (solar, the house, the battery in and out, the
grid in and out, and the flows between them), so a month or a year is one `SUM`. With the config's
`site` and `array`, it also stores the day's clear-sky model energy (`clear_sky_kwh`), which the
all-time page compares the day's solar with. Without dates: the days not stored yet; with them, that
range, recomputed. `poll` does this by itself every 10 minutes.

```
sigen db query "SELECT * FROM readings ORDER BY id DESC LIMIT 10" [--json] [--max-rows 1000] [--max-width 80]
```
Runs SQL and prints the rows as a table (or JSON). **Read-only**: anything that would change the
database is refused (exit 10). Several statements work, e.g. local times:
`sigen db query "SET TIME ZONE 'America/Edmonton'; SELECT started_at, plant_ess_soc_pct FROM plant_register_values ORDER BY reading_id DESC LIMIT 5"`.

```
sigen poll [--ip …] [--interval 60] [--offset 0] [--count N] [--[no-]weather] [--[no-]radar] [--[no-]alerts] [--[no-]cloud]
```
Each of `--interval`, `--offset`, `--weather`, `--radar`, `--alerts` and the capture options can
also come from the config's `"poll"` section; the command line wins (`--no-alerts` over `"alerts":
true`). At start `poll` prints every setting, its value, and where it came from (command line,
config or default).
Reads every unit on a schedule (lined up with the clock: `--interval 15` polls at :00, :15, :30,
:45) and stores each reading. If a second computer polls the same device, give it an `--offset`
(e.g. `--offset 7`: :07, :22, …), so the two don't read it at the same instant. Runs until Ctrl+C unless
`--count`. `--weather` and `--radar` also record the weather at your site in the
background; `--alerts` watches the battery and pushes problems to your phone (see "Alerts").
It also stores each finished day's energy (see `sigen db daily`). When logging its output to a
file, set `PYTHONUNBUFFERED=1`.

When something goes wrong, `poll` carries on where it can:
- the device doesn't answer, drops the connection or is busy: the reading is stored with its
  error, and polling continues. With a MAC in the config, after 3 failed polls in a row
  it looks the inverter up by MAC and carries on at its new IP;
- the database is unreachable: a warning, that poll isn't stored, polling continues;
- the device's reading is a glitch (a total load of exactly 0 while Running, or a PV power of exactly
  0 while the strings carry power: see [docs/SIGENSTOR_NOTES.md](docs/SIGENSTOR_NOTES.md#quirks)):
  it's stored with the reason in `readings.glitch`, and the next poll comes a second later instead
  of on the schedule, up to twice in a row. Each is an ordinary poll (counted by `--count`). The
  dashboard, the daily totals and the alerts leave flagged polls out, both devices' readings. A PV
  zero with the strings under 0.5 kW may be real (dusk): it's only flagged when a retry replaces
  it, so the last attempt is always kept;
- weather, radar and alert problems never stop it;
- it **stops** on a Modbus exception (exit 4: most likely a register the device doesn't support)
  or a database that rejects the data (exit 10), since retrying wouldn't help.

To run it for good (at boot, restarted after a crash, logging to the journal), see
[docs/RUNNING_AS_A_SERVICE.md](docs/RUNNING_AS_A_SERVICE.md).

```
sigen dashboard [--host 0.0.0.0] [--port 8050] [--reload]
```
A web page of live charts, drawn in your browser by [uPlot](https://github.com/leeoniya/uPlot)
(loaded from a CDN) from the database's numbers: where the window's energy went (a Sankey
diagram: solar to the house, the battery and the grid, and so on); each PV string's voltage, and
its power against its clear-sky model (from the config's `site` and `array`) with strips
underneath: how the measured power compares to that model (**PV / clear-sky**), the radar's rain
or snow at your site, and NOAA's satellite cloud; energy and power
now by load; smart-load energy and power; the battery's temperatures; and battery and solar power
with the state of charge and reserve. A menu picks the window: today, the last 10 min to 7 days,
or any day. A window that reaches now is topped up every 15 s with only the newer readings
(`/data/<chart>.json?since=…`, which your own code can use too). Times show in your browser's time
zone. Opening the page also fetches that window's satellite data in the background (`sigen
satellite`, see below). **Daily totals** (`/daily`) shows a day's totals and every flow;
**All-time** (`/all-time`) each finished day's solar as a line, with a strip under it per day (solar
as a share of its clear-sky model, the radar's rain, NOAA's sun through the cloud), and a Sankey of
every finished day in `daily_energy` added up.
It listens on every interface, so a phone on the same network can open
`http://<this computer's IP>:8050/` (printed at start; Windows Firewall asks the first time).
Read-only. `--reload` restarts it when its code changes, and re-reads changed page templates.

**The dashboard has no login.** Anyone who can reach the port can see your power use, your
battery and when you're home. Keep it on your own network: never forward the port from your
router. `--host 127.0.0.1` makes it reachable from this computer only.

### Alerts

`poll --alerts` checks the battery after every reading and sends a push notification through
[ntfy](https://ntfy.sh) to the topic in your config: subscribe to the same topic in the ntfy
phone app. It needs `--weather` too, for the outside air temperature.

| Alert | When | Priority |
|---|---|---|
| `fault` | the SigenStor reports a fault (e.g. the rapid-shutdown button), with its alarms in plain words | urgent |
| `alarm` | any alarm bit (inverter, battery, gateway, DC charger, plant), in plain words; the grid going down is one | high; urgent for the grid down or thermal runaway |
| `unreachable` | no reading at all for 20 polls in a row (5 min at `--interval 15`) | high |
| `below_reserve` | the battery is below its backup reserve | urgent |
| `cold_charging` | charging while the coolest cells are at 2 °C or below | urgent |
| `cold` | the coolest cells are at 5 °C or below | high |
| `plateau`, `warming`, `sawtooth` | the battery holding or gaining heat on its own while idle (a heater?) | high |
| `unexplained_power`, `idle_drain` | power going somewhere it shouldn't while idle | high |
| `cooling` | on track to reach 5 °C within 6 hours | default |
| `glitch` | a total load of 0 while Running (see "Polling"), with its battery, grid and PV power. PV-zero glitches are recorded in the database but not sent: low sun brings them most days | default |

Each event alerts once, and each kind (each alarm bit, for `alarm`) at most once an hour. Every
message ends with when the event began, e.g. `2026-10-12T19:49:30-06:00` (local time, the config's
`site.timezone`). Unless the SigenStor is Running (in a fault, shut down, or starting up), its
battery readings aren't real, so only `fault` can alert: the other alerts ignore every reading up to
a minute after the last one that wasn't Running. One
`unreachable` per outage; contact coming back is recorded in the database but not sent. Every
alert, sent or held back, is recorded in the database.

```
sigen ntfy-test [--msg "Hello world"] [--priority min|low|default|high|urgent] [--record-to-db]
```
Sends one test notification to your topic.

### Weather

```
sigen weather [--resolution 60 15] [--record-to-db] [--capture DIR]
sigen radar   [--record-to-db] [--capture DIR]
```
One fetch each, for trying things out; `poll --weather --radar` does the same on a schedule.
`weather` prints [Open-Meteo](https://open-meteo.com)'s latest conditions at your site (a
weather model, every hour and every 15 minutes); `radar` prints Environment Canada's radar
composite at your site (rain and snow rate, a frame every 6 minutes; it covers Canada and the
northern US). Both are free with no key. What each source is, its quirks and its licence:
[docs/DATA_SOURCES.md](docs/DATA_SOURCES.md).

```
sigen satellite [--day 2026-10-10 | --start "2026-10-10 06:00" --end "…"] [--km 200] [--record-to-db]
```
Saves NOAA GOES-West cloud data (cloud mask, optical depth, cloud-top height; every 10 minutes)
for a box around your site, a day at a time, as HDF5 files for your own analysis (comparing the
clouds with what your panels made, say). GOES-West covers western North
America and the Pacific. A day takes about a minute and ~210 MB of downloads; re-running only
fetches what's new. The files go in the config's `satellite.cube_dir`. Each file is updated by
writing a copy and swapping it in, so readers never see half of one, and only one run at a time
works in a folder: a second one exits with 7 while the first is still going. With
`--record-to-db` it also works out, per scan, how much sun gets through the cloud over the house
and stores it in `satellite_shade` (the dashboard's satellite strips read it from there, and starts
`sigen satellite --record-to-db` itself for the window you open).

### The cloud

`sigen` can also read your system from Sigenergy's **mySigen cloud**, for the few things the local
Modbus server doesn't expose: the **LoadHub / gateway**'s grid, inverter and generator
measurements and its contactor states (the LoadHub has no Modbus at all), and each **battery
pack's heating status**. It uses mySigen's own app API over HTTPS, is read-only, and needs your
mySigen account. Everything else in `sigen` works without it.

```
sigen cloud login     # prompts for your mySigen password and the emailed code; saves the tokens
sigen cloud refresh   # mint a fresh access token from the saved refresh token (no password)
sigen cloud query     # the batteries' heating status (--path /device/gateway/<stationId>, --sn, --json)
```
An access token lasts about 12 hours; the refresh token is single-use, so each `refresh` rotates
it and saves the new one (written atomically). `login` is the one interactive step (your password);
after that `refresh` keeps access alive with no password. Only
one process should refresh at a time — `poll` owns it while it runs (it keeps more than an hour of
life in hand), so `query` never refreshes on its own: it errors if the token has expired, and you
run `cloud refresh`. Needs the config's `"cloud"` section (below).

The tokens live in the file named by `cloud.token_file`, never in `config.json`; keep that file out
of anything you share, and create it with `sigen cloud login`. `login` and `refresh` also need the
app's two static constants, `cloud.obfuscation_key` and `cloud.client_basic` (below) — the same for
every mySigen install, but kept in your config rather than baked into this code.

**Battery heater logging.** With a `"cloud"` section in the config, `sigen poll` logs each battery
pack's heating status to the `battery_heater` table from a background thread. A cloud problem never
stops the Modbus poll; it refreshes the token at launch, and polls every 10 minutes at rest, or
every minute when the battery might be heating (PV over 0.2 kW and the coolest cluster below 15 °C,
or a heater already on). It's on by default whenever a cloud section is present; `--no-cloud` (or
`poll.cloud: false`) turns it off without removing the config — which also frees the cloud session it
would otherwise hold (the mySigen account allows only so many at once). Restart `poll` after changing it.

### Finding gaps

```
sigen db gaps                                        # the last 7 days, every table
sigen db gaps --since 30d --table readings battery_heater --min-gap readings:45s
sigen db gaps --json
```
Read-only. For each table's time series, the stretches with no good row for longer than its **min
gap**: by default 3 × the interval its rows normally come at, so a gap is at least three missed in
a row. The series: the plant's readings (every `poll.interval`), each battery pack's heater checks
(the packs take turns: 10 min × the number of packs), Open-Meteo at each resolution (15 and 60
min), radar frames (6 min) and the satellite scans (10 min, daylight only). Each gap shows how many
attempts failed inside it, with their commonest error, or "no attempts stored" if nothing was
running. The satellite is fetched only while a dashboard page is open, so its gaps often just mean
nobody was looking. `daily_energy` lists its missing days instead.

- `--since` (default `7d`) takes `45s`, `30m`, `12h`, `7d`.
- `--min-gap TABLE:DURATION …` overrides some tables' min gaps; `--table TABLE …` checks only those.
- Times are in the site's timezone; `--json` gives UTC ISO times.

### Syncing between machines

If two machines poll the same SigenStor into separate databases (see "Config file"), an outage on
one — a reboot, a deploy, a crashed database — leaves a gap the other usually has. `sigen sync`
fills it: it reads a time window from the other machine's dashboard and writes in the rows this
database is missing.

```
sigen sync --url http://othermachine:8050 \
           --start-at 2026-10-17T16:00:00Z --end-at 2026-10-17T17:30:00Z [--only-sync weather] [--dry-run]
sigen sync --url http://othermachine:8050 --fill-gaps [--since 30d] [--min-gap readings:45s] [--dry-run]
```
- `--url` is the other machine's **dashboard** base URL; that machine must be running `sigen
  dashboard` (it serves the read-only `/sync/*` endpoints). Nothing on the source is modified.
- `--start-at` / `--end-at` are UTC ISO 8601; the window is half-open `[start, end)`.
- `--fill-gaps`, instead of a window: finds this database's gaps over `--since` (as `sigen db
  gaps`, with the same `--min-gap`) and syncs each one. A gap that stays empty was the device's
  (the other machine missed it too), not this machine's.
- `--only-sync` picks what to copy, as one or more kinds (default: all): `weather` (Open-Meteo),
  `radar` (Environment Canada frames), `heater` (the battery-heater log), `satellite` (the per-scan
  cloud shade), and `readings` (the readings and their raw register values, regrouped into fresh
  polls locally).
- `--dry-run` reports what it would import, per kind, and writes nothing.

Only good rows are copied, database ids are reassigned locally, and a row is skipped if this
database already has it (matched on a natural key, e.g. the weather period), so `sync` is safe to
re-run and over overlapping windows. **Both databases must be on the same migration version**
(`sigen db migrate` on each), or `sync` refuses. When it imports `readings`, it recomputes the
affected days' totals in `daily_energy` automatically (`daily_energy` itself is never copied).

### The config file

```
sigen config show                                    # the whole file, secrets masked; then OK or the problem
sigen config get poll.interval                       # one value (or a section: sigen config get poll)
sigen config set poll.interval 15                    # change one value
sigen config set site.timezone America/Edmonton      # a value that isn't JSON is a string: no quotes needed
sigen config set alerts.ntfy_topic --stdin           # a secret, read from standard input
sigen config unset poll.radar                        # remove a value or a whole section
```
For changing the config without hand-editing JSON (no stray or missing commas), and over ssh without showing its
secrets. Keys are dotted paths (`devices.inverter.ip`, `smart_loads.0.name`); values are JSON (`15`, `true`,
`'{"ip": "192.168.1.100"}'`), and `--string` keeps one a string. Every change is checked with the same rules
sigen loads the file by, and refused (exit 2) before anything is written; then the file is replaced atomically,
after a copy of the old one to `config.json.bak`, keeping its owner and mode (so 640 root:sigen stays that way on a
server). `--dry-run` checks and shows a change without writing it (exit 8). Database passwords, the ntfy topic and
the cloud's keys are masked in everything printed (`get --reveal` shows one). Which file: as for every command (see
[Config file](#config-file)), or `--config FILE`.

### Network

```
sigen mac-lookup --ip 192.168.1.100                  # print its MAC
sigen mac-lookup --mac 02:ab:cd:ef:00:01             # find its IP
```
Windows, and Linux with the `ip` command; no admin rights needed. A /24 takes ~15 s on Windows and
~3–8 s on Linux.

### Testing without a device

```
sigen fake-server [--port 10502] [--fake-data-dir fake_data/real-2026-10-08-both-units]
```
A fake SigenStor that replays a capture, and behaves like the real device. Point any command at
it with `--ip 127.0.0.1 --port 10502`. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md#fake-server-and-data).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK (for writes: verified by read-back, or already at the target) |
| 1 | unexpected error (e.g. `capture` would overwrite a file) |
| 2 | bad usage or invalid config |
| 3 | couldn't connect, the connection dropped, or it timed out (incl. a wrong unit ID); a weather fetch failed |
| 4 | the device returned a Modbus exception (incl. a refused write) |
| 5 | **verify failed**: the read-back doesn't match, or the state is unknown after a dropped write |
| 6 | refused by the allowlist |
| 7 | precondition failed: `--expect-current`, undo after an outside change, the database isn't migrated, or `sigen satellite` is already running |
| 8 | dry run: a change is needed but `--apply` wasn't given (or `sigen config set/unset --dry-run`) |
| 9 | not found: no SigenStor found, nothing to undo, or a missing file |
| 10 | database unreachable, or it rejected a query |

`--json` on `read`, `scan`, `set` and `undo` prints machine-readable output on stdout; progress
messages go to stderr.

## Config file

Commands find their config by themselves, in this order:
1. `--config FILE`, if given (`--no-config` instead: use none);
2. the file named by the `SIGEN_CONFIG` environment variable;
3. the nearest `config.json` in the current directory or above it.

To read or change it without hand-editing the JSON, use `sigen config` (see
[The config file](#the-config-file)): every change is checked before it's written, and secrets are masked.

Every section is optional; a command that needs one says so. Start from `config.example.json`:

```
{
  "smart_loads": [{"slot": 1, "name": "Garage"}, {"slot": 3, "name": "Heat Pump"}],
  "database": {"url": "postgresql://sigen:PASSWORD@localhost:5432/sigen",
               "test_url": "postgresql://sigen_test:PASSWORD@localhost:5432/sigen_test"},
  "devices": {"inverter": {"ip": "192.168.1.100", "mac": "02:ab:cd:ef:00:01", "units": [1]},
              "loadhub":  {"ip": "192.168.1.101", "mac": "02:ab:cd:ef:00:02"}},
  "site": {"latitude": 51.0443, "longitude": -114.0631, "timezone": "America/Edmonton",
           "altitude_m": 1045},
  "alerts": {"ntfy_topic": "a-long-random-topic", "ntfy_server": "https://ntfy.sh"},
  "array": {"panel": {"kw": 0.5, "gamma_pct_per_c": -0.28, "mount": "close_mount_glass_glass"},
            "albedo": 0.2,
            "faces": {"south": {"tilt": 20, "azimuth": 180}, "west": {"tilt": 20, "azimuth": 270}},
            "strings": {"1": {"panels": 10, "face": "south"}, "2": {"panels": 8, "face": "west"}}},
  "satellite": {"cube_dir": "captures/satellite/cubes"},
  "cloud": {"token_file": "scratch/cloud_token.json",
            "base_url": "https://api-us.sigencloud.com", "app_base_url": "https://app-us.sigencloud.com",
            "obfuscation_key": "the-apps-static-key", "client_basic": "base64-of-the-apps-oauth-client",
            "station_id": "1234567890123456", "batteries": ["110AXX1234000A", "110AXX1234000B"]},
  "poll": {"interval": 15, "offset": 0, "weather": true, "radar": true, "alerts": true},
  "dashboard": {"host": "0.0.0.0", "port": 8050, "reload": false}
}
```
- `smart_loads`: names for the smart-load circuits (slots 1–24), for display.
- `database`: `url` for `sigen db …`, `poll` and the dashboard (and your own code, see "Exploring
  the data in Python"); `test_url`, a separate database for the tests. Passwords are masked in
  everything sigen prints.
- `devices.inverter`: `ip` lets commands run without `--ip`; `units` adds the inverter's own unit
  ID(s); `mac` lets `poll` find the inverter again if its IP changes (Windows, or Linux with `ip`).
- `site`: where the panels are, for the weather, the satellite and the clear-sky model. The
  timezone is an IANA name; `altitude_m` is optional.
- `alerts`: your ntfy topic (and server, default https://ntfy.sh). Anyone who knows the topic can
  read and post to it, so make it long and random, and keep it out of anything you share.
- `array`: your panels, for the dashboard's clear-sky model: the panel (kW at STC, temperature
  coefficient, pvlib mounting type), the ground albedo, each roof face's tilt and azimuth (degrees
  clockwise from true north), and which face each PV input's string is on and how many panels it
  has.
- `satellite`: where `sigen satellite` keeps its files and the dashboard reads them. A relative
  `cube_dir` is relative to the config file's folder (the default: `captures/satellite/cubes` there).
- `cloud`: the mySigen cloud (see "The cloud"). `token_file` is a path (relative to the config
  folder) where the rotating access and refresh tokens are kept — gitignore it; it is not a secret
  in `config.json` itself. `station_id` and the `batteries` list (pack serials in the app's order,
  Battery 1 first) name what `cloud query` and the poll's heater job read. `base_url` and `app_base_url`
  are your region's API and web-app hosts (both default to the US ones).
  `obfuscation_key` and `client_basic` are the app's two static constants (the password key and the
  OAuth client credential): the same for every mySigen install, but kept here rather than in the
  code so the source carries neither. `login` needs both, `refresh` needs `client_basic`, and a
  plain `query` with a valid token needs neither. As of this writing, an LLM can trivially find the
  `obfuscation_key` and `client_basic` from the webapp, at: https://app-us.sigencloud.com/
  Simply ask it to find the AES key and the Basic Auth string from the webapp.
- `poll`, `dashboard`: what `sigen poll` and `sigen dashboard` do on this computer when the command
  line doesn't say, named like their options without the dashes: `poll` takes `interval`,
  `offset`, `weather`, `radar`, `alerts`, `cloud`, `capture_open_meteo_weather` and `capture_radar`;
  `dashboard` takes `host`, `port` and `reload`. An option on the command line wins; a setting in
  neither is the option's default. Handy when two computers poll the same device: each one's
  `offset` and `alerts` live in its own config.

`config.json` is in `.gitignore`: it holds your passwords and your location.

## Database

Tables:
- `readings`: one row per device per poll, including failed ones (with the error), so gaps are
  explained. `glitch` says why a poll's values aren't to be trusted (`total_load_zero`, `pv_zero`,
  or `pv_zero_suspect` for one a retry replaced); add `AND glitch IS NULL` to leave them out.
- `raw_plant_register_values`, `raw_inverter_register_values`: every register of each successful
  reading, one column per register, as the device sent it.
- `plant_register_values`, `inverter_register_values` (views): the same, scaled to real units,
  with the unit in each column's name (`plant_ess_soc_pct`, `plant_photovoltaic_power_kw`). Start
  here.
- `register_meta`: each register's name, type, scale and unit.
- `open_meteo_weather`: the weather model's conditions at your site, hourly and every 15 minutes.
- `radar_frames`: the radar's rain and snow rate at your site, every 6 minutes.
- `alert_log`: every alert, sent or held back.
- `daily_energy`: each finished day's energy, one row per local day: each lifetime counter's
  increase between this computer's readings at the two midnights, and the flows between solar,
  the battery, the house and the grid. `SELECT sum(solar_kwh) FROM daily_energy WHERE day >=
  '2027-01-01'` is a year's solar. Days add up exactly; a gap in polling moves energy to the day
  before the gap. `clear_sky_kwh` is the clear-sky model's energy for the day (NULL without a
  `site` and `array`).
- `satellite_shade`: one row per NOAA GOES-West scan (every 10 minutes): the sun's elevation
  (`sun_elevation_deg`), the share of sunlight through the cloud where a cloud at the measured top
  or at 0 / 2 / 5 / 10 km would shade the house (`trans_*`, 0 to 1), and the cloud probability
  there (`prob_*`, 0 to 1). Written by `sigen satellite --record-to-db`.
- `battery_heater`: one row per cloud poll of a battery pack's heating status (`sigen poll` with a
  `cloud` section): `polled_at`, `battery` (1 or 2, the pack's place in `cloud.batteries`),
  `heater_on` (NULL on a cloud error), `error`.
- `schema_migrations`: which migrations have been applied.

Plant and inverter every 15 seconds take about 10 MB a day (~4 GB a year); `sigen db status`
shows each table's actual size and growth.

Example queries:
```
-- SOC and solar over time, scaled
SELECT started_at, plant_ess_soc_pct, plant_photovoltaic_power_kw
FROM plant_register_values ORDER BY started_at;

-- string 1, raw (before the gain)
SELECT started_at, inverter_pv1_voltage, inverter_pv1_current
FROM raw_inverter_register_values WHERE started_at > now() - interval '1 hour';

-- plant and inverter readings of the same poll
SELECT * FROM readings WHERE poll_id = 4000;
```

## Exploring the data in Python

The scaled views are the easiest place to start. In a script or a Jupyter notebook (pandas comes
with `sigen`), `get_config()` finds your `config.json` the same way the commands do:

```python
import pandas as pd
import psycopg
from sigen.config import get_config

with psycopg.connect(get_config().require_database_url()) as conn:
    cur = conn.execute("SELECT started_at, plant_ess_soc_pct, plant_photovoltaic_power_kw "
                       "FROM plant_register_values WHERE started_at > now() - interval '1 day' "
                       "ORDER BY started_at")
    df = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])

df = df.set_index("started_at").astype(float)     # the views' numbers come back as exact decimals
df.plot(subplots=True)
```

Mind the energy counters (`…_kwh`): they move in 0.01 kWh steps, so compare them over 15 minutes
or more; for anything shorter, use the power readings (`…_kw`).

## More

- [docs/RUNNING_AS_A_SERVICE.md](docs/RUNNING_AS_A_SERVICE.md): `poll` (and the dashboard) as
  systemd services on Linux, e.g. a Raspberry Pi.
- [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md): the weather, radar and satellite data, their
  quirks and licences.
- [docs/SIGENSTOR_NOTES.md](docs/SIGENSTOR_NOTES.md): what a real SigenStor does, beyond the
  protocol document: unsupported registers, quirks, the battery's behaviour.
- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md): the code's layout, tests, how reads and writes
  work, the register map, migrations, the fake server.

## License

MIT: use it for anything, at your own risk. See [LICENSE](LICENSE). Sigenergy and SigenStor are
Sigenergy's trademarks; this project isn't affiliated with them.
