/* The dashboard's charts (templates/dashboard.html, /).

   Each chart's numbers stay in the page. The first fetch of its /data/<name>.json brings the whole window; after that,
   every few seconds, ?since=<the newest "t", exactly as it came> brings only the readings after it. (The string, not
   a Date: a Date keeps milliseconds, the server microseconds, and the newest reading would come again.) Means, the
   energy since the window started, and readings on other clocks (Open-Meteo's air, the plant's SOC) are worked out
   here from what the page holds, so a top-up is one reading, not the day. uPlot draws them; times show in this
   device's timezone.

   The arithmetic (rollingMean, resample, append, dropBefore) is plain functions, tested in Node
   (tests/js/live_charts.test.js). In a browser this file sets the global LiveCharts; in Node, require() returns the
   same object. */
(function (root) {
  "use strict";

  // ---------- arithmetic ----------

  // A trailing mean over each reading's last `window` seconds (readings in (t − window, t]): null (a gap in the line)
  // unless those readings span at least coverage × window with no gap over maxGap between them, i.e. at the start and
  // after a stop in the polling, rather than a mean of a few readings.
  function rollingMean(ts, vs, window, coverage = 0.9, maxGap = 120) {
    const out = [];
    let total = 0, lo = 0, lastGap = null;                // lastGap: the index after the latest gap over maxGap
    for (let hi = 0; hi < ts.length; hi++) {
      const t = ts[hi];
      total += vs[hi];
      if (hi && t - ts[hi - 1] > maxGap) lastGap = hi;
      while (ts[lo] <= t - window) total -= vs[lo++];
      const full = t - ts[lo] >= window * coverage && (lastGap === null || lastGap <= lo);
      out.push(full ? total / (hi - lo + 1) : null);
    }
    return out;
  }

  // `values` (at the ascending times `from`) at each of the ascending times `ts`. "linear": between the neighbours
  // either side, null before the first, after the last, or between two more than maxGap apart. "hold": the last one at
  // or before, null if none or it's more than maxGap old.
  function resample(ts, from, values, how = "linear", maxGap = Infinity) {
    const out = [];
    let j = 0;                                            // from[j - 1] <= t < from[j]
    for (const t of ts) {
      while (j < from.length && from[j] <= t) j++;
      const a = j - 1;
      if (a < 0) out.push(null);
      else if (how === "hold") out.push(t - from[a] <= maxGap ? values[a] : null);
      else if (from[a] === t) out.push(values[a]);
      else if (j === from.length || from[j] - from[a] > maxGap) out.push(null);
      else out.push(values[a] + (t - from[a]) / (from[j] - from[a]) * (values[j] - values[a]));
    }
    return out;
  }

  // A table: {t: [unix seconds], cols: {label: [values]}}, the columns as long as t.
  const emptyTable = () => ({t: [], cols: {}});

  // The value a column takes in rows that left it out: its last (a counter that didn't move), or 0 (a power that
  // stayed at 0).
  const hold = (column) => (column.length ? column[column.length - 1] : null);
  const zero = () => 0;

  // Appends the rows {t: [ISO 8601], cols: {label: [values]}} to `table`, those newer than its last row only (some
  // endpoints send their last few again). A label the rows leave out gets fill(its column) in each new row. A label
  // the table doesn't have yet: false, and the table is unchanged, unless the table is empty (the caller fetches the
  // whole window again: the label's earlier values were never sent).
  function append(table, t, cols, fill = hold) {
    const ts = t.map((iso) => Date.parse(iso) / 1000);
    const newest = table.t.length ? table.t[table.t.length - 1] : -Infinity;
    let k0 = 0;
    while (k0 < ts.length && ts[k0] <= newest) k0++;
    const fresh = Object.keys(cols).filter((label) => !(label in table.cols));
    if (fresh.length && table.t.length) return false;
    for (const label of fresh) table.cols[label] = [];
    const n = ts.length - k0;
    for (const [label, column] of Object.entries(table.cols)) {
      const added = label in cols ? cols[label].slice(k0) : new Array(n).fill(fill(column));
      table.cols[label] = column.concat(added);             // (concat: push(...a) overflows the stack for long a)
    }
    table.t = table.t.concat(ts.slice(k0));
    return true;
  }

  // Drops the rows before `cut` (unix seconds); returns how many.
  function dropBefore(table, cut) {
    let k = 0;
    while (k < table.t.length && table.t[k] < cut) k++;
    if (k) {
      table.t = table.t.slice(k);
      for (const label of Object.keys(table.cols)) table.cols[label] = table.cols[label].slice(k);
    }
    return k;
  }

  // ---------- drawing ----------

  const TAB10 = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22",
                 "#17becf"];                              // matplotlib's C0–C9, as the old PNG charts had
  const GREY = "#808080", BLUE = TAB10[0], ORANGE = TAB10[1], GREEN = TAB10[2], RED = TAB10[3], PURPLE = TAB10[4];
  const GOLDENROD = "#daa520";
  const MEAN = 5 * 60;                                    // seconds: the 5-minute means
  const AXIS = 60;                                        // px: the battery charts' axes, so their time axes line up
  const BAR_ROW = 34;                                     // px per bar of the energy-by-load chart
  const labelWidth = (labels) => {                        // px: the widest label in uPlot's axis font
    if (!root.document) return 100;
    const ctx = root.document.createElement("canvas").getContext("2d");
    ctx.font = "12px system-ui, -apple-system, sans-serif";
    return Math.ceil(Math.max(0, ...labels.map((l) => ctx.measureText(l).width)));
  };

  const faint = (hex, alpha = 0.2) => {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  };
  const pad2 = (n) => String(n).padStart(2, "0");
  // Times in this device's zone, 24-hour: ymd "2026-10-14", hm "18:05", when "2026-10-14 18:05:30".
  const ymd = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
  const hm = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const when = (seconds) => {
    const d = new Date(seconds * 1000);
    return `${ymd(d)} ${hm(d)}:${pad2(d.getSeconds())}`;
  };

  // The time axis's tick labels (uPlot's axis `values`): 24-hour times, with seconds once the ticks are under a minute
  // apart, and the date under the first tick and wherever a new day starts; just the date for ticks a day or more
  // apart.
  function timeTicks(u, splits, axisIdx, space, incr) {
    let day = null;
    return splits.map((s) => {
      const d = new Date(s * 1000), date = ymd(d);
      if (incr >= 86400) return date;
      const time = incr < 60 ? `${hm(d)}:${pad2(d.getSeconds())}` : hm(d);
      const label = date !== day ? `${time}\n${date}` : time;
      day = date;
      return label;
    });
  }
  const shown = (digits, unit) => (u, v) => (v == null ? "-" : `${v.toFixed(digits)} ${unit}`);
  const padded = (lo, hi) => {                            // 5 % either side, as matplotlib's margins
    const m = (hi - lo) * 0.05 || 1;
    return [lo - m, hi + m];
  };
  const withZero = (u, min, max) => (min == null ? [0, 1] : padded(Math.min(min, 0), Math.max(max, 0)));
  const loadColours = (labels) =>                         // C0, C1, … in order, Other grey
    labels.map((label, i) => (label.startsWith("Other") ? GREY : TAB10[i % TAB10.length]));

  // A thin black line at 0 on `scale` (matplotlib's axhline(0)).
  const zeroLine = (scale) => (u) => {
    const y = Math.round(u.valToPos(0, scale, true)), {left, top, width, height} = u.bbox;
    if (y < top || y > top + height) return;
    u.ctx.save();
    u.ctx.strokeStyle = "#000";
    u.ctx.lineWidth = 0.6 * (root.devicePixelRatio || 1);
    u.ctx.beginPath();
    u.ctx.moveTo(left, y);
    u.ctx.lineTo(left + width, y);
    u.ctx.stroke();
    u.ctx.restore();
  };

  // ---------- strips: rows of coloured bars under a chart, on its time axis ----------
  // Each strip (from the server: charts.Strip) is a row: its label, then a bar from each start, `length` seconds long
  // or up to the next start, in its colour (null: no data, so the hatching shows). They're painted on the chart's own
  // canvas, in the room the x axis leaves above its time labels (its `gap`), with the chart's own x mapping
  // (valToPos): they line up with the lines at any zoom and width.

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

  // ---------- the night: what the string charts show for "today" before sunrise ----------

  function mulberry32(seed) {                             // a small seeded random generator: the same sky all night
    return () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // A five-pointed star's points around (x, y): outer radius r, inner 0.45 r, a point straight up.
  function starPoints(x, y, r) {
    const out = [];
    for (let k = 0; k < 10; k++) {
      const a = -Math.PI / 2 + k * Math.PI / 5, d = k % 2 ? r * 0.45 : r;
      out.push(`${(x + d * Math.cos(a)).toFixed(1)},${(y + d * Math.sin(a)).toFixed(1)}`);
    }
    return out.join(" ");
  }

  // An SVG night sky, width × height px: a dark blue gradient, a cartoon crescent moon (sleepy eye, rosy cheek, a
  // soft glow) and `stars` five-pointed stars, some twinkling, kept off the moon. `label` along the bottom. `id`
  // prefixes its gradients' and mask's ids: two of these on one page mustn't share them.
  function nightSvg(width, height, seed, label, id = "night", stars = 30) {
    const rand = mulberry32(seed), r = Math.min(height * 0.2, 64);
    const mx = width * 0.78, my = height * 0.34;          // the moon's centre
    const pts = [];
    for (let tries = 0; pts.length < stars && tries < 5000; tries++) {
      const x = 10 + rand() * (width - 20), y = 10 + rand() * (height - 40), size = 3 + rand() * 4.5;
      if (Math.hypot(x - mx, y - my) < r * 1.7) continue;              // not on the moon or its glow
      if (pts.some((p) => Math.hypot(p.x - x, p.y - y) < 18)) continue;  // nor on another star
      pts.push({x, y, size, twinkle: rand() < 0.4, delay: rand() * 3, period: 2.5 + rand() * 2});
    }
    const starsSvg = pts.map((p) => `<polygon class="${p.twinkle ? "tw" : ""}" points="${starPoints(p.x, p.y, p.size)}"` +
      (p.twinkle ? ` style="animation-duration:${p.period.toFixed(1)}s;animation-delay:${p.delay.toFixed(1)}s"` : "") +
      ` fill="${p.size > 5.5 ? "#fff6cf" : "#e8eeff"}"/>`).join("");
    const f = (v) => v.toFixed(1);
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" ` +
      `role="img" aria-label="Night: ${label}">` +
      `<style>.tw{animation:twinkle 3s ease-in-out infinite}@keyframes twinkle{50%{opacity:.25}}</style>` +
      `<defs><linearGradient id="${id}-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#060d26"/>` +
      `<stop offset="1" stop-color="#14245a"/></linearGradient>` +
      `<radialGradient id="${id}-glow"><stop offset="0.55" stop-color="#fff3b0" stop-opacity="0.35"/>` +
      `<stop offset="1" stop-color="#fff3b0" stop-opacity="0"/></radialGradient>` +
      `<mask id="${id}-crescent"><rect width="${width}" height="${height}" fill="#fff"/>` +
      `<circle cx="${f(mx + r * 0.5)}" cy="${f(my - r * 0.25)}" r="${f(r * 0.88)}" fill="#000"/></mask></defs>` +
      `<rect width="${width}" height="${height}" fill="url(#${id}-sky)" rx="6"/>` + starsSvg +
      `<circle cx="${f(mx)}" cy="${f(my)}" r="${f(r * 1.6)}" fill="url(#${id}-glow)"/>` +
      `<circle cx="${f(mx)}" cy="${f(my)}" r="${f(r)}" fill="#ffe680" mask="url(#${id}-crescent)"/>` +
      `<path d="M ${f(mx - r * 0.72)} ${f(my - r * 0.1)} q ${f(r * 0.13)} ${f(r * 0.13)} ${f(r * 0.26)} 0" ` +
      `stroke="#5a4a1a" stroke-width="${f(Math.max(r * 0.06, 1.5))}" fill="none" stroke-linecap="round"/>` +
      `<circle cx="${f(mx - r * 0.66)}" cy="${f(my + r * 0.22)}" r="${f(r * 0.1)}" fill="#ff9e9e" opacity="0.7"/>` +
      `<text x="14" y="${height - 14}" fill="#c9d3f0" font-family="system-ui, sans-serif" font-size="13">` +
      `${label}</text></svg>`;
  }

  // ---------- the battery temperatures' two axes, their ticks on the same lines ----------

  // The battery axis: from N = min(0, floor(min / 10) × 10) up in 10 °C steps, at least 40 °C tall and up to the
  // next 10 above max: usually 0–40, −10–30 on a cold night, 0–50 on a hot day.
  function tempRange(min, max) {
    if (min == null || max == null) return [0, 40];
    const lo = Math.min(0, Math.floor(min / 10) * 10);
    return [lo, Math.max(lo + 40, Math.ceil(max / 10) * 10)];
  }
  const intervals = (lo, hi) => Math.max(1, Math.round((hi - lo) / 10));   // the battery axis's 10 °C steps

  // A second axis with the same number of intervals `k`: the first step of `steps` that fits [min, max] in k of them,
  // starting from a multiple of it at or below min. So each of its ticks lies on one of the first axis's.
  function alignedRange(min, max, k, steps = [10, 20, 25, 50, 100]) {
    if (min == null || max == null) return [0, k * steps[0]];
    for (const step of steps) {
      const lo = Math.floor(min / step) * step;
      if (lo + k * step >= max) return [lo, lo + k * step];
    }
    const step = Math.ceil((max - min) / k), lo = Math.floor(min / step) * step;
    return [lo, lo + k * step];
  }

  // k + 1 ticks, evenly from lo to hi.
  const evenSplits = (lo, hi, k) => Array.from({length: k + 1}, (_, i) => lo + (hi - lo) * i / k);

  // The lowest and highest value of series `idxs` within the x range shown ([null, null] if none).
  function visibleExtent(u, idxs) {
    const x = u.data[0], from = u.scales.x.min, to = u.scales.x.max;
    let lo = Infinity, hi = -Infinity;
    for (let k = 0; k < x.length; k++) {
      if (x[k] < from || x[k] > to) continue;
      for (const i of idxs) {
        const v = u.data[i][k];
        if (v == null) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    return lo <= hi ? [lo, hi] : [null, null];
  }

  // ---------- a bar per load, turned on its side (energy by load, power by load now) ----------

  const barsHeight = (bars) => Math.max(Object.keys(bars).length, 1) * BAR_ROW + 60;

  // An axis span that only ever grows, so bars that change every few seconds don't all rescale whenever the biggest
  // one moves: at first the values' span × ROOM; after that, an end moves out (to that value × ROOM again, for room
  // to wiggle) only when a value passes it.
  const ROOM = 1.3;
  function growingSpan(span, values) {
    const hi = Math.max(...values, 0.01), lo = Math.min(...values, 0);
    const [bottom, top] = span || [0, 0];
    return [lo < bottom ? lo * ROOM : bottom, hi > top ? hi * ROOM : top];
  }

  // {load: value}, `total` the house's (for each bar's share): horizontal bars in the loads' colours, the loads' names
  // down the left, each bar's value and share written past its end (or inside it, in white, where there's no room). A
  // bar can be negative (Other, when the loads were read a moment apart from the total): it runs left of 0.
  function loadBars(bars, total, unit, axisLabel, note, span = null) {    // span: [bottom, top], else fitted
    const labels = Object.keys(bars), values = labels.map((l) => bars[l]), n = labels.length;
    const colours = loadColours(labels), xs = labels.map((_, i) => i);
    const [bottom, top] = span || [Math.min(...values, 0) * 1.05, Math.max(...values, 0.01) * 1.05];
    const text = (v) => `${v.toFixed(2)} ${unit}` + (total ? ` · ${(100 * v / total).toFixed(1)}%` : "");
    const annotate = (u) => {
      const r = root.devicePixelRatio || 1, gap = 5 * r, right = u.bbox.left + u.bbox.width;
      u.ctx.save();
      u.ctx.textBaseline = "middle";
      u.ctx.font = `${12 * r}px system-ui, sans-serif`;
      values.forEach((v, i) => {
        const y = u.valToPos(i, "x", true), s = text(v);
        const end = Math.max(u.valToPos(v, "y", true), u.valToPos(0, "y", true));   // a negative bar: from 0
        if (end + gap + u.ctx.measureText(s).width <= right) {
          u.ctx.fillStyle = "#222";
          u.ctx.textAlign = "left";
          u.ctx.fillText(s, end + gap, y);
        } else {                                          // no room past it: inside the bar's end, in white
          u.ctx.fillStyle = "#fff";
          u.ctx.textAlign = "right";
          u.ctx.fillText(s, end - gap, y);
        }
      });
      u.ctx.restore();
    };
    return {
      x: xs,
      data: values.map((v, i) => xs.map((j) => (j === i ? v : null))),
      series: labels.map((l, i) => ({label: l, fill: colours[i], stroke: colours[i], width: 0, fillTo: 0,
                                     paths: root.uPlot.paths.bars({size: [0.7, 40]}), points: {show: false},
                                     value: shown(2, unit)})),
      // x is the loads, turned to run down the left (ori 1; dir −1: the first at the top); y, the values, along the
      // bottom
      scales: {x: {time: false, ori: 1, dir: -1, range: () => [-0.5, Math.max(n, 1) - 0.5]},
               y: {ori: 0, range: () => [bottom, top]}},
      xaxis: {side: 3, splits: () => xs, values: (u, splits) => splits.map((i) => labels[i] ?? ""),
              grid: {show: false}, size: labelWidth(labels) + 18},
      axes: [{side: 2, label: axisLabel}],
      hooks: {draw: [annotate]},
      legend: {show: false},
      cursor: {drag: {x: false, y: false}},
      note,
    };
  }

  // Series of every reading, faint, behind each one's 5-minute mean: [faint…, means…]; the faint ones are left out of
  // the legend, and hiding a mean hides its readings too.
  function meansOverReadings(t, labels, columns, colours, unit, digits, width = 1.3, names = labels) {
    const n = labels.length;
    return {
      data: [...columns, ...columns.map((c) => rollingMean(t, c, MEAN))],
      series: [...labels.map((l, i) => ({label: `${l}, every reading`, stroke: faint(colours[i]), width: 0.6,
                                         value: shown(digits, unit)})),
               ...names.map((l, i) => ({label: l, stroke: colours[i], width, value: shown(digits, unit)}))],
      hidden: labels.map((_, i) => i + 1),                // uPlot's series indexes (0 is time)
      pairs: Object.fromEntries(labels.map((_, i) => [n + i + 1, i + 1])),
    };
  }

  // ---------- the charts ----------
  // Each: aspect (height ÷ width), tables(response) → {main: {t, cols}, aux: {name: {t, cols}}}, fill (for main's
  // missing labels), build(store, env) → {data, series, axes, scales, hooks, hidden, pairs, note}; or whole: true
  // (the whole window every time, no ?since=) with build({raw}). xrange(data, min, max, env): the time span shown when
  // not zoomed in.

  const STRINGS = ["1", "2", "3", "4"];

  const CHARTS = {
    string_voltage: {
      aspect: 4.2 / 11,
      night: true,                                        // "today" before sunrise: a night sky (nightSvg)
      tables: (d) => ({main: {t: d.t, cols: d.volts}}),
      build: (s) => ({
        data: STRINGS.map((n) => s.main.cols[n] || []),
        series: STRINGS.map((n, i) => ({label: `String ${n}`, stroke: TAB10[i], width: 1, value: shown(1, "V")})),
        axes: [{label: "String voltage (V)"}],
        scales: {y: {range: (u, min, max) => [0, max > 0 ? max * 1.05 : 1]}},
      }),
      xrange: (data, min, max, env) => {                  // the first reading with a lit string to the last
        const lit = [];
        for (let k = 0; k < data[0].length; k++)
          if (data.slice(1).some((c) => c[k] >= env.live_volts)) lit.push(data[0][k]);
        return lit.length > 1 ? [lit[0], lit[lit.length - 1]] : [min, max];
      },
    },

    string_power: {
      aspect: 4.2 / 11,
      night: true,
      side: "string_power_strips",                        // the radar and satellite strips, fetched every few minutes
      tables: (d) => ({main: {t: d.t, cols: d.kw}, meta: {faces: d.faces},
                       aux: {sky: {t: d.sky ? d.sky.t : [], cols: d.sky ? d.sky.kw : {}}}}),
      build: (s) => {
        const t = s.main.t, sky = s.aux.sky || emptyTable(), faces = s.meta.faces || {};
        const kw = STRINGS.map((n) => s.main.cols[n] || []);
        const name = (n) => `String ${n}` + (faces[n] ? ` (${faces[n]})` : "");
        const series = STRINGS.map((n, i) => ({label: name(n), stroke: TAB10[i], width: 0.9, value: shown(2, "kW")}));
        let measured = 0;                                 // kWh: trapezoids between readings, as StringData.kwh
        for (let k = 0; k + 1 < t.length; k++) {
          const a = kw.reduce((sum, c) => sum + c[k], 0), b = kw.reduce((sum, c) => sum + c[k + 1], 0);
          measured += (a + b) / 2 * (t[k + 1] - t[k]) / 3600;
        }
        const out = {data: kw, series, scales: {y: {range: (u, min, max) => [0, max > 0 ? max * 1.05 : 1]}},
                     axes: [{label: "Power (kW) = string voltage × current"}], note: `Measured ${measured.toFixed(1)} kWh`};
        if (sky.t.length) {                               // each string's clear-sky model, dashed and faint, behind it
          const models = STRINGS.map((n) => resample(t, sky.t, sky.cols[n] || [], "linear", 600));
          const step = sky.t.length > 1 ? (sky.t[1] - sky.t[0]) / 3600 : 0;
          const clear = STRINGS.reduce((sum, n) => sum + (sky.cols[n] || []).reduce((a, v) => a + (v || 0), 0), 0) * step;
          out.data = [...models, ...kw];
          out.series = [...STRINGS.map((n, i) => ({label: `${name(n)}, clear sky`, stroke: faint(TAB10[i], 0.55),
                                                   width: 1.3, dash: [6, 4], value: shown(2, "kW")})), ...series];
          out.hidden = [1, 2, 3, 4];                      // one legend entry per string: hiding it hides its model
          out.pairs = {5: 1, 6: 2, 7: 3, 8: 4};
          out.note += `, clear sky ${clear.toFixed(1)} kWh (dashed: each string's clear-sky model)`;
        }
        out.note += ".";
        if (s.side) {
          out.strips = s.side.strips;
          out.note += " " + s.side.notes.join(". ") + ".";
        }
        return out;
      },
      xrange: (data, min, max) => {                       // the first reading with any power to the last
        const kw = data.slice(-4), lit = [];
        for (let k = 0; k < data[0].length; k++) if (kw.some((c) => c[k] > 0)) lit.push(data[0][k]);
        return lit.length > 1 ? [lit[0], lit[lit.length - 1]] : [min, max];
      },
    },

    load_energy: {                                        // horizontal bars: room for the loads' names on the left
      whole: true,
      height: ({raw}) => barsHeight(raw.bars),
      build: ({raw}) => {
        const hours = raw.first ? (Date.parse(raw.last) - Date.parse(raw.first)) / 3600e3 : 0;
        return loadBars(raw.bars, raw.house, "kWh", "Energy (kWh)",
                        raw.first ? `${when(Date.parse(raw.first) / 1000)} to ${when(Date.parse(raw.last) / 1000)} ` +
                                    `(${hours.toFixed(1)} h, ${raw.house.toFixed(2)} kWh in all).`
                                  : "Fewer than two plant readings in this window.");
      },
    },

    load_power_now: {                                     // the same bars, of the newest reading's power
      whole: true,
      height: ({raw}) => barsHeight(raw.bars),
      build: (s) => {
        const raw = s.raw;
        s.span = growingSpan(s.span, Object.values(raw.bars));   // kept in the store: from fetch to fetch
        return loadBars(raw.bars, raw.house, "kW", "Power (kW), the newest reading",
                        raw.at ? `At ${when(Date.parse(raw.at) / 1000)}: the house drew ` +
                                 `${raw.house.toFixed(2)} kW in all.`
                               : "No plant readings in this window.", s.span);
      },
    },

    load_counters: {
      aspect: 4.8 / 11,
      fill: hold,                                         // left out of a top-up: the counter didn't move
      tables: (d) => ({main: {t: d.t, cols: d.kwh}}),
      build: (s) => {
        const labels = Object.keys(s.main.cols), colours = loadColours(labels);
        return {
          data: labels.map((l) => { const c = s.main.cols[l]; return c.map((v) => v - c[0]); }),
          series: labels.map((l, i) => ({label: l, stroke: colours[i], width: 1.5, value: shown(2, "kWh")})),
          axes: [{label: "kWh since start"}],
        };
      },
    },

    load_snapshots: {
      aspect: 4.5 / 11,
      fill: zero,                                         // left out of a top-up: it stayed at 0
      tables: (d) => ({main: {t: d.t, cols: d.kw}}),
      build: (s) => {
        const labels = Object.keys(s.main.cols);
        return {...meansOverReadings(s.main.t, labels, labels.map((l) => s.main.cols[l]), loadColours(labels), "kW", 3),
                axes: [{label: "Power (kW), instantaneous"}]};
      },
    },

    battery_temps: {
      aspect: 4.5 / 11,
      tables: (d) => ({main: {t: d.t, cols: d.c}, aux: {air: {t: d.air.t, cols: {air: d.air.c}}}}),
      build: (s) => {
        const t = s.main.t, c = s.main.cols, air = s.aux.air, coolest = c["coolest cluster"] || [];
        const n = coolest.length;
        return {
          data: [coolest, c["warmest cluster"] || [], c["average cell"] || [],
                 resample(t, air.t, air.cols.air || [], "linear", 6 * 3600),
                 rollingMean(t, c["inverter internal"] || [], MEAN)],
          series: [{label: "coolest cluster", stroke: BLUE, width: 1.8, value: shown(1, "°C")},
                   {label: "warmest cluster", stroke: ORANGE, width: 1, value: shown(1, "°C")},
                   {label: "average cell", stroke: PURPLE, width: 1, value: shown(1, "°C")},
                   {label: "outside air (Open-Meteo)", stroke: GREY, width: 1.2, dash: [6, 4], value: shown(1, "°C")},
                   {label: "inverter internal (5-min mean)", stroke: RED, width: 1.2, scale: "inverter",
                    value: shown(1, "°C")}],
          // Ticks every 10 °C from 0 or below (tempRange); the inverter's axis has as many intervals (alignedRange),
          // so its ticks sit on the battery's grid lines. Series 1–4 (the battery and the air) are on the left.
          axes: [{label: "Battery (°C)", size: AXIS, splits: (u, i, min, max) => evenSplits(min, max, intervals(min, max))},
                 {label: "Inverter internal (°C)", scale: "inverter", side: 1, size: AXIS, stroke: RED,
                  grid: {show: false},
                  splits: (u, i, min, max) => evenSplits(min, max, intervals(u.scales.y.min, u.scales.y.max))}],
          scales: {y: {range: (u, min, max) => tempRange(min, max)},
                   inverter: {range: (u, min, max) => {
                     const [lo, hi] = tempRange(...visibleExtent(u, [1, 2, 3, 4]));
                     return alignedRange(min, max, intervals(lo, hi));
                   }}},
          hooks: {draw: [zeroLine("y")]},
          note: n ? `Coolest cluster: low ${Math.min(...coolest).toFixed(1)} °C, now ${coolest[n - 1].toFixed(1)} °C.` : "",
        };
      },
    },

    battery_power: {
      aspect: 3.5 / 11,
      tables: (d) => ({main: {t: d.t, cols: d.kw}, aux: {soc: {t: d.soc.t, cols: d.soc.pct}}}),
      build: (s) => {
        const t = s.main.t, bat = s.main.cols.battery || [], pv = s.main.cols.pv || [], soc = s.aux.soc;
        const power = meansOverReadings(t, ["pv", "battery"], [pv, bat], [GOLDENROD, GREEN], "kW", 3, 1.2,
                                        ["PV (5-min mean)", "battery, >0 charging, <0 discharging (5-min mean)"]);
        let charged = 0, discharged = 0;                  // each reading's power held until the next
        for (let k = 0; k + 1 < t.length; k++) {
          const kwh = bat[k] * (t[k + 1] - t[k]) / 3600;
          if (kwh > 0) charged += kwh; else discharged -= kwh;
        }
        const out = {
          ...power,
          axes: [{label: "Power (kW)", size: AXIS}],
          scales: {y: {range: (u, min, max) => {          // at least 2 kW tall, around the data's middle
            if (min == null) return [-1, 1];
            if (max - min < 2) return [(min + max) / 2 - 1, (min + max) / 2 + 1];
            return withZero(u, min, max);
          }}},
          hooks: {draw: [zeroLine("y")]},
          note: `Battery ${charged.toFixed(2)} kWh in, ${discharged.toFixed(2)} kWh out`,
        };
        if (soc.t.length) {                               // the SOC and its settings on the right, held from the plant
          const at = (label) => resample(t, soc.t, soc.cols[label] || [], "hold", 60);
          const pct = at("SOC");
          out.data = [...out.data, pct, at("backup reserve"), at("charge cut-off")];
          out.series = [...out.series,
            {label: "SOC", stroke: BLUE, width: 1.4, scale: "soc", value: shown(1, "%")},
            {label: "backup reserve (setting)", stroke: faint(BLUE, 0.6), width: 1.1, dash: [6, 4], scale: "soc",
             value: shown(0, "%")},
            {label: "charge cut-off (setting)", stroke: faint(BLUE, 0.6), width: 1.1, dash: [1.5, 3], scale: "soc",
             value: shown(0, "%")}];
          out.axes = [...out.axes, {label: "SOC (%)", scale: "soc", side: 1, size: AXIS, stroke: BLUE,
                                    grid: {show: false}}];
          out.scales.soc = {range: (u, min, max) => {     // the settings and SOC ± 2, at least 10 points, in 0–100
            if (min == null) return [0, 100];
            let lo = min - 2, hi = max + 2;
            if (hi - lo < 10) [lo, hi] = [(lo + hi) / 2 - 5, (lo + hi) / 2 + 5];
            return [Math.max(lo, 0), Math.min(hi, 100)];
          }};
          const known = pct.filter((v) => v != null);
          if (known.length) out.note += `, SOC ${known[0].toFixed(1)} → ${known[known.length - 1].toFixed(1)} %`;
        }
        out.note += ".";
        return out;
      },
    },
  };

  // ---------- one chart, kept in the page and topped up ----------

  // A chart's note is tucked away behind the ⓘ button by its title (the page toggles data-open); an error shows anyway,
  // so a broken chart isn't silently empty.
  function setNote(note, text) {
    note.textContent = text;
    note.hidden = note.dataset.open !== "1";
  }
  function setError(note, text) {
    note.innerHTML = "";
    const span = root.document.createElement("span");
    span.className = "error";
    span.textContent = text;
    note.appendChild(span);
    note.hidden = false;
  }

  async function getJSON(url) {
    const d = await (await root.fetch(url)).json();
    if (d.error) throw new Error(d.error);
    return d;
  }

  function liveChart(name, spec, env, resizers) {
    const el = root.document.getElementById(`plot-${name}`), note = root.document.getElementById(`note-${name}`);
    const url = env.data[name];
    const fresh = () => (spec.whole ? {raw: null} : {main: emptyTable(), aux: {}, meta: {}, side: null});
    let store = fresh(), last = null, plot = null, key = null, data = null, zoomed = false, busy = false;
    let strips = [], extra = 0, line = null;             // the strips under the plot, the px they take, the cursor
    let status = "", sideStatus = "", loaded = false, scene = null;
    const size = () => ({width: el.clientWidth,                 // a fixed height (spec.height), or by the width
                         height: extra + (spec.height ? spec.height(store)
                                 : Math.round(Math.min(Math.max(el.clientWidth * (spec.aspect || 0.42), 220), 460)))});
    resizers.push(() => plot && plot.setSize(size()));

    // uPlot asks for the x span with the data's ends when it fits the data (at first, and on a double-click), and
    // with the selection when zooming in: so this also tells whether the chart is zoomed in.
    const xrange = (u, min, max) => {
      const x = data[0], n = x.length, auto = n === 0 || (min === x[0] && max === x[n - 1]);
      zoomed = !auto;
      return auto && spec.xrange ? spec.xrange(data, min, max, env) : [min, max];
    };

    function take(d) {                                    // false: start again with the whole window
      if (spec.whole) {
        store.raw = d;
        return true;
      }
      const tables = spec.tables(d);
      if (!append(store.main, tables.main.t, tables.main.cols, spec.fill || hold)) return false;
      Object.assign(store.meta, tables.meta || {});
      for (const [aux, table] of Object.entries(tables.aux || {})) {
        store.aux[aux] = store.aux[aux] || emptyTable();
        append(store.aux[aux], table.t, table.cols);
      }
      if (d.t.length) last = d.t[d.t.length - 1];
      return true;
    }

    function draw() {
      const b = spec.build(store, env);
      data = [b.x || store.main.t, ...b.data];
      strips = b.strips || [];
      const shape = JSON.stringify([b.series.map((s) => s.label), strips.length]);
      // A whole-window chart (the bars) is built afresh every time: its build's hooks and ranges hold that fetch's
      // numbers (each bar's label, the axis's length), which setData wouldn't change. It's a handful of bars.
      if (plot !== null && shape === key && !spec.whole) {
        plot.setData(data, !zoomed);                      // zoomed in: stay there (redraws the strips too)
        return b;
      }
      if (plot !== null) plot.destroy();                  // a load came or went, or the strips came: a new chart
      key = shape;
      zoomed = false;
      const hooks = {...(b.hooks || {})};
      if (b.pairs) hooks.setSeries = [(u, i, o) => {      // a mean and its faint readings show and hide together
        const j = b.pairs[i];
        if (j != null && o && o.show != null && u.series[j].show !== o.show) u.setSeries(j, {show: o.show});
      }];
      let xaxis = b.x ? b.xaxis || {} : {values: timeTicks, ...(b.xaxis || {})};   // (b.x: not a time axis)
      line = null;
      if (strips.length) {                                // a row per strip between the plot and its time labels
        const band = stripRows(strips.length);
        extra = band.height + STRIP.below;
        xaxis = {...xaxis, gap: extra, size: extra + 40, ticks: {show: false}};
        hooks.draw = [...(hooks.draw || []), (u) => paintStrips(u, strips, band)];
        hooks.setCursor = [(u) => line && moveStripCursor(u, line, band)];
      } else {
        extra = 0;
      }
      plot = new root.uPlot({
        ...size(),
        scales: {x: {time: true, range: xrange}, ...(b.scales || {})},
        series: [b.x ? {} : {value: (u, v) => (v == null ? "--" : when(v))}, ...b.series],   // the legend's Time
        axes: [xaxis, ...(b.axes || [])],
        hooks, legend: b.legend || {}, cursor: b.cursor || {},
      }, data, el);
      const rows = plot.root.querySelectorAll(".u-legend .u-series");
      for (const i of b.hidden || []) if (rows[i]) rows[i].style.display = "none";
      if (strips.length) {                                // the cursor's line, carried on down through the strips
        line = root.document.createElement("div");
        line.style.cssText = "position:absolute;width:0;border-left:1px dashed #607d8b;pointer-events:none;display:none";
        plot.over.parentNode.appendChild(line);
      }
      return b;
    }

    // "Today" before sunrise, a chart with spec.night shows a night sky instead (the readings still come in behind
    // it); the first check after sunrise draws the chart.
    const night = () => spec.night && env.sunrise != null && Date.now() / 1000 < env.sunrise;
    const hhmm = (seconds) => { const d = new Date(seconds * 1000); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
    function showNight() {
      const w = el.clientWidth, h = size().height - extra;
      if (scene && scene.dataset.size === `${w}x${h}`) return;
      if (scene) scene.remove();
      const holder = root.document.createElement("div");
      holder.innerHTML = nightSvg(w, h, Math.floor(env.sunrise / 86400), `Sunrise at ${hhmm(env.sunrise)}: the chart starts then.`,
                                  `night-${name}`);
      scene = holder.firstElementChild;
      scene.dataset.size = `${w}x${h}`;
      scene.style.display = "block";
      el.appendChild(scene);
      if (plot) plot.root.style.display = "none";
    }
    function hideNight() {
      if (scene) scene.remove();
      scene = null;
      if (plot) plot.root.style.display = "";
    }
    resizers.push(() => scene && showNight());

    function render() {                                   // the chart, and the line under it
      if (night()) {
        showNight();
        setNote(note, `Before sunrise (${hhmm(env.sunrise)}).` + status + sideStatus);
        return;
      }
      hideNight();
      const b = draw();
      let text = spec.whole ? b.note || "" : "";
      if (!spec.whole) {
        const t = store.main.t, n = t.length;
        text = n ? `${n} readings, ${when(t[0])} to ${when(t[n - 1])}.` : "No readings in this window.";
        if (n && b.note) text += ` ${b.note}`;
      }
      setNote(note, text + status + sideStatus);
    }

    async function updateSide() {                         // the whole window's strips, now and then
      try {
        const d = await getJSON(env.data[spec.side]);
        store.side = {...d, strips: d.strips.map((s) => ({...s, starts: s.starts.map((iso) => Date.parse(iso) / 1000)}))};
        sideStatus = "";
      } catch (e) {
        sideStatus = ` The strips: failed (${e.message}).`;
      }
      if (loaded) render();
    }

    async function update() {
      if (busy) return;                                   // the last check hasn't come back yet
      busy = true;
      const checked = Date.now() / 1000;
      try {
        let d = await getJSON(spec.whole || last === null ? url
                              : `${url}${url.includes("?") ? "&" : "?"}since=${encodeURIComponent(last)}`);
        if (!take(d)) {                                   // a load the chart hasn't got: its earlier readings too
          const side = spec.whole ? null : store.side;
          store = fresh();
          if (side) store.side = side;
          last = null;
          d = await getJSON(url);
          take(d);
        }
        if (env.keep != null && !spec.whole) {            // a sliding window: drop what's slid out of it
          dropBefore(store.main, checked - env.keep);
          for (const table of Object.values(store.aux)) dropBefore(table, checked - env.keep - 3600);
        }
        status = env.live ? ` Checked at ${when(checked)}` + (spec.whole ? "." : `: ${d.t.length} new.`) : "";
        loaded = true;
        render();
      } catch (e) {
        setError(note, `Checked at ${when(checked)}: failed (${e.message})` +
                       (env.live ? `; trying again in ${env.every} s.` : "."));
      } finally {
        busy = false;
      }
    }

    update();
    if (env.live) root.setInterval(update, env.every * 1000);
    if (spec.side) {
      updateSide();
      if (env.live) root.setInterval(updateSide, env.strips_every * 1000);
    }
  }

  // The Sankey diagram at the top (static/my_sankey.js draws it): the whole window's, fetched again every
  // env.sankey_every seconds while the window reaches now.
  function sankeyChart(env, resizers) {
    const canvas = root.document.getElementById("sankey"), note = root.document.getElementById("note-sankey");
    let data = null;
    const draw = () => {
      const s = data && data.sankey;
      canvas.style.display = s && s.sources.length ? "block" : "none";
      if (s && s.sources.length) root.MySankey.draw(canvas, s);
    };
    resizers.push(draw);
    async function update() {
      const checked = Date.now() / 1000;
      try {
        data = await getJSON(env.data.sankey);
        draw();
        const s = data.sankey;
        let text = s ? `${when(Date.parse(data.first) / 1000)} to ${when(Date.parse(data.last) / 1000)}.`
                     : "No readings in this window.";
        if (s && s.dropped.length) {
          text += ` Not drawn: ${s.dropped.length} flow${s.dropped.length > 1 ? "s" : ""} under 0.5 % of the energy ` +
                  `in (${s.dropped.map((f) => `${f.from} → ${f.to} ${f.kwh.toFixed(3)} kWh`).join(", ")}).`;
        }
        setNote(note, text + (env.live ? ` Checked at ${when(checked)}.` : ""));
      } catch (e) {
        setError(note, `Checked at ${when(checked)}: failed (${e.message})` +
                       (env.live ? `; trying again in ${env.sankey_every} s.` : "."));
      }
    }
    update();
    if (env.live) root.setInterval(update, env.sankey_every * 1000);
  }

  // The page: the Sankey diagram, and a live chart in each .plot element, by its id (plot-<name>).
  function start(env) {
    const resizers = [];
    root.addEventListener("resize", () => resizers.forEach((resize) => resize()));
    if (env.data.sankey && root.document.getElementById("sankey") && root.MySankey) sankeyChart(env, resizers);
    root.document.querySelectorAll(".plot").forEach((el) => {
      const name = el.id.slice("plot-".length);
      if (name in env.data && name in CHARTS) liveChart(name, CHARTS[name], env, resizers);
    });
    if (env.today) {                                      // "today" after midnight is a new day: load it
      const day = new Date().toDateString();
      root.setInterval(() => { if (new Date().toDateString() !== day) root.location.reload(); }, 60 * 1000);
    }
  }

  const api = {rollingMean, resample, emptyTable, append, dropBefore, hold, zero, stripRows, stripSpans, nightSvg, tempRange, alignedRange, evenSplits, timeTicks, growingSpan,
               CHARTS, start};
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.LiveCharts = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
