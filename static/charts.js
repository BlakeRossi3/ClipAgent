/* Shorts Agent dashboard charts: dependency-free inline SVG.
 * Each <div class="chart" data-chart="ID"> reads its spec from <script type="application/json" id="ID-data">.
 * Specs are produced by dashboard.py (time_chart / insights scatter / retention curve).
 * All labels are inserted with textContent (titles come from model output and external metadata).
 * Every chart has a server-rendered table twin, so the tooltip never gates a value.
 */
(function () {
  'use strict';
  var NS = 'http://www.w3.org/2000/svg';
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var tip = null;

  function el(tag, attrs, parent) {
    var e = document.createElementNS(NS, tag);
    if (attrs) { for (var k in attrs) { if (attrs[k] !== undefined && attrs[k] !== null) e.setAttribute(k, attrs[k]); } }
    if (parent) parent.appendChild(e);
    return e;
  }

  function strip0(s) { return s.replace(/\.0(?=[KMB]?$)/, ''); }
  function compact(v) {
    var a = Math.abs(v), sign = v < 0 ? '-' : '';
    if (a < 1000) return sign + (a >= 10 || a === Math.round(a) ? Math.round(a).toString() : a.toFixed(1));
    var units = [[1e9, 'B'], [1e6, 'M'], [1e3, 'K']];
    for (var i = 0; i < units.length; i++) {
      if (a >= units[i][0]) { var x = a / units[i][0]; return sign + strip0(x < 100 ? x.toFixed(1) : x.toFixed(0)) + units[i][1]; }
    }
    return sign + a.toFixed(0);
  }
  function usd(v) {
    var a = Math.abs(v), sign = v < 0 ? '-' : '';
    if (a === 0) return '$0';
    if (a >= 10000) return sign + '$' + compact(a);
    if (a >= 100) return sign + '$' + Math.round(a).toLocaleString('en-US');
    if (a >= 0.995) return sign + '$' + a.toFixed(2);
    if (a >= 0.1) return sign + '$' + a.toFixed(2);
    if (a >= 0.01) return sign + '$' + a.toFixed(3);
    if (a >= 0.0001) return sign + '$' + a.toFixed(4).replace(/0+$/, '');
    return sign + '<$0.0001';
  }
  function fmt(v, kind) {
    if (v === null || v === undefined || isNaN(v)) return '–';
    if (kind === 'usd') return usd(v);
    if (kind === 'pct') return (Math.abs(v) < 10 ? v.toFixed(1) : Math.round(v).toString()) + '%';
    if (kind === 'int') return Math.round(v).toLocaleString('en-US');
    return compact(v);
  }
  function axisFmt(v, kind) {
    if (kind === 'usd') { return v === 0 ? '$0' : (Math.abs(v) < 1 ? '$' + v.toFixed(2) : '$' + compact(v)); }
    if (kind === 'pct') return Math.round(v) + '%';
    return Math.abs(v) >= 10000 ? compact(v) : Math.round(v).toLocaleString('en-US');
  }
  function dateLabel(iso) { var p = String(iso).split('-'); return MONTHS[+p[1] - 1] + ' ' + (+p[2]); }
  function longDate(iso) {
    var p = String(iso).split('-');
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    var wd = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getUTCDay()];
    return wd + ', ' + dateLabel(iso) + ', ' + p[0];
  }

  function niceTicks(min, max, count, integer) {
    if (max === min) { max = min + (integer ? 1 : 1); }
    var raw = (max - min) / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var n = raw / mag;
    var step = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
    if (integer && step < 1) step = 1;
    var lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    var out = [];
    for (var v = lo; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }

  /* ---------- tooltip ---------- */
  function tooltip() {
    if (!tip) tip = document.getElementById('tooltip');
    return tip;
  }
  function showTip(title, rows, x, y) {
    var t = tooltip();
    if (!t) return;
    while (t.firstChild) t.removeChild(t.firstChild);
    var h = document.createElement('div');
    h.className = 'tt-title';
    h.textContent = title;
    t.appendChild(h);
    rows.forEach(function (r) {
      var row = document.createElement('div');
      row.className = 'tt-row';
      if (r.slot || r.ref) {
        var k = document.createElement('span');
        k.className = 'tt-key' + (r.ref ? ' ref' : ' slot-' + r.slot);
        row.appendChild(k);
      }
      var v = document.createElement('strong');
      v.textContent = r.value;
      row.appendChild(v);
      var n = document.createElement('span');
      n.className = 'tt-name';
      n.textContent = r.name;
      row.appendChild(n);
      t.appendChild(row);
    });
    t.hidden = false;
    var vw = window.innerWidth, vh = window.innerHeight;
    var r = t.getBoundingClientRect();
    var left = x + 14, top = y + 14;
    if (left + r.width > vw - 8) left = Math.max(8, x - r.width - 14);
    if (top + r.height > vh - 8) top = Math.max(8, y - r.height - 14);
    t.style.left = left + 'px';
    t.style.top = top + 'px';
  }
  function hideTip() { var t = tooltip(); if (t) t.hidden = true; }

  function roundedBar(x, yTop, w, h, up) {
    // rounded data-end (4px), square at the baseline
    var r = Math.max(0, Math.min(4, w / 2, h));
    if (h <= 0) return '';
    if (up) {
      return 'M' + x + ',' + (yTop + h) + 'V' + (yTop + r) + 'Q' + x + ',' + yTop + ' ' + (x + r) + ',' + yTop +
        'H' + (x + w - r) + 'Q' + (x + w) + ',' + yTop + ' ' + (x + w) + ',' + (yTop + r) + 'V' + (yTop + h) + 'Z';
    }
    var yb = yTop + h;
    return 'M' + x + ',' + yTop + 'V' + (yb - r) + 'Q' + x + ',' + yb + ' ' + (x + r) + ',' + yb +
      'H' + (x + w - r) + 'Q' + (x + w) + ',' + yb + ' ' + (x + w) + ',' + (yb - r) + 'V' + yTop + 'Z';
  }

  /* ---------- time series: line / bars / curve ---------- */
  function renderSeries(box, spec) {
    var W = box.clientWidth, H = box.clientHeight || spec.height || 200;
    if (W < 40) return;
    var n = spec.x.length;
    var isBars = spec.type === 'bars', isCurve = spec.type === 'curve';
    var series = spec.series || [], refs = spec.refs || [];
    var vals = [0];
    if (spec.stacked) {
      for (var i = 0; i < n; i++) {
        var pos = 0, neg = 0;
        series.forEach(function (s) { var v = s.values[i] || 0; if (v >= 0) pos += v; else neg += v; });
        vals.push(pos, neg);
      }
    } else {
      series.forEach(function (s) { s.values.forEach(function (v) { if (v !== null && v !== undefined) vals.push(v); }); });
    }
    refs.forEach(function (r) { r.values.forEach(function (v) { if (v !== null && v !== undefined) vals.push(v); }); });
    var ymin = Math.min.apply(null, vals), ymax = Math.max.apply(null, vals);
    if (ymax === ymin) ymax = ymin + 1;
    var integer = spec.fmt === 'int' || spec.fmt === 'compact';
    var yt = niceTicks(ymin, ymax, H < 180 ? 3 : 4, integer);
    var y0 = yt[0], y1 = yt[yt.length - 1];
    var labels = yt.map(function (v) { return axisFmt(v, spec.fmt); });
    var left = Math.max(26, Math.max.apply(null, labels.map(function (s) { return s.length; })) * 6.6 + 10);
    var right = 10, top = 10, bottom = 24;
    var pw = Math.max(10, W - left - right), ph = Math.max(10, H - top - bottom);
    var band = pw / Math.max(1, n);
    function X(i) {
      if (isCurve) return left + pw * spec.x[i];
      if (isBars) return left + band * (i + 0.5);
      return n === 1 ? left + pw / 2 : left + pw * i / (n - 1);
    }
    function Y(v) { return top + ph * (1 - (v - y0) / (y1 - y0)); }

    var svg = el('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, tabindex: '0', focusable: 'true' });
    var ax = el('g', { 'class': 'ax' }, svg);
    yt.forEach(function (v, k) {
      var y = Math.round(Y(v)) + 0.5;
      el('line', { x1: left, x2: left + pw, y1: y, y2: y, 'class': v === 0 ? 'base' : 'grid' }, ax);
      var t = el('text', { x: left - 8, y: y + 4, 'text-anchor': 'end' }, ax);
      t.textContent = labels[k];
    });
    // x labels
    if (isCurve) {
      [0, 0.25, 0.5, 0.75, 1].forEach(function (f, k) {
        var t = el('text', { x: left + pw * f, y: H - 6, 'text-anchor': k === 0 ? 'start' : (k === 4 ? 'end' : 'middle') }, ax);
        t.textContent = Math.round(f * 100) + '%';
      });
    } else if (n > 0) {
      var maxLabels = Math.max(2, Math.floor(pw / 64));
      var step = Math.max(1, Math.ceil(n / maxLabels));
      var idx = [];
      for (var j = n - 1; j >= 0; j -= step) idx.push(j);
      idx.forEach(function (j) {
        var x = X(j), anchor = 'middle';
        if (x - left < 22) anchor = 'start';
        if (left + pw - x < 22) anchor = 'end';
        var t = el('text', { x: anchor === 'start' ? Math.max(left, x - (isBars ? band / 2 : 0)) : (anchor === 'end' ? Math.min(left + pw, x + (isBars ? band / 2 : 0)) : x), y: H - 6, 'text-anchor': anchor }, ax);
        t.textContent = dateLabel(spec.x[j]);
      });
    }

    var marks = el('g', null, svg);
    var baseY = Y(Math.max(y0, Math.min(0, y1)));
    if (isBars) {
      var gap = band >= 8 ? 2 : (band >= 4 ? 1 : 0);
      var bw = Math.max(1, Math.min(24, band - gap * 2, band * 0.72));
      for (var i2 = 0; i2 < n; i2++) {
        var x0 = X(i2) - bw / 2, accPos = 0, accNeg = 0;
        var segs = [];
        series.forEach(function (s) {
          var v = s.values[i2];
          if (!v) return;
          if (spec.stacked) {
            if (v >= 0) { segs.push({ a: accPos, b: accPos + v, slot: s.slot }); accPos += v; }
            else { segs.push({ a: accNeg, b: accNeg + v, slot: s.slot }); accNeg += v; }
          } else {
            segs.push({ a: 0, b: v, slot: s.slot });
          }
        });
        var lastPos = -1, lastNeg = -1;
        segs.forEach(function (sg, k) { if (sg.b >= 0) lastPos = k; else lastNeg = k; });
        segs.forEach(function (sg, k) {
          var up = sg.b >= 0;
          var ya = Y(sg.a), yb = Y(sg.b);
          var yTop = Math.min(ya, yb), h = Math.abs(yb - ya);
          var gapPx = (spec.stacked && sg.a !== 0) ? 2 : 0; // 2px surface gap between stacked segments
          if (up) { h -= gapPx; } else { yTop += gapPx; h -= gapPx; }
          if (h < 0.5) h = 0.5;
          var isEnd = up ? k === lastPos : k === lastNeg;
          var d = isEnd ? roundedBar(x0, yTop, bw, h, up) :
            'M' + x0 + ',' + yTop + 'h' + bw + 'v' + h + 'h' + (-bw) + 'Z';
          el('path', { d: d, 'class': 'mark-bar slot-' + sg.slot }, marks);
        });
      }
    } else {
      series.forEach(function (s) {
        var d = '', area = '', started = false, firstX = null, lastX = null, last = null;
        s.values.forEach(function (v, i3) {
          if (v === null || v === undefined) { started = false; return; }
          var x = X(i3), y = Y(v);
          d += (started ? 'L' : 'M') + x.toFixed(1) + ',' + y.toFixed(1);
          if (!started && firstX === null) firstX = x;
          started = true;
          lastX = x; last = { x: x, y: y };
        });
        if (spec.area && series.length === 1 && d) {
          var pts = d.replace(/^M/, '');
          area = 'M' + firstX.toFixed(1) + ',' + baseY.toFixed(1) + 'L' + pts + 'L' + lastX.toFixed(1) + ',' + baseY.toFixed(1) + 'Z';
          // only safe when there are no gaps
          if (d.indexOf('M', 1) === -1) el('path', { d: area, 'class': 'mark-area slot-' + s.slot }, marks);
        }
        if (d) el('path', { d: d, 'class': 'mark-line slot-' + s.slot }, marks);
        if (last) el('circle', { cx: last.x, cy: last.y, r: 4, 'class': 'mark-dot slot-' + s.slot }, marks);
      });
    }
    // reference lines (caps): step line across bands for bars
    refs.forEach(function (r) {
      var d = '';
      r.values.forEach(function (v, i4) {
        if (v === null || v === undefined) return;
        var y = Y(v).toFixed(1);
        if (isBars) {
          var xa = left + band * i4, xb = left + band * (i4 + 1);
          d += (d ? 'L' : 'M') + xa.toFixed(1) + ',' + y + 'L' + xb.toFixed(1) + ',' + y;
        } else {
          d += (d ? 'L' : 'M') + X(i4).toFixed(1) + ',' + y;
        }
      });
      if (!d) return;
      el('path', { d: d, 'class': 'mark-ref' }, marks);
      var lastV = r.values[r.values.length - 1];
      if (lastV !== null && lastV !== undefined && !isCurve) {
        var lbl = el('text', { x: left + pw, y: Y(lastV) - 5, 'text-anchor': 'end', 'class': 'ref-label' }, marks);
        lbl.textContent = 'cap ' + fmt(lastV, spec.fmt);
      }
    });

    // hover layer
    var hover = el('g', null, svg);
    var cross = el('line', { y1: top, y2: top + ph, 'class': 'crosshair', visibility: 'hidden' }, hover);
    var hl = el('rect', { y: top, height: ph, 'class': 'band-hit is-hover', visibility: 'hidden' }, hover);
    var hit = el('rect', { x: left, y: top, width: pw, height: ph, fill: 'transparent' }, svg);
    var current = -1;
    function nearest(px) {
      if (isCurve) {
        var best = 0, bd = Infinity;
        for (var q = 0; q < n; q++) { var dd = Math.abs(X(q) - px); if (dd < bd) { bd = dd; best = q; } }
        return best;
      }
      if (isBars) return Math.max(0, Math.min(n - 1, Math.floor((px - left) / band)));
      return n === 1 ? 0 : Math.max(0, Math.min(n - 1, Math.round((px - left) / pw * (n - 1))));
    }
    function show(i5, cx, cy) {
      current = i5;
      if (isBars) { hl.setAttribute('x', left + band * i5); hl.setAttribute('width', band); hl.setAttribute('visibility', 'visible'); }
      else { cross.setAttribute('x1', X(i5)); cross.setAttribute('x2', X(i5)); cross.setAttribute('visibility', 'visible'); }
      var rows = [];
      var total = 0;
      series.forEach(function (s) {
        var v = s.values[i5];
        total += v || 0;
        rows.push({ name: s.name, value: fmt(v, spec.fmt), slot: s.slot });
      });
      if (spec.stacked && series.length > 1) rows.push({ name: 'Total', value: fmt(total, spec.fmt) });
      refs.forEach(function (r) { rows.push({ name: r.name, value: fmt(r.values[i5], spec.fmt), ref: true }); });
      var title = isCurve ? 'At ' + Math.round(spec.x[i5] * 100) + '% of the clip' : longDate(spec.x[i5]);
      showTip(title, rows, cx, cy);
    }
    function hide() { current = -1; cross.setAttribute('visibility', 'hidden'); hl.setAttribute('visibility', 'hidden'); hideTip(); }
    hit.addEventListener('pointermove', function (ev) {
      var rect = svg.getBoundingClientRect();
      show(nearest(ev.clientX - rect.left), ev.clientX, ev.clientY);
    });
    hit.addEventListener('pointerleave', hide);
    svg.addEventListener('blur', hide);
    svg.addEventListener('keydown', function (ev) {
      if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight' && ev.key !== 'Home' && ev.key !== 'End') return;
      ev.preventDefault();
      var i6 = current < 0 ? n - 1 : current;
      if (ev.key === 'ArrowLeft') i6 = Math.max(0, i6 - 1);
      if (ev.key === 'ArrowRight') i6 = Math.min(n - 1, i6 + 1);
      if (ev.key === 'Home') i6 = 0;
      if (ev.key === 'End') i6 = n - 1;
      var rect = svg.getBoundingClientRect();
      show(i6, rect.left + X(i6), rect.top + top + 10);
    });
    svg.addEventListener('focus', function () {
      var rect = svg.getBoundingClientRect();
      show(n - 1, rect.left + X(n - 1), rect.top + top + 10);
    });
    box.appendChild(svg);
  }

  /* ---------- scatter (Haiku score vs views) ---------- */
  function hash(n) { var x = Math.sin(n * 12.9898) * 43758.5453; return x - Math.floor(x); }
  function renderScatter(box, spec) {
    var W = box.clientWidth, H = box.clientHeight || spec.height || 260;
    if (W < 40) return;
    var pts = spec.points || [];
    var lg = function (v) { return Math.log(Math.max(0, v) + 1) / Math.LN10; };
    var maxY = Math.max(1, Math.max.apply(null, pts.map(function (p) { return lg(p.y); })));
    var decades = Math.ceil(maxY);
    var ytv = [];
    for (var d = 0; d <= decades; d++) ytv.push(d === 0 ? 0 : Math.pow(10, d));
    var labels = ytv.map(function (v) { return compact(v); });
    var left = Math.max(28, Math.max.apply(null, labels.map(function (s) { return s.length; })) * 6.6 + 10);
    var right = 12, top = 10, bottom = 34;
    var pw = W - left - right, ph = H - top - bottom;
    var xd = spec.xDomain || [1, 10];
    var x0 = Math.floor(xd[0]) - 0.5, x1 = xd[1] + 0.5;
    function X(v) { return left + pw * (v - x0) / (x1 - x0); }
    function Y(v) { return top + ph * (1 - lg(v) / decades); }
    var svg = el('svg', { width: W, height: H, viewBox: '0 0 ' + W + ' ' + H });
    var ax = el('g', { 'class': 'ax' }, svg);
    ytv.forEach(function (v, k) {
      var y = Math.round(Y(v)) + 0.5;
      el('line', { x1: left, x2: left + pw, y1: y, y2: y, 'class': k === 0 ? 'base' : 'grid' }, ax);
      var t = el('text', { x: left - 8, y: y + 4, 'text-anchor': 'end' }, ax);
      t.textContent = labels[k];
    });
    for (var s = Math.ceil(xd[0]); s <= xd[1]; s++) {
      var t2 = el('text', { x: X(s), y: top + ph + 16, 'text-anchor': 'middle' }, ax);
      t2.textContent = String(s);
    }
    var xl = el('text', { x: left + pw / 2, y: H - 2, 'text-anchor': 'middle' }, ax);
    xl.textContent = spec.xLabel || '';
    var g = el('g', null, svg);
    var placed = pts.map(function (p) {
      var jx = (hash(p.id || 0) - 0.5) * 0.5;
      var c = { x: X(p.x + jx), y: Y(p.y), p: p };
      c.node = el('circle', { cx: c.x.toFixed(1), cy: c.y.toFixed(1), r: 4, 'class': 'scatter-dot' }, g);
      return c;
    });
    var hit = el('rect', { x: left, y: top, width: pw, height: ph, fill: 'transparent' }, svg);
    var cur = null;
    function nearestPt(mx, my) {
      var best = null, bd = 24 * 24;
      placed.forEach(function (c) { var dx = c.x - mx, dy = c.y - my, dd = dx * dx + dy * dy; if (dd < bd) { bd = dd; best = c; } });
      return best;
    }
    hit.addEventListener('pointermove', function (ev) {
      var rect = svg.getBoundingClientRect();
      var c = nearestPt(ev.clientX - rect.left, ev.clientY - rect.top);
      if (cur && cur !== c) cur.node.classList.remove('is-hover');
      cur = c;
      if (!c) { hideTip(); hit.style.cursor = 'default'; return; }
      c.node.classList.add('is-hover');
      g.appendChild(c.node);
      hit.style.cursor = c.p.href ? 'pointer' : 'default';
      showTip(c.p.label, [{ name: '7-day views', value: fmt(c.p.y, 'int') }, { name: 'Haiku score', value: String(c.p.x) }],
        ev.clientX, ev.clientY);
    });
    hit.addEventListener('pointerleave', function () { if (cur) cur.node.classList.remove('is-hover'); cur = null; hideTip(); });
    hit.addEventListener('click', function () { if (cur && cur.p.href && cur.p.href.indexOf('/dashboard/') === 0) window.location.href = cur.p.href; });
    box.appendChild(svg);
  }

  function render(box) {
    var id = box.getAttribute('data-chart');
    var src = document.getElementById(id + '-data');
    if (!src) return;
    var spec;
    try { spec = JSON.parse(src.textContent); } catch (e) { return; }
    while (box.firstChild) box.removeChild(box.firstChild);
    if (spec.type === 'scatter') renderScatter(box, spec);
    else renderSeries(box, spec);
  }

  function init() {
    var boxes = Array.prototype.slice.call(document.querySelectorAll('.chart[data-chart]'));
    boxes.forEach(render);
    if ('ResizeObserver' in window) {
      var widths = new WeakMap();
      var pending = new Set();
      var ro = new ResizeObserver(function (entries) {
        entries.forEach(function (en) {
          var w = Math.round(en.contentRect.width);
          if (widths.get(en.target) !== w) { widths.set(en.target, w); pending.add(en.target); }
        });
        window.requestAnimationFrame(function () { pending.forEach(render); pending.clear(); });
      });
      boxes.forEach(function (b) { widths.set(b, Math.round(b.clientWidth)); ro.observe(b); });
    }
    window.addEventListener('scroll', hideTip, { passive: true });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
