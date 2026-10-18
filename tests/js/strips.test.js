// The strips under a uPlot chart (src/sigen/static/strips.js): shared by the string-power chart and the all-time page.
// Run: node --test tests/js/strips.test.js (tests/test_js.py runs it from pytest).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const S = require(path.join(__dirname, "..", "..", "src", "sigen", "static", "strips.js"));
const L = require(path.join(__dirname, "..", "..", "src", "sigen", "static", "live_charts.js"));

test("live_charts uses strips.js's functions, not copies of them", () => {
  assert.equal(L.stripRows, S.stripRows);
  assert.equal(L.stripSpans, S.stripSpans);
});

test("layout: no strips, nothing changes; with strips, room under the plot and the hooks that paint them", () => {
  const xaxis = {space: 80}, hooks = {draw: ["mine"]};
  const none = S.layout([], xaxis, hooks);
  assert.equal(none.xaxis, xaxis);
  assert.equal(none.hooks, hooks);
  assert.equal(none.extra, 0);
  const two = [{label: "a", starts: [0], length: 60, colours: ["#000"]}, {label: "b", starts: [0], length: 60, colours: [null]}];
  const lay = S.layout(two, xaxis, hooks);
  const band = S.stripRows(2);
  assert.equal(lay.extra, band.height + S.STRIP.below);
  assert.deepEqual(lay.xaxis, {space: 80, gap: lay.extra, size: lay.extra + 40, ticks: {show: false}});
  assert.equal(lay.hooks.draw.length, 2);                              // the chart's own draw hook kept, ours added
  assert.equal(lay.hooks.draw[0], "mine");
  assert.equal(lay.hooks.setCursor.length, 1);
  assert.equal(lay.cursor.line, null);                                  // set once the chart exists
  assert.deepEqual(xaxis, {space: 80});                                 // the arguments are left alone
  assert.deepEqual(hooks, {draw: ["mine"]});
});
