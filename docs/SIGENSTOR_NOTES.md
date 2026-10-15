# SigenStor notes

What one real system taught us, beyond (or contrary to) Sigenergy's Modbus protocol document.
Observed on a **SigenStor EC SP US** (single inverter, split phase 120/240 V, firmware
V100R001C22SPC117) with two batteries (BAT 10.0 + BAT 6.0, LiFePO4) and a Sigen LoadHub, against
protocol V2.9. Other models and firmware may differ: check before relying on any of it.

## Addressing

- One IP, port 502. The **plant** (the whole system) is unit **247**; each inverter has its own
  unit ID, set in the app (here **1**); 0 is broadcast (writes to all, no reply).
- Plant and inverter address ranges never overlap (plant 30000–30286 / 40000–40159 / 50000+,
  inverter 30500–31526 / 40500–41507), so one address map serves both.
- A **wrong unit ID times out**; it doesn't return Modbus exception 11.
- The inverter ignores ping and has no open port besides 502.
- The **LoadHub** (the North American backup gateway, with the smart circuits) is on the network
  but has no Modbus and no open TCP ports; it talks to the inverter via as yet unknown means.

## Reading

What the device accepts in one read (input or holding registers):
- A read that **starts** on an unsupported address, or **splits** a multi-word register at either
  end, is rejected (exception 2). At most 125 words per read.
- Anything else inside a read is fine: addresses outside the map return data, and unsupported
  ones read `0xFFFF`.

So a block read can't tell you a register is unsupported; it just reads `0xFFFF`. **Check a new
register on its own before relying on it.** Rejected on this hardware and firmware:
- plant 30073–30082 (reserved), 30279 and 30281;
- inverter PV strings 5–36 (this model has 4 MPPTs);
- the ESS preheating settings, 50000–50183 (new in protocol V2.9; this firmware predates them),
  on both units, input and holding.

Of the ~200 registers that do read, about 115 never changed over the first days: ratings, unused
smart-load slots, hardware that isn't there, "not set" limits, and alarms and statuses (whose
change is the event, so they're worth logging anyway).

## Quirks

- **The clock** (30000) holds local wall-clock time as if it were a UTC epoch, and the timezone
  register reads the standard-time offset even during daylight saving time. Timestamp readings
  with your own clock.
- **30012** ("max apparent power") is documented in kvar but is kVA; it equals 30010.
- **Total load** (30284) equals general load (30282); smart loads are part of it. "Load excluding
  EV chargers" (30228) equals the house load (30094) when the EV charges from a plain outlet on a
  smart circuit: only a Sigen-integrated EV charger counts as one.
- **Glitched readings**: now and then, for a single reading, the device hands over summary values
  that don't belong together. Two kinds so far, a few a day in all:
  - the plant's total load exactly 0 while Running, usually with other powers wrong too (e.g. the
    battery discharging 0.6 kW and the grid exporting 1 kW, with no sun, between two ordinary
    readings). It lasts under ~12 s: a reader a few seconds either side sees normal values.
  - the PV power (the inverter's, usually the plant's too) exactly 0 while all four strings' voltage
    and current stay steady (e.g. 1.61 → 0 → 1.51 kW while the strings made 1.9 kW, and the inverter's
    AC output plus the battery still matched them). The load, the battery power or the phase currents
    are often wrong in the same reading. Only with the sun low (under ~20°), never with the strings
    above 2 kW, always a single reading above 0.5 kW. Below that, at dusk, the PV can really be 0
    while the strings' V × I still reads a few hundred watts.

  `sigen poll` flags such a poll (`readings.glitch`, `sigen.glitches`) and reads again a second
  later; the dashboard, daily totals and alerts leave flagged polls out.
- **Alarms** (plant 30027–30030, 30072, 30280; inverter 30605–30609) decode with the protocol's
  appendices; `0xFFFF` means "not available", not 16 alarms.
- **Energy counters** move in 0.01 kWh steps: good for energy over 15 minutes or more, useless at
  15-second intervals (they read 0 or 2.4 kW). Power registers have 1 W resolution and about ±10 W
  of noise.
- **Smart loads**: per-circuit power and lifetime energy only. They can't be switched locally;
  the app switches them through Sigenergy's cloud.
- **Temperatures**: only the battery (average cell 30286 / 30603, warmest and coolest cluster
  30620 / 30621) and the inverter's internal temperature (31003). No ambient or panel sensor.

## The battery

- **Capacity**: 30066 is the energy still deliverable, 30064 the room left; their sum is the BMS's
  live total, which moves with temperature. SOC (30014) is exactly 30066 ÷ that sum. 30083 is
  just the nameplate.
- **SOC limits**: backup reserve (40046), charge cut-off (40047, mirrored in 30085) and discharge
  cut-off (40048). At the charge cut-off, charging stops dead and surplus solar is exported. Below
  the backup reserve, the battery **charges from the grid at full power** up to the reserve, then
  holds.
- **Charge acceptance** drops near full: from the rated 7.3 kW to ~1.7 kW above ~97 % here.
- **Cold**: the BMS throttles charging (30047, the allowed charge power): ~1.7–2.3 kW at 7 °C,
  ~3.6 kW at 10 °C, full power from ~16 °C, extrapolating to ~0 at 0–1 °C. It depends on more
  than the coolest cluster (SOC, perhaps).
- **Self-heating?** Twice, cold cells (~8 °C) warmed at up to ~10 °C/h as soon as charging began,
  and kept warming briefly after it stopped. That's far more than charging current explains, so
  probably a heater in the pack, but its power shows up in no register.
- **Round trip** at the battery's DC port: ~96 % (discharging ~0.150 kWh per SOC point on 15 kWh
  of batteries, charging ~0.156). Just below 100 % the BMS re-trims SOC, so the first points down
  from full are "short".
- **Standby**: about 26 W when idle. The DC balance (PV − battery − AC power) measures it well at
  idle but reads ~0 under load, where a real inverter loses ~100 W: those registers can't resolve
  a few hundred watts.

## The solar side

- The system is **DC-coupled**: solar charges the battery without going through the inverter, so
  it can feed the battery (≤ 7.3 kW here) and the AC side (≤ 11.5 kW) at once. Clipping only
  happens when the battery is full or slowing near full.
- The sum of the strings' V × I is ~1.06 × the inverter's PV power register.

## Writing

- Writes only take effect with the EMS mode set to **Remote EMS** in the app.
- Remote EMS control mode (40031): 0 PCS remote control, 1 standby, 2 maximum self-consumption,
  3 charge from grid first, 4 charge from PV first, 5 discharge PV first, 6 discharge ESS first,
  8 V2G. `sigen` only allows 0 and 2.
- The backup reserve (40046) was written and read back successfully many times; it takes effect
  at once.
