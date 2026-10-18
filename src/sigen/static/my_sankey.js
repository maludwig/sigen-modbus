/* my_sankey.js: a small Sankey diagram on a <canvas>, for sigen's dashboard, daily and all-time pages. No dependencies.

   data = {sources: [{name, kwh, colour}], sinks: [{name, kwh, colour}], flows: [{from, to, kwh}]}
   (`from` indexes sources, `to` indexes sinks; colours are "#rrggbb").

   Every vertical length is true: one kWh-to-pixel scale for everything. Each band is exactly kwh × scale thick at
   both ends, and each node is a plain BAR in its colour spanning exactly its bands, stacked in it in the order of the
   nodes at their other ends. The label shows the node's own kwh (its counter): it can be a little more than its bands
   (the inverter's losses), which the bar doesn't draw. The bands pass behind the bars. Each node also has a SLOT (not
   drawn): as tall as the bar, or as its label needs (minNode), whichever is more, with the bar along its inner edge,
   in its middle, and the label beside the bar on the page's background. The scale is the largest at which both
   columns of slots fit the canvas. Sources on the left, sinks on the right; bands are cubic Béziers, flat at both
   ends, filled with a left-to-right gradient from the source's colour to the sink's. A band thinner than a pixel is
   drawn as a hairline (minBand); the page leaves out flows that small anyway.

   layout() is pure arithmetic, tested with Node (tests/js); draw() paints a layout onto a canvas. In a browser this
   file sets the global MySankey; in Node, require() returns the same functions. */

/**
 * One source or sink as the server sends it (charts.sankey()).
 * @typedef {Object} SankeyDataNode
 * @property {string} name  "Solar", "Battery", "Grid" or "Load"
 * @property {number} kwh  its counter's kWh; 0 without one
 * @property {string} colour  "#rrggbb"
 */

/**
 * One flow from a source to a sink, as the server sends it.
 * @typedef {Object} SankeyFlow
 * @property {number} from  an index into sources
 * @property {number} to  an index into sinks
 * @property {number} kwh
 */

/**
 * The diagram's input: charts.sankey() from the server. dropped and cutoff
 * are for the note under it; the drawing doesn't read them.
 * @typedef {Object} SankeyData
 * @property {SankeyDataNode[]} sources
 * @property {SankeyDataNode[]} sinks
 * @property {SankeyFlow[]} flows
 * @property {SankeyFlow[]} [dropped]  flows too small to draw
 * @property {number} [cutoff]  kWh: a flow under this is dropped
 */

/**
 * The options: DEFAULTS, any of them overridden, plus draw()'s height.
 * @typedef {Object} SankeyOptions
 * @property {number} nodeWidth  a slot's width, a share of the canvas's
 * @property {number} minNodeWidth  CSS px
 * @property {number} maxNodeWidth  CSS px
 * @property {number} gap  CSS px between the slots of a column
 * @property {number} minNode  CSS px: the shortest a slot gets
 * @property {number} minBand  CSS px: the thinnest a band or bar gets
 * @property {number} barWidth  CSS px
 * @property {number} bandAlpha  the bands' opacity, 0-1
 * @property {number} [height]  draw() only: the canvas's CSS px height
 */

/**
 * A node as layout() places it, in CSS px from the canvas's top left.
 * @typedef {Object} SankeyNode
 * @property {"left"|"right"} side  sources left, sinks right
 * @property {number} index  into data.sources or data.sinks
 * @property {string} name
 * @property {string} colour  "#rrggbb"
 * @property {number} kwh  its label: its counter, at least its bands
 * @property {number} banded  its bands' kWh
 * @property {number} share  of its column's kwh, 0-1
 * @property {number} x  the slot
 * @property {number} y
 * @property {number} w
 * @property {number} h
 * @property {number} barY  the bar, within the slot
 * @property {number} barH
 */

/**
 * A band as layout() places it, in CSS px: y0 / y0b its top and bottom at
 * its source, y1 / y1b at its sink.
 * @typedef {Object} SankeyBand
 * @property {number} from  an index into data.sources
 * @property {number} to  an index into data.sinks
 * @property {number} kwh
 * @property {number} x0  the sources' inner edge
 * @property {number} y0
 * @property {number} y0b
 * @property {number} x1  the sinks' inner edge
 * @property {number} y1
 * @property {number} y1b
 * @property {string} colour0  the source's, "#rrggbb"
 * @property {string} colour1  the sink's, "#rrggbb"
 */

