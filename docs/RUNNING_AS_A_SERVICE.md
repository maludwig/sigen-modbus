# Running as a service (Linux, systemd)

`sigen poll` is meant to run for months. Started in a terminal, it ends when the terminal closes or
the machine reboots. As a systemd service it starts at boot, restarts after a crash, waits for the
clock to be right, and logs to the journal.

This setup puts the code in `/opt/sigen-modbus`, runs it as its own unprivileged `sigen` user, and
keeps the config (it holds passwords) in `/etc/sigen-modbus/`. Change the paths to taste; they
only have to agree with each other. It assumes PostgreSQL is already running and has the `sigen`
role and database (see the README's "Set up the database").

## Install

```
sudo useradd --system --home-dir /opt/sigen-modbus --shell /usr/sbin/nologin sigen
sudo git clone https://github.com/maludwig/sigen-modbus.git /opt/sigen-modbus
sudo python3 -m venv /opt/sigen-modbus/.venv
sudo /opt/sigen-modbus/.venv/bin/pip install -e /opt/sigen-modbus
```

The code stays owned by root: the service can read it but not change it. The install is editable
(`-e`), so a `git pull` updates the code (see "Updating").

## Config

```
sudo mkdir -p /etc/sigen-modbus
sudo cp /opt/sigen-modbus/config.example.json /etc/sigen-modbus/config.json
sudo nano /etc/sigen-modbus/config.json
sudo chown root:sigen /etc/sigen-modbus/config.json
sudo chmod 640 /etc/sigen-modbus/config.json
```

Readable by root and the `sigen` group only: it holds the database password and the ntfy topic.
The service finds it through `SIGEN_CONFIG` (set in the unit below). To run `sigen` by hand
against the same config, as the service would:

```
sudo -u sigen SIGEN_CONFIG=/etc/sigen-modbus/config.json /opt/sigen-modbus/.venv/bin/sigen db migrate
sudo -u sigen SIGEN_CONFIG=/etc/sigen-modbus/config.json /opt/sigen-modbus/.venv/bin/sigen db status
```

`db migrate` must have run before the service starts: `poll` refuses to start on a database that
isn't up to date.

## The unit

`/etc/systemd/system/sigen-poll.service`:

```ini
[Unit]
Description=sigen-modbus: poll a SigenStor into PostgreSQL
Documentation=https://github.com/maludwig/sigen-modbus
# Timestamps come from this computer's clock, so wait until it has been set (a Raspberry Pi has no
# battery-backed clock), and for the network and the database.
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target postgresql.service
# Keep retrying however often it fails: an outage can last hours.
StartLimitIntervalSec=0

[Service]
Type=exec
User=sigen
Group=sigen
WorkingDirectory=/opt/sigen-modbus
Environment=SIGEN_CONFIG=/etc/sigen-modbus/config.json
Environment=PYTHONUNBUFFERED=1
# What it does (interval, offset, weather, radar, alerts) is the config's "poll" section; options added here would win.
ExecStart=/opt/sigen-modbus/.venv/bin/sigen poll
# Ctrl+C, in effect: poll stops its background threads and prints its summary.
KillSignal=SIGINT
Restart=on-failure
RestartSec=30
# Restarting can't fix these; wait for a person (see "Exit codes" below).
RestartPreventExitStatus=2 4 7
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

and in `/etc/sigen-modbus/config.json`, the settings (see the README's "Config file"):

```
"poll": {"interval": 15, "weather": true, "radar": true, "alerts": true}
```

Changing them later is an edit of the config and `sudo systemctl restart sigen-poll`; the unit stays as it is.
`journalctl -u sigen-poll` shows, at each start, every setting and where it came from.

`time-sync.target` only waits for a real clock sync if a service provides it. With
systemd-timesyncd (Ubuntu, Raspberry Pi OS), enable its companion:

```
sudo systemctl enable systemd-time-wait-sync.service
```

Then start it, now and at every boot:

```
sudo systemctl daemon-reload
sudo systemctl enable --now sigen-poll.service
```

If PostgreSQL runs somewhere else (another machine, Docker), the `postgresql.service` in `After=`
does nothing and can stay: if the database isn't reachable yet, `poll` exits 10 and is restarted
30 seconds later.

## Watching it

```
systemctl status sigen-poll
journalctl -u sigen-poll -f                  # follow the log, one line per poll
journalctl -u sigen-poll --since today -p warning
```

`sigen db status` (as above) shows the last reading, the weather rows and the alerts from the
database's side.

## Exit codes

What the service does when `poll` stops:

| Exit | Meaning | The service |
|---|---|---|
| 2 | bad usage or an invalid config | stays stopped: fix the config, then `systemctl restart` |
| 4 | the device rejected a register: most likely a register-map bug | stays stopped: restarting would hit it again every 30 s |
| 7 | the database isn't migrated | stays stopped: run `sigen db migrate`, then restart |
| 10 | the database is unreachable (e.g. not up yet at boot) or rejected the data | restarts after 30 s |
| 1, a crash | anything unexpected | restarts after 30 s |

A device that's offline doesn't stop `poll` at all: those readings are stored with their error,
and polling carries on.

## Updating

```
cd /opt/sigen-modbus
sudo git pull
sudo .venv/bin/pip install -e .              # picks up new dependencies, if any
sudo -u sigen SIGEN_CONFIG=/etc/sigen-modbus/config.json .venv/bin/sigen db migrate
sudo systemctl restart sigen-poll
```

`poll` loads its code once, so a pull changes nothing until the restart.

**After a distribution upgrade** (e.g. Debian 13 → 14), the system Python moves to a new minor version
(3.13 → 3.14) and the venv's packages, installed for the old one, can't be imported: `sigen` fails at
start and systemd retries every 30 s, with the error in the journal. Rebuild the venv:

```
sudo rm -rf /opt/sigen-modbus/.venv
sudo python3 -m venv /opt/sigen-modbus/.venv
sudo /opt/sigen-modbus/.venv/bin/pip install -e /opt/sigen-modbus
sudo systemctl restart sigen-poll
```

Patch updates (3.13.5 → 3.13.6) need nothing: the venv follows the system Python.

## A Python the system never changes

To keep the service's Python out of the distribution's hands entirely, build one into `/opt` with
pyenv's builder ([python-build](https://github.com/pyenv/pyenv/tree/master/plugins/python-build), which
needs the usual build libraries: libssl, libffi, zlib, bz2, readline, sqlite3, lzma) and make the venv
from it instead of `python3`:

```
sudo python-build 3.9.25 /opt/python/3.9.25       # ~8 minutes on a Raspberry Pi 4
sudo /opt/python/3.9.25/bin/python3 -m venv /opt/sigen-modbus/.venv
sudo /opt/sigen-modbus/.venv/bin/pip install -e /opt/sigen-modbus
```

It then changes only when you change it, but it also gets no security fixes from the distribution.
Any version from 3.9 up works; 3.9.25 passed the full test suite on a Pi.

## The dashboard, too

The same pattern, as `/etc/systemd/system/sigen-dashboard.service`:

```ini
[Unit]
Description=sigen-modbus: the dashboard
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=exec
User=sigen
Group=sigen
WorkingDirectory=/opt/sigen-modbus
Environment=SIGEN_CONFIG=/etc/sigen-modbus/config.json
# The port and host are the config's "dashboard" section (default: every interface, port 8050).
ExecStart=/opt/sigen-modbus/.venv/bin/sigen dashboard
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=full
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

The dashboard has no login, so keep it on your own network (the README says more).

## More than one poller

Two machines polling the same SigenStor is fine for the device, but each writes its own database,
and with `--alerts` on both, every alert arrives twice: each has its own cooldown. Give
`--alerts` to one of them.

## MAC following

With `devices.inverter.mac` in the config, `poll` finds the inverter again if its IP changes. On
Linux that needs the `ip` command (iproute2), which Ubuntu and Raspberry Pi OS have; without it,
`poll` logs a warning at start and carries on without. A DHCP reservation for the inverter
avoids the question.

## Storage

Plant and inverter every 15 seconds write ~10 MB a day to PostgreSQL. On a Raspberry Pi, an SD
card wears out under a database's steady writes; a USB SSD for the system (or at least for
PostgreSQL's data) lasts far longer.
