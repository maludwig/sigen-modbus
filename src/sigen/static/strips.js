/* strips.js: rows of coloured bars under a uPlot chart, on its time axis -- the radar / satellite / PV strips under the
   dashboard's string-power chart (static/live_charts.js) and the per-day strips under the all-time page's solar line
   (templates/all_time.html).

   Each strip (from the server: charts.Strip, with its starts turned into seconds) is a row: its label, then a bar from
   each start, `length` seconds long or up to the next start, in its colour (null: no data, so the hatching shows).
   They're painted on the chart's own canvas, in the room the x axis leaves above its time labels (its `gap`), with the
   chart's own x mapping (valToPos): they line up with the lines at any zoom and width.

   Use: lay = Strips.layout(strips, xaxis, hooks); new uPlot({..., height: h + lay.extra, axes: [lay.xaxis, ...],
   hooks: lay.hooks}, ...); then lay.cursor.line = Strips.cursorLine(plot). In a browser this file sets the global
   Strips; in Node, require() returns the same functions (stripRows and stripSpans are tested in tests/js). */
(function (root) {
  "use strict";

  const STRIP = {top: 6, label: 14, bar: 9, gap: 6, below: 4};   // CSS px: above the first, each row's parts, under

  // The rows' places in CSS px from the plot's bottom edge, and the band's height.
  function stripRows(n, s = STRIP) {
    const step = s.label + s.bar + s.gap, rows = [];
    for (let i = 0; i < n; i++) {
      const y = s.top + i * step;
      rows.push({labelY: y, barY: y + s.label, barH: s.bar});
    }
    return {rows, height: n ? s.top + n * step - s.gap : 0};
  }

  // A strip's bars as [left, right, colour] in pixels: toPx(time) mapped and clipped to [left, right]; bars wholly
  // outside are left out.
  function stripSpans(starts, length, colours, toPx, left, right) {
    const out = [];
    for (let i = 0; i < starts.length; i++) {
      const stop = Math.min(starts[i] + length, i + 1 < starts.length ? starts[i + 1] : Infinity);
      const a = Math.max(toPx(starts[i]), left), b = Math.min(toPx(stop), right);
      if (b > a) out.push([a, b, colours[i]]);
    }
    return out;
  }

  let hatching = null;                                    // the "no data" pattern, made once per pixel ratio
  function hatch(ctx, pr) {
    if (hatching && hatching.pr === pr) return hatching.pattern;
    const tile = root.document.createElement("canvas"), n = Math.round(6 * pr), g = tile.getContext("2d");
    tile.width = tile.height = n;
    g.fillStyle = "#fff";
    g.fillRect(0, 0, n, n);
    g.strokeStyle = "#c8c8c8";
    g.lineWidth = pr;
    g.beginPath();                                        // "////", seamless across tiles
    for (const k of [-n, 0, n]) {
      g.moveTo(k, n);
      g.lineTo(k + n, 0);
    }
    g.stroke();
    hatching = {pr, pattern: ctx.createPattern(tile, "repeat")};
    return hatching.pattern;
  }

  function paintStrips(u, strips, band) {
    const pr = root.devicePixelRatio || 1, ctx = u.ctx, {left, top, width, height} = u.bbox;
    const below = top + height, toPx = (t) => u.valToPos(t, "x", true);
    ctx.save();
    ctx.font = `${11 * pr}px system-ui, sans-serif`;
    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    strips.forEach((s, i) => {
      const row = band.rows[i], y = Math.round(below + row.barY * pr), h = Math.round(row.barH * pr);
      ctx.fillStyle = "#444";
      ctx.fillText(s.label, left, below + (row.labelY + STRIP.label / 2) * pr);
      ctx.fillStyle = hatch(ctx, pr);
      ctx.fillRect(left, y, width, h);
      for (const [a, b, colour] of stripSpans(s.starts, s.length, s.colours, toPx, left, left + width)) {
        if (colour === null) continue;
        const x = Math.round(a);
        ctx.fillStyle = colour;
        ctx.fillRect(x, y, Math.max(Math.round(b) - x, 1), h);
      }
      ctx.strokeStyle = "#ddd";                           // an outline, so a clear (white) strip still shows
      ctx.lineWidth = pr;
      ctx.strokeRect(left + pr / 2, y + pr / 2, width - pr, h - pr);
    });
    ctx.restore();
  }

  function moveStripCursor(u, line, band) {               // uPlot's cursor line stops at the plot: carry it on
    const x = u.cursor.left;
    if (x == null || x < 0) {
      line.style.display = "none";
      return;
    }
    const pr = root.devicePixelRatio || 1;
    Object.assign(line.style, {display: "", left: `${u.bbox.left / pr + x}px`,
                               top: `${(u.bbox.top + u.bbox.height) / pr}px`, height: `${band.height}px`});
  }

  // Room for `strips` under a chart: {xaxis (with the gap the band needs), hooks (painting them and carrying the
  // cursor down), extra (CSS px to add to the chart's height), cursor (set cursor.line = cursorLine(plot) once the
  // chart exists)}. No strips: everything as given.
  function layout(strips, xaxis, hooks) {
    if (!strips.length) return {xaxis, hooks, extra: 0, cursor: {line: null}};
    const band = stripRows(strips.length), cursor = {line: null}, extra = band.height + STRIP.below;
    return {xaxis: {...xaxis, gap: extra, size: extra + 40, ticks: {show: false}}, extra, cursor,
            hooks: {...hooks, draw: [...(hooks.draw || []), (u) => paintStrips(u, strips, band)],
                    setCursor: [(u) => cursor.line && moveStripCursor(u, cursor.line, band)]}};
  }

  function cursorLine(plot) {                             // the cursor's line, carried on down through the strips
    const line = root.document.createElement("div");
    line.style.cssText = "position:absolute;width:0;border-left:1px dashed #607d8b;pointer-events:none;display:none";
    plot.over.parentNode.appendChild(line);
    return line;
  }

  const api = {STRIP, stripRows, stripSpans, paintStrips, moveStripCursor, layout, cursorLine};
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.Strips = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
