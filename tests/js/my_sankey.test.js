// The daily page's Sankey layout (src/sigen/static/my_sankey.js): pure arithmetic, so all of it is testable here.
// Run: node --test tests/js/my_sankey.test.js (tests/test_js.py runs it from pytest).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const S = require(path.join(__dirname, "..", "..", "src", "sigen", "static", "my_sankey.js"));

// 2026-10-13 up to 18:25, as the page had it
const DAY = {
  sources: [{name: "Solar", kwh: 40.45, colour: "#e8c547"}, {name: "Battery", kwh: 0.46, colour: "#45b8ac"},
            {name: "Grid", kwh: 0.11, colour: "#6f7bd9"}],
  sinks: [{name: "Battery", kwh: 2.06, colour: "#45b8ac"}, {name: "Load", kwh: 10.46, colour: "#9b5de5"},
          {name: "Grid", kwh: 28.51, colour: "#6f7bd9"}],
  flows: [{from: 0, to: 1, kwh: 9.907}, {from: 0, to: 0, kwh: 2.026}, {from: 0, to: 2, kwh: 28.339},
          {from: 1, to: 1, kwh: 0.378}, {from: 1, to: 2, kwh: 0.06}, {from: 2, to: 1, kwh: 0.082},
          {from: 2, to: 0, kwh: 0.007}],
};
const W = 800, H = 500, o = S.DEFAULTS;
const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) <= eps, `${a} is not ${b} (±${eps})`);
const node = (l, side, name) => l.nodes.find((n) => n.side === side && n.name === name);
const column = (l, side) => l.nodes.filter((n) => n.side === side);

const SOLAR_BANDS = 9.907 + 2.026 + 28.339;                          // 40.272 of solar's 40.45 kWh counter

test("both ends of every band are its kWh on one scale, and every bar spans exactly its bands", () => {
  const l = S.layout(DAY, W, H);
  // The sources bind: solar's bar plus two minimum slots and two gaps fill the height.
  close(l.scale, (H - 2 * o.gap - 2 * o.minNode) / SOLAR_BANDS);
  const t = (b) => Math.max(b.kwh * l.scale, o.minBand);
  for (const b of l.bands) {
    close(b.y0b - b.y0, t(b));
    close(b.y1b - b.y1, t(b));
  }
  for (const n of l.nodes) {
    const mine = l.bands.filter((b) => (n.side === "left" ? b.from : b.to) === n.index);
    close(n.barH, mine.map(t).reduce((a, b) => a + b, 0));
    close(Math.min(...mine.map((b) => (n.side === "left" ? b.y0 : b.y1))), n.barY);            // starts at the top band
    close(Math.max(...mine.map((b) => (n.side === "left" ? b.y0b : b.y1b))), n.barY + n.barH); // ends at the lowest
  }
});

test("a label keeps its node's counter; the bar draws only its bands", () => {
  const solar = node(S.layout(DAY, W, H), "left", "Solar");
  assert.equal(solar.kwh, 40.45);
  close(solar.banded, SOLAR_BANDS);
});

test("the scale is the largest at which both columns of cards fit", () => {
  const l = S.layout(DAY, W, H);
  const solar = column(l, "left");
  close(solar[0].y, 0);
  close(solar[2].y + solar[2].h, H);                                  // the binding column fills the canvas
  const sinks = column(l, "right");
  assert.ok(sinks[2].y + sinks[2].h < H);                             // the other has room to spare: centred
  close(sinks[0].y, H - (sinks[2].y + sinks[2].h));
});

test("a card is as tall as its bar or as its label needs, with the bar in its middle", () => {
  const l = S.layout(DAY, W, H);
  for (const n of l.nodes) {
    close(n.h, Math.max(n.barH, o.minNode));
    close(n.barY - n.y, (n.h - n.barH) / 2);
  }
  const grid = node(l, "left", "Grid");
  close(grid.h, o.minNode);                                           // 0.09 kWh of bands: two hairlines in a slot
  close(grid.barH, 2 * o.minBand);
  close(node(l, "left", "Solar").h, node(l, "left", "Solar").barH);   // a big one: the card is the bar
});

test("cards stand in columns, in order, gap apart, at the canvas edges", () => {
  const l = S.layout(DAY, W, H);
  for (const side of ["left", "right"]) {
    const nodes = column(l, side);
    assert.equal(nodes.length, 3);
    for (let i = 1; i < nodes.length; i++) close(nodes[i].y, nodes[i - 1].y + nodes[i - 1].h + o.gap);
  }
  assert.deepEqual(column(l, "left").map((n) => n.x), [0, 0, 0]);
  const load = node(l, "right", "Load");
  close(load.x + load.w, W);
  close(load.w, Math.min(Math.max(W * o.nodeWidth, o.minNodeWidth), o.maxNodeWidth));
});