/**
 * What layout() returns (and draw() too).
 * @typedef {Object} SankeyLayout
 * @property {number} width  CSS px
 * @property {number} height  CSS px
 * @property {number} scale  CSS px per kWh; 0 if even the minimum slots
 *   don't fit
 * @property {SankeyNode[]} nodes  the sources, then the sinks
 * @property {SankeyBand[]} bands
 */
(function (root) {
  "use strict";

  const DEFAULTS = {
    nodeWidth: 0.22,        // a slot's width (bar and label), as a share of the canvas width…
    minNodeWidth: 84,       // …within these (CSS pixels)
    maxNodeWidth: 170,
    gap: 12,                // between the slots of a column
    minNode: 44,            // the shortest a slot gets: its label's NAME and value
    minBand: 1,             // a band or bar thinner than this is drawn this thick: a tiny flow still shows
    barWidth: 10,           // the bar, along the slot's inner edge
    bandAlpha: 0.55,
  };

  const clamp = (x, lo, hi) => Math.min(Math.max(x, lo), hi);
  const sum = (xs) => xs.reduce((a, b) => a + b, 0);

  /**
   * The largest kWh-to-pixel scale at which every column's cards (each
   * max(kwh × scale, minNode) tall, gap apart) fit `height`; 0 if even the
   * minimum-height cards don't.
   * @param {{kwh: number}[][]} columns  each column's nodes
   * @param {number} height  CSS px
   * @param {{minNode: number, gap: number}} o  CSS px, as in SankeyOptions
   * @returns {number}  CSS px per kWh
   */
  function fitScale(columns, height, o) {
    const used = (nodes, s) => sum(nodes.map((n) => Math.max(n.kwh * s, o.minNode))) + o.gap * Math.max(nodes.length - 1, 0);
    const fits = (s) => columns.every((nodes) => used(nodes, s) <= height + 1e-9);
    const full = columns.filter((nodes) => nodes.length)
      .map((nodes) => (height - o.gap * (nodes.length - 1)) / sum(nodes.map((n) => n.kwh)));
    let hi = full.length ? Math.min(...full) : 0;                     // no minimum heights: the most it can be
    if (!(hi > 0)) return 0;
    if (fits(hi)) return hi;
    let lo = 0;
    for (let i = 0; i < 60; i++) {                                    // bisect: `used` only grows with the scale
      const mid = (lo + hi) / 2;
      if (fits(mid)) lo = mid; else hi = mid;
    }
    return lo;
  }

  /**
   * Where everything goes on a width × height canvas: {scale (px per kWh),
   * nodes: [{side, index, name, kwh (its label: its counter, at least its
   * bands), banded (its bands' kWh), colour, share, x, y, w, h (the slot),
   * barY, barH}], bands: [{from, to, kwh, x0, y0, y0b, x1, y1, y1b, colour0,
   * colour1}]} (y0 / y0b: the band's top and bottom at its source; y1 / y1b
   * at its sink). Nodes without a band, and flows of 0, are left out.
   * @param {SankeyData} data  from charts.sankey()
   * @param {number} width  CSS px
   * @param {number} height  CSS px
   * @param {Partial<SankeyOptions>} [opts]  overrides of DEFAULTS
   * @returns {SankeyLayout}
   */
  function layout(data, width, height, opts) {
    const o = Object.assign({}, DEFAULTS, opts || {});
    const flows = (data.flows || []).filter((f) => f.kwh > 0);
    const nodeW = clamp(width * o.nodeWidth, o.minNodeWidth, o.maxNodeWidth);
    const column = (list, side, key) => (list || []).map((n, index) => {
      const banded = sum(flows.filter((f) => f[key] === index).map((f) => f.kwh));
      return {side, index, name: n.name, colour: n.colour, banded, kwh: Math.max(n.kwh || 0, banded)};
    }).filter((n) => n.banded > 0);
    const left = column(data.sources, "left", "from");
    const right = column(data.sinks, "right", "to");
    // Sized by what's drawn: the bands. (A hairline floor can add a pixel or two; the page drops flows that small.)
    const scale = fitScale([left, right].map((c) => c.map((n) => ({kwh: n.banded}))), height, o);
    const thickness = (kwh) => Math.max(kwh * scale, o.minBand);
    const bandsOf = (n) => flows.filter((f) => f[n.side === "left" ? "from" : "to"] === n.index);

    for (const [nodes, x] of [[left, 0], [right, width - nodeW]]) {
      const bars = nodes.map((n) => sum(bandsOf(n).map((f) => thickness(f.kwh))));   // a bar spans its bands
      const slots = bars.map((b) => Math.max(b, o.minNode));
      const total = sum(nodes.map((n) => n.kwh));
      let y = Math.max((height - sum(slots) - o.gap * Math.max(nodes.length - 1, 0)) / 2, 0);   // centred
      nodes.forEach((n, i) => {
        Object.assign(n, {x, y, w: nodeW, h: slots[i], barY: y + (slots[i] - bars[i]) / 2, barH: bars[i],
                          share: n.kwh / total});
        y += slots[i] + o.gap;
      });
    }

    const find = (nodes, index) => nodes.find((n) => n.index === index);
    const bands = flows.map((f) => ({f, src: find(left, f.from), dst: find(right, f.to)}))
      .filter((b) => b.src && b.dst)
      .map(({f, src, dst}) => ({from: f.from, to: f.to, kwh: f.kwh, src, dst,
                                x0: src.x + src.w, x1: dst.x, colour0: src.colour, colour1: dst.colour}));
    // Stack each bar's bands from its top, ordered by the node at the other end, so they cross as little as possible.
    for (const n of left) {
      let y = n.barY;
      for (const b of bands.filter((b) => b.src === n).sort((a, b) => a.dst.y - b.dst.y)) {
        b.y0 = y;
        y += thickness(b.kwh);
        b.y0b = y;
      }
    }
    for (const n of right) {
      let y = n.barY;
      for (const b of bands.filter((b) => b.dst === n).sort((a, b) => a.src.y - b.src.y)) {
        b.y1 = y;
        y += thickness(b.kwh);
        b.y1b = y;
      }
    }
    bands.forEach((b) => { delete b.src; delete b.dst; });
    return {width, height, scale, nodes: left.concat(right), bands};
  }

  /**
   * A "#rrggbb" colour's red, green and blue, each 0-255.
   * @param {string} hex
   * @returns {number[]}  [r, g, b]
   */
  function rgb(hex) {
    const v = parseInt(hex.slice(1), 16);
    return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
  }
  const rgba = (hex, alpha) => `rgba(${rgb(hex).join(",")},${alpha})`;
  const darker = (hex, by = 0.3) => `rgb(${rgb(hex).map((c) => Math.round(c * (1 - by))).join(",")})`;

  const FONT = "system-ui, sans-serif";
  const SMALL = 11;            // the name and the share
  const PAD = 6, LEADING = 3;  // around a label, and between its lines: NAME and the value fit a 44 px slot
  const kwhText = (n) => `${n.kwh.toFixed(2)} kWh`;

  /**
   * One size for every node's kWh (so they all match): the largest up to
   * maxSize at which each one fits its slot, less `inset` (the bar).
   * measure(text, size) is its width in bold at that size (the canvas
   * measures it; tests pass their own).
   * @param {{kwh: number, w: number}[]} nodes  SankeyNodes, or stand-ins
   * @param {function(string, number): number} measure  text, size -> px
   * @param {number} [inset]  CSS px
   * @param {number} [maxSize]  px
   * @param {number} [minSize]  px: returned when no larger size fits
   * @returns {number}  a font size, px
   */
  function valueSize(nodes, measure, inset = 0, maxSize = 18, minSize = 9) {
    for (let size = maxSize; size > minSize; size--) {
      if (nodes.every((n) => measure(kwhText(n), size) <= n.w - 2 * PAD - inset)) return size;
    }
    return minSize;
  }

  /**
   * The node's label beside its bar, on the outer side, centred on the bar
   * (within its slot), as much as the slot's height allows: NAME (in the
   * node's colour, darkened to read on white) / 40.45 kWh / 99.4 %, or
   * without the share, or just the value.
   * @param {CanvasRenderingContext2D} ctx  scaled to CSS px
   * @param {SankeyNode} n
   * @param {number} size  the value's font size, px, from valueSize()
   * @param {SankeyOptions} o  only barWidth is read
   * @returns {void}
   */
  function label(ctx, n, size, o) {
    const all = [[`bold ${SMALL}px ${FONT}`, SMALL, n.name.toUpperCase(), darker(n.colour, 0.35)],
                 [`bold ${size}px ${FONT}`, size, kwhText(n), "#1d1d1f"],
                 [`${SMALL}px ${FONT}`, SMALL, `${(n.share * 100).toFixed(1)} %`, "#666"]];
    const tall = (lines) => lines.reduce((h, [, s]) => h + s, 0) + LEADING * (lines.length - 1);
    const lines = [all, all.slice(0, 2), all.slice(1, 2)].find((ls) => tall(ls) + 2 * PAD <= n.h) || all.slice(1, 2);
    const left = n.side === "left";
    ctx.textAlign = left ? "right" : "left";             // against the bar: left of it on the left, right on the right
    ctx.textBaseline = "top";
    const x = left ? n.x + n.w - o.barWidth - PAD : n.x + o.barWidth + PAD;
    let y = clamp(n.barY + n.barH / 2 - tall(lines) / 2, n.y + PAD, n.y + n.h - PAD - tall(lines));
    for (const [font, lineSize, text, colour] of lines) {
      ctx.font = font;
      ctx.fillStyle = colour;
      ctx.fillText(text, x, y);               // no maxWidth: the canvas would squeeze the letters to fit
      y += lineSize + LEADING;
    }
  }

  /**
   * Paint `data` onto `canvas`: as wide as the canvas is on screen,
   * opts.height tall (default: 70 % of the width, within 300-520 px), sharp
   * on high-density screens. Returns the layout.
   * @param {HTMLCanvasElement} canvas
   * @param {SankeyData} data  from charts.sankey()
   * @param {Partial<SankeyOptions>} [opts]  overrides of DEFAULTS, and height
   * @returns {SankeyLayout}
   */
  function draw(canvas, data, opts) {
    const o = Object.assign({}, DEFAULTS, opts || {});
    const width = canvas.clientWidth;
    const height = o.height || Math.round(clamp(width * 0.7, 300, 520));
    const dpr = root.devicePixelRatio || 1;
    canvas.style.height = `${height}px`;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const l = layout(data, width, height, o);
    for (const b of l.bands) {
      const mid = (b.x0 + b.x1) / 2;
      const gradient = ctx.createLinearGradient(b.x0, 0, b.x1, 0);
      gradient.addColorStop(0, rgba(b.colour0, o.bandAlpha));
      gradient.addColorStop(1, rgba(b.colour1, o.bandAlpha));
      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.moveTo(b.x0, b.y0);
      ctx.bezierCurveTo(mid, b.y0, mid, b.y1, b.x1, b.y1);       // the top edge, flat at both ends
      ctx.lineTo(b.x1, b.y1b);
      ctx.bezierCurveTo(mid, b.y1b, mid, b.y0b, b.x0, b.y0b);    // the bottom edge, back
      ctx.closePath();
      ctx.fill();
    }
    const size = valueSize(l.nodes, (text, s) => { ctx.font = `bold ${s}px ${FONT}`; return ctx.measureText(text).width; },
                           o.barWidth);
    for (const n of l.nodes) {
      ctx.fillStyle = n.colour;                                   // the bar: plain, sharp, over the bands' ends
      ctx.fillRect(n.side === "left" ? n.x + n.w - o.barWidth : n.x, n.barY, o.barWidth, Math.max(n.barH, o.minBand));
      label(ctx, n, size, o);
    }
    return l;
  }

  const api = {DEFAULTS, fitScale, layout, valueSize, draw};
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.MySankey = api;
  }
})(typeof window !== "undefined" ? window : globalThis);
