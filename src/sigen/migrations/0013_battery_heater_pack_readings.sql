-- The rest of each cloud poll's answer about the pack (sigen.heater): besides its "Heating status", the cloud reports
-- the pack's own SOC, charge/discharge power and voltage -- values the Modbus registers only give for the whole
-- battery. The heater job stores them with each poll from now on, for working out when the heater runs. NULL in the
-- rows from before this migration, and in a failed poll's row (the answer's "dataCurrTimeStamp" is always empty, so
-- it isn't kept).
ALTER TABLE battery_heater
    ADD COLUMN soc_pct   REAL,   -- the pack's "Battery SOC", %
    ADD COLUMN power_kw  REAL,   -- its "Charging & Discharging Power", kW: + charging, - discharging
    ADD COLUMN voltage_v REAL;   -- its "Battery Pack Voltage", V (about 3.2 V x its modules: 6 or 9 here)
