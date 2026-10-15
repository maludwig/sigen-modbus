// The uPlot dashboard's arithmetic (src/sigen/static/live_charts.js): means, resampling, topping up a table.
// Run: node --test tests/js/live_charts.test.js (tests/test_js.py runs it from pytest).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const L = require(path.join(__dirname, "..", "..", "src", "sigen", "static", "live_charts.js"));

const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} is not ${b} (±${eps})`);
const every15 = (n, from = 0) => Array.from({length: n}, (_, k) => from + 15 * k);

test("rollingMean: as charts.rolling_mean (the same cases as test_rolling_mean)", () => {
  const t = every15(40), v = t.map((_, k) => 30 + (k % 2));            // 30, 31, 30, 31, … every 15 s
  const out = L.rollingMean(t, v, 300);
  assert.ok(out.slice(0, 18).every((x) => x === null));               // less than 4.5 min behind them
  close(out[18], (10 * 30 + 9 * 31) / 19);                             // 4.5 min: 19 readings
  out.slice(19).forEach((x) => close(x, 30.5));                        // 20 readings: ten of each
  const gap = [...t.slice(0, 20), ...t.slice(20).map((x) => x + 600)]; // polling stopped for 10 minutes
  const g = L.rollingMean(gap, v, 300);
  assert.equal(g[25], null);
  assert.notEqual(g[19], null);
  const oneMissed = [...t.slice(0, 10), ...t.slice(11)];               // one 30 s gap is fine
  assert.notEqual(L.rollingMean(oneMissed, v.slice(0, 39), 300).at(-1), null);
});

test("resample: linear between neighbours, held, and null where there's nothing to go on", () => {
  const same = (got, want) => {
    assert.equal(got.length, want.length);
    want.forEach((w, k) => (w === null ? assert.equal(got[k], null, `[${k}]`) : close(got[k], w)));
  };
  const from = [0, 900, 1800, 9000], values = [10, 13, 16, 0];
  same(L.resample([-5, 0, 300, 900, 1500, 1800, 1801], from.slice(0, 3), values.slice(0, 3)),
       [null, 10, 11, 13, 15, 16, null]);                              // nothing after the last row
  same(L.resample([1000, 5000], from, values, "linear", 3600), [13 + 1 / 3, null]);   // 5000: in a 2 h gap
  same(L.resample([-1, 0, 199, 899, 2000, 2100], from.slice(0, 3), values.slice(0, 3), "hold", 200),
       [null, 10, 10, null, 16, null]);                                // held for at most 200 s
});

test("append: newer rows only; a missing label filled; a new one refused unless the table is empty", () => {
  const iso = (s) => new Date(s * 1000).toISOString();
  const table = L.emptyTable();
  assert.ok(L.append(table, [iso(0), iso(15)], {a: [1, 2], b: [5, 5]}));
  assert.deepEqual(table, {t: [0, 15], cols: {a: [1, 2], b: [5, 5]}});
  assert.ok(L.append(table, [iso(15), iso(30)], {a: [2, 3]}, L.hold));   // 15 again: already there; b held
  assert.deepEqual(table, {t: [0, 15, 30], cols: {a: [1, 2, 3], b: [5, 5, 5]}});
  assert.ok(L.append(table, [iso(45)], {b: [7]}, L.zero));
  assert.deepEqual(table.cols, {a: [1, 2, 3, 0], b: [5, 5, 5, 7]});
  assert.equal(L.append(table, [iso(60)], {a: [1], c: [1]}), false);     // c's earlier readings were never sent
  assert.deepEqual(table.t, [0, 15, 30, 45]);                            // unchanged
  assert.ok(L.append(table, [], {a: []}));                               // nothing new
  assert.equal(table.t.length, 4);
  // microseconds: the server's "t" strings parse to milliseconds, which is all the table needs
  const m = L.emptyTable();
  L.append(m, ["2026-10-14T03:01:30.997599Z"], {a: [1]});
  assert.equal(m.t[0], Date.parse("2026-10-14T03:01:30.997Z") / 1000);
});

test("dropBefore: the rows before the cut, from every column", () => {
  const table = {t: [0, 15, 30], cols: {a: [1, 2, 3], b: [4, 5, 6]}};
  assert.equal(L.dropBefore(table, 15), 1);
  assert.deepEqual(table, {t: [15, 30], cols: {a: [2, 3], b: [5, 6]}});
  assert.equal(L.dropBefore(table, -1), 0);
});

test("every chart the dashboard serves data for has a spec, and their builds run on empty data", () => {
  global.uPlot = {paths: {bars: () => null}};                          // the bar chart asks uPlot for its paths
  global.devicePixelRatio = 1;
  for (const [name, spec] of Object.entries(L.CHARTS)) {
    const store = spec.whole ? {raw: {first: null, last: null, house: null, bars: {}}}
                             : {main: L.emptyTable(), aux: {air: L.emptyTable(), soc: L.emptyTable(), sky: L.emptyTable()},
                                meta: {}, side: null};
    const b = spec.build(store, {live_volts: 10});
    assert.equal(b.data.length, b.series.length, name);               // a column per series
  }
});

test("string power: a clear-sky model per string behind it, the strips, and both kWh", () => {
  const t = [0, 3600, 7200];
  const store = {main: {t, cols: {1: [1, 2, 1], 2: [0, 0, 0], 3: [0, 0, 0], 4: [0, 0, 0]}}, meta: {faces: {1: "SE"}},
                 aux: {sky: {t: [0, 3600, 7200], cols: {1: [2, 2, 2], 2: [0, 0, 0], 3: [0, 0, 0], 4: [0, 0, 0]}}},
                 side: {strips: [{label: "Rain", starts: [0], length: 360, colours: ["#ffffff"]}], notes: ["Rain: …"]}};
  const b = L.CHARTS.string_power.build(store);
  assert.equal(b.series.length, 8);                                    // 4 models, then the 4 strings
  assert.equal(b.series[4].label, "String 1 (SE)");
  assert.deepEqual(b.hidden, [1, 2, 3, 4]);
  assert.deepEqual(b.data[0], [2, 2, 2]);                              // string 1's model at each reading
  assert.equal(b.strips.length, 1);
  assert.match(b.note, /^Measured 3\.0 kWh, clear sky 6\.0 kWh/);       // trapezoids; the model's rows × an hour
});

test("the night sky: 30 five-pointed stars, a crescent moon, the same sky for the same seed, its own ids", () => {
  const svg = L.nightSvg(800, 300, 20740, "Sunrise at 07:43: the chart starts then.", "night-string_power");
  const stars = svg.match(/<polygon [^>]*points="([^"]+)"/g);
  assert.equal(stars.length, 30);
  stars.forEach((s) => assert.equal(s.match(/points="([^"]+)"/)[1].split(" ").length, 10));   // 5 points, 5 notches
  assert.match(svg, /mask="url\(#night-string_power-crescent\)"/);
  assert.match(svg, /fill="url\(#night-string_power-sky\)"/);
  assert.ok(!/id="night-(sky|glow|crescent)"/.test(svg));               // prefixed: two charts don't clash
  assert.match(svg, /Sunrise at 07:43/);
  assert.equal(svg, L.nightSvg(800, 300, 20740, "Sunrise at 07:43: the chart starts then.", "night-string_power"));
  assert.notEqual(svg, L.nightSvg(800, 300, 20741, "Sunrise at 07:43: the chart starts then.", "night-string_power"));
  assert.equal(L.nightSvg(360, 140, 1, "x").match(/<polygon /g).length, 30);   // room for them on a phone too
});

test("battery temperatures: 10 °C ticks from 0 or below, and the inverter's axis on the same lines", () => {
  assert.deepEqual(L.tempRange(8.2, 31.5), [0, 40]);                  // the usual: 0–40
  assert.deepEqual(L.tempRange(-3, 12), [-10, 30]);                   // a cold night: still 40 tall
  assert.deepEqual(L.tempRange(5, 47), [0, 50]);                      // a hot day: up to the next 10 above
  assert.deepEqual(L.tempRange(-22, 41), [-30, 50]);
  assert.deepEqual(L.tempRange(null, null), [0, 40]);                 // no data yet
  assert.deepEqual(L.alignedRange(38.4, 58.1, 4), [30, 70]);          // 10 °C steps, as many as the battery's
  assert.deepEqual(L.alignedRange(25, 75, 4), [20, 100]);             // too wide for 4 × 10: 4 × 20
  assert.deepEqual(L.alignedRange(40, 50, 5), [40, 90]);
  assert.deepEqual(L.evenSplits(-10, 30, 4), [-10, 0, 10, 20, 30]);
  assert.deepEqual(L.evenSplits(30, 70, 4), [30, 40, 50, 60, 70]);    // the same heights as the battery's ticks
});

test("time axis labels: 24-hour, the date under the first tick and each new day", () => {
  process.env.TZ = "UTC";                                             // (Node reads it for local time from here on)
  const at = (iso) => Date.parse(iso) / 1000;
  const hours = [at("2026-10-14T22:00:00Z"), at("2026-10-14T23:00:00Z"), at("2026-10-15T00:00:00Z")];
  assert.deepEqual(L.timeTicks(null, hours, 0, 50, 3600), ["22:00\n2026-10-14", "23:00", "00:00\n2026-10-15"]);
  assert.deepEqual(L.timeTicks(null, [at("2026-10-14T14:00:30Z")], 0, 50, 30), ["14:00:30\n2026-10-14"]);
  assert.deepEqual(L.timeTicks(null, [at("2026-10-14T00:00:00Z")], 0, 50, 86400), ["2026-10-14"]);
});

test("power by load now: the axis starts at the biggest bar × 1.3 and only ever grows", () => {
  const close2 = (got, want) => want.forEach((w, k) => close(got[k], w));
  close2(L.growingSpan(null, [0.2, 1.0, 0.5]), [0, 1.3]);
  close2(L.growingSpan([0, 1.3], [0.2, 0.4]), [0, 1.3]);              // smaller now: the axis stays
  close2(L.growingSpan([0, 1.3], [2.0, 0.4]), [0, 2.6]);              // past the end: grows, with room again
  close2(L.growingSpan([0, 2.6], [1.0, -0.1]), [-0.13, 2.6]);         // a negative Other: room on the left too
  global.uPlot = {paths: {bars: () => null}};
  const store = {raw: {at: "2026-10-15T00:00:00Z", house: 1, bars: {A: 1.0, Other: 0.0}}};
  const range = () => L.CHARTS.load_power_now.build(store).scales.y.range();
  close2(range(), [0, 1.3]);
  store.raw = {at: "2026-10-15T00:00:15Z", house: 0.3, bars: {A: 0.3, Other: 0.0}};   // the next fetch
  close2(range(), [0, 1.3]);                                          // the store keeps the span between builds
});

test("strips: each a label row then its bar, with gaps; bars clipped to the plot and ended by the next", () => {
  const {rows, height} = L.stripRows(2, {top: 6, label: 14, bar: 9, gap: 6, below: 4});
  assert.deepEqual(rows, [{labelY: 6, barY: 20, barH: 9}, {labelY: 35, barY: 49, barH: 9}]);
  assert.equal(height, 58);                                            // no gap after the last
  assert.equal(L.stripRows(0).height, 0);
  const px = (t) => t;                                                 // one pixel a second
  assert.deepEqual(L.stripSpans([0, 100, 500], 360, ["#a", null, "#b"], px, 50, 700),
                   [[50, 100, "#a"], [100, 460, null], [500, 700, "#b"]]);   // clipped; the next start; the right edge
  assert.deepEqual(L.stripSpans([0], 360, ["#a"], px, 400, 700), []);  // wholly left of the plot
});