test("a bar's bands stack from its top, ordered by the node at the other end", () => {
  const l = S.layout(DAY, W, H);
  const solar = node(l, "left", "Solar");
  const fromSolar = l.bands.filter((b) => b.from === 0).sort((a, b) => a.y0 - b.y0);
  assert.deepEqual(fromSolar.map((b) => b.to), [0, 1, 2]);           // battery, load, grid: as the sinks stand
  close(fromSolar[0].y0, solar.barY);
  for (let i = 1; i < fromSolar.length; i++) close(fromSolar[i].y0, fromSolar[i - 1].y0b);
  close(fromSolar[2].y0b, solar.barY + solar.barH);                  // no gap at the bottom: the bar ends there
  const load = node(l, "right", "Load");
  const intoLoad = l.bands.filter((b) => b.to === 1).sort((a, b) => a.y1 - b.y1);
  assert.deepEqual(intoLoad.map((b) => b.from), [0, 1, 2]);
  close(intoLoad[0].y1, load.barY);
  for (const b of l.bands) {
    close(b.x0, solar.w);                                             // from the sources' inner edge…
    close(b.x1, load.x);                                              // …to the sinks'
  }
});

test("a tiny flow still shows as a hairline", () => {
  const b = S.layout(DAY, W, H).bands.find((b) => b.from === 2 && b.to === 0);     // grid → battery, 0.007 kWh
  close(b.y0b - b.y0, o.minBand);
  close(b.y1b - b.y1, o.minBand);
  assert.equal(b.colour0, "#6f7bd9");
  assert.equal(b.colour1, "#45b8ac");
});

test("fitScale: as big as fits, smaller when minimum cards take room, 0 for nothing", () => {
  const opts = {minNode: 10, gap: 0};
  close(S.fitScale([[{kwh: 1}, {kwh: 3}]], 400, opts), 100);         // no minimums in play: 400 px for 4 kWh
  close(S.fitScale([[{kwh: 100}, {kwh: 1}, {kwh: 1}]], 300, {minNode: 40, gap: 0}), 2.2, 1e-9);   // 100 s + 80 = 300
  assert.equal(S.fitScale([[], []], 300, opts), 0);
});

test("nodes without a band, and flows of 0, are left out; a node without a counter is labelled with its bands", () => {
  const quiet = {
    sources: [{name: "Solar", kwh: 20, colour: "#e8c547"}, {name: "Battery", kwh: 0, colour: "#45b8ac"},
              {name: "Grid", kwh: 3, colour: "#6f7bd9"}],                // a counter, but no band: left out
    sinks: [{name: "Battery", kwh: 0, colour: "#45b8ac"}, {name: "Load", kwh: 0, colour: "#9b5de5"},
            {name: "Grid", kwh: 12, colour: "#6f7bd9"}],
    flows: [{from: 0, to: 1, kwh: 8}, {from: 0, to: 2, kwh: 12}, {from: 1, to: 1, kwh: 0}],
  };
  const l = S.layout(quiet, W, H);
  assert.deepEqual(l.nodes.map((n) => `${n.side} ${n.name}`), ["left Solar", "right Load", "right Grid"]);
  assert.equal(node(l, "right", "Load").kwh, 8);                     // no counter: its bands' 8 kWh
  assert.equal(l.bands.length, 2);
});

test("every node's kWh gets one size: the largest at which the widest still fits its card", () => {
  const measure = (text, size) => text.length * size * 0.6;          // a stand-in for the canvas's measureText
  const nodes = [{kwh: 2.06, w: 120}, {kwh: 28.54, w: 120}];         // "2.06 kWh" 8 characters, "28.54 kWh" 9
  assert.equal(S.valueSize(nodes, measure), 18);                     // 9 × 18 × 0.6 = 97.2 ≤ 120 - 12
  const narrow = [{kwh: 2.06, w: 84}, {kwh: 28.54, w: 84}];          // 72 px for text: the widest decides
  assert.equal(S.valueSize(narrow, measure), 13);                    // 9 × 13 × 0.6 = 70.2; at 14 it's 75.6
  assert.equal(S.valueSize(narrow, measure, 6), 12);                 // 6 px less beside the bar
  assert.equal(S.valueSize([{kwh: 123456.78, w: 20}], measure), 9);  // nothing fits: the smallest
});

test("nothing at all lays out as nothing, without failing", () => {
  const l = S.layout({sources: [], sinks: [], flows: []}, W, H);
  assert.deepEqual([l.nodes, l.bands, l.scale], [[], [], 0]);
});
