/* 自製 SVG 圖表模組
 * 不使用任何外部圖表庫（Chart.js / D3 等），確保打包後可完全離線運作，
 * 評審端不需要網路也能看到完整視覺化。
 */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const PALETTE = ["#38bdf8", "#f59e0b", "#34d399", "#f472b6", "#a78bfa",
                   "#fb7185", "#4ade80", "#facc15", "#60a5fa", "#c084fc"];

  function svg(w, h, cls) {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", `0 0 ${w} ${h}`);
    s.setAttribute("preserveAspectRatio", "xMidYMid meet");
    s.setAttribute("class", "kchart " + (cls || ""));
    s.setAttribute("role", "img");
    return s;
  }

  function el(name, attrs, text) {
    const n = document.createElementNS(NS, name);
    for (const k in attrs) {
      if (attrs[k] === null || attrs[k] === undefined) continue;
      n.setAttribute(k, attrs[k]);
    }
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "-";
    const d = digits === undefined ? 0 : digits;
    return Number(v).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const exp = Math.floor(Math.log10(v));
    const base = Math.pow(10, exp);
    const n = v / base;
    let step;
    if (n <= 1) step = 1; else if (n <= 2) step = 2;
    else if (n <= 2.5) step = 2.5; else if (n <= 5) step = 5; else step = 10;
    return step * base;
  }

  function mount(host, node) {
    if (typeof host === "string") host = document.getElementById(host);
    if (!host) return null;
    host.innerHTML = "";
    host.appendChild(node);
    return node;
  }

  /* 提示框（單一實例，跟隨滑鼠） */
  let tipEl = null;
  function tip() {
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "kchart-tip";
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }
  function bindTip(node, html) {
    node.addEventListener("mousemove", (e) => {
      const t = tip();
      t.innerHTML = html;
      t.style.display = "block";
      const pad = 14;
      let x = e.clientX + pad, y = e.clientY + pad;
      const r = t.getBoundingClientRect();
      if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - pad;
      if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - pad;
      t.style.left = x + "px";
      t.style.top = y + "px";
    });
    node.addEventListener("mouseleave", () => { if (tipEl) tipEl.style.display = "none"; });
  }

  /* ------------------------------------------------------------ 直條圖 */
  function bar(host, opt) {
    const data = opt.data || [];
    const W = 760, H = opt.height || 260;
    const m = { t: 18, r: 16, b: opt.rotate ? 66 : 40, l: 52 };
    const s = svg(W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const maxV = niceMax(Math.max(...data.map(d => d.value), 0.0001));
    const g = el("g", { transform: `translate(${m.l},${m.t})` });

    for (let i = 0; i <= 4; i++) {
      const y = ih - (ih * i / 4);
      g.appendChild(el("line", { x1: 0, y1: y, x2: iw, y2: y, class: "grid" }));
      g.appendChild(el("text", { x: -8, y: y + 4, class: "axis end" },
        fmtNum(maxV * i / 4, opt.decimals || 0)));
    }
    const bw = iw / Math.max(1, data.length);
    data.forEach((d, i) => {
      const h = Math.max(0, (d.value / maxV) * ih);
      const x = i * bw + bw * 0.18;
      const w = bw * 0.64;
      const rect = el("rect", {
        x: x, y: ih - h, width: w, height: h, rx: 3,
        fill: d.color || PALETTE[i % PALETTE.length], class: "kbar"
      });
      bindTip(rect, `<b>${d.label}</b><br>${opt.valueLabel || "數值"}：${fmtNum(d.value, opt.decimals || 0)}${opt.unit || ""}` +
        (d.note ? `<br>${d.note}` : ""));
      if (opt.onClick) {
        rect.style.cursor = "pointer";
        rect.addEventListener("click", () => opt.onClick(d));
      }
      g.appendChild(rect);
      if (opt.showValue) {
        g.appendChild(el("text", { x: x + w / 2, y: ih - h - 6, class: "axis mid" },
          fmtNum(d.value, opt.decimals || 0)));
      }
      const lbl = el("text", {
        x: opt.rotate ? x + w / 2 : x + w / 2,
        y: opt.rotate ? ih + 10 : ih + 18,
        class: "axis mid",
        transform: opt.rotate ? `rotate(38 ${x + w / 2} ${ih + 12})` : null
      }, d.label);
      if (opt.rotate) lbl.setAttribute("text-anchor", "start");
      g.appendChild(lbl);
    });
    s.appendChild(g);
    return mount(host, s);
  }

  /* ---------------------------------------------------- 水平橫條（排行） */
  function hbar(host, opt) {
    const data = opt.data || [];
    const rowH = opt.rowH || 26;
    const W = 760, H = Math.max(60, data.length * rowH + 24);
    const labelW = opt.labelW || 250;
    const s = svg(W, H);
    const iw = W - labelW - 90;
    const maxV = Math.max(...data.map(d => Math.abs(d.value)), 0.0001);
    data.forEach((d, i) => {
      const y = 12 + i * rowH;
      const w = Math.max(1, (Math.abs(d.value) / maxV) * iw);
      const g = el("g", {});
      g.appendChild(el("text", { x: labelW - 8, y: y + rowH * 0.62, class: "axis end lbl" },
        d.label.length > 18 ? d.label.slice(0, 17) + "…" : d.label));
      const rect = el("rect", {
        x: labelW, y: y + 4, width: w, height: rowH - 11, rx: 3,
        fill: d.color || PALETTE[0]
      });
      bindTip(rect, `<b>${d.label}</b><br>${opt.valueLabel || "數值"}：${fmtNum(d.value, opt.decimals === undefined ? 1 : opt.decimals)}${opt.unit || ""}` +
        (d.note ? `<br>${d.note}` : ""));
      if (opt.onClick) {
        rect.style.cursor = "pointer";
        rect.addEventListener("click", () => opt.onClick(d));
        g.style.cursor = "pointer";
        g.addEventListener("click", () => opt.onClick(d));
      }
      g.appendChild(rect);
      g.appendChild(el("text", { x: labelW + w + 6, y: y + rowH * 0.62, class: "axis" },
        fmtNum(d.value, opt.decimals === undefined ? 1 : opt.decimals) + (opt.unit || "")));
      s.appendChild(g);
    });
    return mount(host, s);
  }

  /* ------------------------------------------------------------ 折線圖 */
  function line(host, opt) {
    const series = opt.series || [];
    const W = 760, H = opt.height || 260;
    const m = { t: 18, r: 18, b: 42, l: 56 };
    const s = svg(W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const xs = [], ys = [];
    series.forEach(se => se.points.forEach(p => { xs.push(p.x); ys.push(p.y); }));
    const xMin = opt.xMin !== undefined ? opt.xMin : Math.min(...xs, 0);
    const xMax = opt.xMax !== undefined ? opt.xMax : Math.max(...xs, 1);
    const yMin = opt.yMin !== undefined ? opt.yMin : Math.min(...ys, 0);
    const yMax = opt.yMax !== undefined ? opt.yMax : niceMax(Math.max(...ys, 0.0001));
    const sx = v => ((v - xMin) / Math.max(1e-9, xMax - xMin)) * iw;
    const sy = v => ih - ((v - yMin) / Math.max(1e-9, yMax - yMin)) * ih;
    const g = el("g", { transform: `translate(${m.l},${m.t})` });

    for (let i = 0; i <= 4; i++) {
      const y = ih - ih * i / 4;
      g.appendChild(el("line", { x1: 0, y1: y, x2: iw, y2: y, class: "grid" }));
      g.appendChild(el("text", { x: -8, y: y + 4, class: "axis end" },
        (opt.yFormat ? opt.yFormat(yMin + (yMax - yMin) * i / 4) : fmtNum(yMin + (yMax - yMin) * i / 4, opt.decimals || 0))));
    }
    (opt.xTicks || []).forEach(t => {
      g.appendChild(el("text", { x: sx(t.x), y: ih + 18, class: "axis mid" }, t.label));
      g.appendChild(el("line", { x1: sx(t.x), y1: 0, x2: sx(t.x), y2: ih, class: "grid faint" }));
    });
    if (opt.diagonal) {
      g.appendChild(el("line", { x1: sx(xMin), y1: sy(yMin), x2: sx(xMax), y2: sy(yMax), class: "diag" }));
    }
    series.forEach((se, i) => {
      const color = se.color || PALETTE[i % PALETTE.length];
      const d = se.points.map((p, j) => `${j ? "L" : "M"}${sx(p.x).toFixed(2)},${sy(p.y).toFixed(2)}`).join(" ");
      if (se.area) {
        g.appendChild(el("path", {
          d: d + ` L${sx(se.points[se.points.length - 1].x)},${sy(yMin)} L${sx(se.points[0].x)},${sy(yMin)} Z`,
          fill: color, opacity: 0.14, stroke: "none"
        }));
      }
      g.appendChild(el("path", { d: d, fill: "none", stroke: color, "stroke-width": se.width || 2.2, "stroke-linejoin": "round" }));
      if (se.dots !== false) {
        se.points.forEach(p => {
          const c = el("circle", { cx: sx(p.x), cy: sy(p.y), r: 3.4, fill: color });
          bindTip(c, `<b>${se.name}</b><br>${p.label || opt.xLabel || "x"}：${p.xLabel || fmtNum(p.x, 2)}<br>${opt.yLabel || "y"}：${fmtNum(p.y, opt.decimals === undefined ? 2 : opt.decimals)}`);
          g.appendChild(c);
        });
      }
    });
    s.appendChild(g);
    if (series.length > 1 || opt.legend) {
      const lg = el("g", { transform: `translate(${m.l + 8},${m.t + 2})` });
      series.forEach((se, i) => {
        lg.appendChild(el("rect", { x: 0, y: i * 16, width: 10, height: 10, rx: 2, fill: se.color || PALETTE[i % PALETTE.length] }));
        lg.appendChild(el("text", { x: 15, y: i * 16 + 9, class: "axis" }, se.name));
      });
      s.appendChild(lg);
    }
    return mount(host, s);
  }

  /* ------------------------------------------------------------ 環圈圖 */
  function donut(host, opt) {
    const data = (opt.data || []).filter(d => d.value > 0);
    const W = 400, H = opt.height || 240;
    const s = svg(W, H);
    const cx = 118, cy = H / 2, R = Math.min(96, H / 2 - 12), r = R * 0.6;
    const total = data.reduce((a, b) => a + b.value, 0) || 1;
    let ang = -Math.PI / 2;
    data.forEach((d, i) => {
      const a2 = ang + (d.value / total) * Math.PI * 2;
      const large = a2 - ang > Math.PI ? 1 : 0;
      const p = [
        `M${cx + R * Math.cos(ang)},${cy + R * Math.sin(ang)}`,
        `A${R},${R} 0 ${large} 1 ${cx + R * Math.cos(a2)},${cy + R * Math.sin(a2)}`,
        `L${cx + r * Math.cos(a2)},${cy + r * Math.sin(a2)}`,
        `A${r},${r} 0 ${large} 0 ${cx + r * Math.cos(ang)},${cy + r * Math.sin(ang)}`, "Z"
      ].join(" ");
      const path = el("path", { d: p, fill: d.color || PALETTE[i % PALETTE.length], class: "kslice" });
      bindTip(path, `<b>${d.label}</b><br>${fmtNum(d.value)} 所（${(d.value / total * 100).toFixed(1)}%）` + (d.note ? `<br>${d.note}` : ""));
      if (opt.onClick) { path.style.cursor = "pointer"; path.addEventListener("click", () => opt.onClick(d)); }
      s.appendChild(path);
      ang = a2;
    });
    s.appendChild(el("text", { x: cx, y: cy - 4, class: "big mid" }, fmtNum(opt.centerValue !== undefined ? opt.centerValue : total)));
    s.appendChild(el("text", { x: cx, y: cy + 16, class: "axis mid" }, opt.centerLabel || "總計"));
    const lg = el("g", { transform: `translate(238,${Math.max(14, cy - data.length * 11)})` });
    data.forEach((d, i) => {
      lg.appendChild(el("rect", { x: 0, y: i * 22, width: 11, height: 11, rx: 2, fill: d.color || PALETTE[i % PALETTE.length] }));
      lg.appendChild(el("text", { x: 17, y: i * 22 + 10, class: "axis" },
        `${d.label} ${fmtNum(d.value)}（${(d.value / total * 100).toFixed(1)}%）`));
    });
    s.appendChild(lg);
    return mount(host, s);
  }

  /* ------------------------------------------------------------ 雷達圖 */
  function radar(host, opt) {
    const axes = opt.axes || [];
    const W = 420, H = opt.height || 300;
    const s = svg(W, H);
    const cx = W / 2, cy = H / 2 + 4, R = Math.min(W, H) / 2 - 52;
    const n = axes.length;
    const ang = i => -Math.PI / 2 + i * 2 * Math.PI / n;
    for (let ring = 1; ring <= 4; ring++) {
      const rr = R * ring / 4;
      const pts = axes.map((_, i) => `${cx + rr * Math.cos(ang(i))},${cy + rr * Math.sin(ang(i))}`).join(" ");
      s.appendChild(el("polygon", { points: pts, class: "grid-poly" }));
    }
    axes.forEach((a, i) => {
      s.appendChild(el("line", { x1: cx, y1: cy, x2: cx + R * Math.cos(ang(i)), y2: cy + R * Math.sin(ang(i)), class: "grid" }));
      const lx = cx + (R + 22) * Math.cos(ang(i));
      const ly = cy + (R + 22) * Math.sin(ang(i));
      const t = el("text", { x: lx, y: ly + 4, class: "axis mid" }, a);
      s.appendChild(t);
    });
    (opt.series || []).forEach((se, k) => {
      const color = se.color || PALETTE[k % PALETTE.length];
      const pts = se.values.map((v, i) => {
        const rr = R * Math.max(0, Math.min(100, v)) / 100;
        return `${cx + rr * Math.cos(ang(i))},${cy + rr * Math.sin(ang(i))}`;
      }).join(" ");
      s.appendChild(el("polygon", { points: pts, fill: color, "fill-opacity": 0.22, stroke: color, "stroke-width": 2 }));
      se.values.forEach((v, i) => {
        const rr = R * Math.max(0, Math.min(100, v)) / 100;
        const c = el("circle", { cx: cx + rr * Math.cos(ang(i)), cy: cy + rr * Math.sin(ang(i)), r: 3.6, fill: color });
        bindTip(c, `<b>${axes[i]}</b><br>${se.name}：${fmtNum(v, 1)}`);
        s.appendChild(c);
      });
    });
    return mount(host, s);
  }

  /* ------------------------------------------------------------ 散布圖 */
  function scatter(host, opt) {
    const pts = (opt.points || []).filter(p => p.x !== null && p.y !== null && !isNaN(p.x) && !isNaN(p.y));
    const W = 760, H = opt.height || 320;
    const m = { t: 18, r: 18, b: 46, l: 62 };
    const s = svg(W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const xs = pts.map(p => p.x), ys = pts.map(p => p.y);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const yMin = Math.min(...ys), yMax = Math.max(...ys);
    const px = (xMax - xMin) * 0.06 || 0.01, py = (yMax - yMin) * 0.06 || 0.01;
    const sx = v => ((v - (xMin - px)) / ((xMax + px) - (xMin - px))) * iw;
    const sy = v => ih - ((v - (yMin - py)) / ((yMax + py) - (yMin - py))) * ih;
    const g = el("g", { transform: `translate(${m.l},${m.t})` });
    for (let i = 0; i <= 4; i++) {
      const y = ih - ih * i / 4;
      g.appendChild(el("line", { x1: 0, y1: y, x2: iw, y2: y, class: "grid" }));
      const val = (yMin - py) + ((yMax + py) - (yMin - py)) * i / 4;
      g.appendChild(el("text", { x: -8, y: y + 4, class: "axis end" }, opt.yFormat ? opt.yFormat(val) : fmtNum(val, 2)));
      const x = iw * i / 4;
      g.appendChild(el("line", { x1: x, y1: 0, x2: x, y2: ih, class: "grid faint" }));
      const xv = (xMin - px) + ((xMax + px) - (xMin - px)) * i / 4;
      g.appendChild(el("text", { x: x, y: ih + 18, class: "axis mid" }, opt.xFormat ? opt.xFormat(xv) : fmtNum(xv, 2)));
    }
    if (opt.xRef !== undefined) g.appendChild(el("line", { x1: sx(opt.xRef), y1: 0, x2: sx(opt.xRef), y2: ih, class: "ref" }));
    if (opt.yRef !== undefined) g.appendChild(el("line", { x1: 0, y1: sy(opt.yRef), x2: iw, y2: sy(opt.yRef), class: "ref" }));
    pts.forEach(p => {
      const c = el("circle", {
        cx: sx(p.x), cy: sy(p.y), r: p.size || 4,
        fill: p.color || PALETTE[0], "fill-opacity": p.opacity === undefined ? 0.75 : p.opacity,
        stroke: p.stroke || "none", "stroke-width": 1
      });
      bindTip(c, `<b>${p.label || ""}</b><br>${opt.xLabel || "x"}：${opt.xFormat ? opt.xFormat(p.x) : fmtNum(p.x, 3)}<br>${opt.yLabel || "y"}：${opt.yFormat ? opt.yFormat(p.y) : fmtNum(p.y, 3)}` + (p.note ? `<br>${p.note}` : ""));
      if (opt.onClick) { c.style.cursor = "pointer"; c.addEventListener("click", () => opt.onClick(p)); }
      g.appendChild(c);
    });
    g.appendChild(el("text", { x: iw / 2, y: ih + 36, class: "axis mid" }, opt.xLabel || ""));
    s.appendChild(g);
    s.appendChild(el("text", { x: 14, y: m.t + ih / 2, class: "axis mid", transform: `rotate(-90 14 ${m.t + ih / 2})` }, opt.yLabel || ""));
    return mount(host, s);
  }

  /* ------------------------------------------------------------ 箱形圖 */
  function box(host, opt) {
    const groups = opt.groups || [];
    const W = 760, H = opt.height || 260;
    const m = { t: 18, r: 18, b: 40, l: 62 };
    const s = svg(W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    let lo = Infinity, hi = -Infinity;
    groups.forEach(gp => { lo = Math.min(lo, gp.min); hi = Math.max(hi, gp.max); });
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const pad = (hi - lo) * 0.1 || 0.05;
    lo -= pad; hi += pad;
    const sy = v => ih - ((v - lo) / Math.max(1e-9, hi - lo)) * ih;
    const g = el("g", { transform: `translate(${m.l},${m.t})` });
    for (let i = 0; i <= 4; i++) {
      const y = ih - ih * i / 4;
      g.appendChild(el("line", { x1: 0, y1: y, x2: iw, y2: y, class: "grid" }));
      g.appendChild(el("text", { x: -8, y: y + 4, class: "axis end" },
        opt.yFormat ? opt.yFormat(lo + (hi - lo) * i / 4) : fmtNum(lo + (hi - lo) * i / 4, 2)));
    }
    const bw = iw / Math.max(1, groups.length);
    groups.forEach((gp, i) => {
      const cx = i * bw + bw / 2;
      const w = Math.min(72, bw * 0.44);
      const color = gp.color || PALETTE[i % PALETTE.length];
      g.appendChild(el("line", { x1: cx, y1: sy(gp.min), x2: cx, y2: sy(gp.max), stroke: color, "stroke-width": 1.4 }));
      g.appendChild(el("line", { x1: cx - w / 4, y1: sy(gp.min), x2: cx + w / 4, y2: sy(gp.min), stroke: color, "stroke-width": 1.4 }));
      g.appendChild(el("line", { x1: cx - w / 4, y1: sy(gp.max), x2: cx + w / 4, y2: sy(gp.max), stroke: color, "stroke-width": 1.4 }));
      const r = el("rect", {
        x: cx - w / 2, y: sy(gp.q3), width: w, height: Math.max(2, sy(gp.q1) - sy(gp.q3)),
        rx: 3, fill: color, "fill-opacity": 0.28, stroke: color, "stroke-width": 1.4
      });
      bindTip(r, `<b>${gp.group}</b>（n=${gp.n}）<br>` +
        `上緣：${opt.yFormat ? opt.yFormat(gp.max) : fmtNum(gp.max, 3)}<br>` +
        `Q3：${opt.yFormat ? opt.yFormat(gp.q3) : fmtNum(gp.q3, 3)}<br>` +
        `中位數：${opt.yFormat ? opt.yFormat(gp.median) : fmtNum(gp.median, 3)}<br>` +
        `Q1：${opt.yFormat ? opt.yFormat(gp.q1) : fmtNum(gp.q1, 3)}<br>` +
        `下緣：${opt.yFormat ? opt.yFormat(gp.min) : fmtNum(gp.min, 3)}`);
      g.appendChild(r);
      g.appendChild(el("line", { x1: cx - w / 2, y1: sy(gp.median), x2: cx + w / 2, y2: sy(gp.median), stroke: color, "stroke-width": 2.6 }));
      if (gp.mark !== undefined && gp.mark !== null) {
        g.appendChild(el("circle", { cx: cx, cy: sy(gp.mark), r: 5, fill: "#fff", stroke: "#b3261e", "stroke-width": 2.4 }));
      }
      g.appendChild(el("text", { x: cx, y: ih + 18, class: "axis mid" }, gp.group));
    });
    s.appendChild(g);
    return mount(host, s);
  }

  /* -------------------------------------------------- 首位數／尾數分布 */
  function digitBars(host, opt) {
    const obs = opt.observed || [], exp = opt.expected || [];
    const labels = opt.labels || obs.map((_, i) => String(i + 1));
    const W = 760, H = opt.height || 250;
    const m = { t: 22, r: 16, b: 40, l: 48 };
    const s = svg(W, H);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const maxV = niceMax(Math.max(...obs, ...exp, 0.0001));
    const g = el("g", { transform: `translate(${m.l},${m.t})` });
    for (let i = 0; i <= 4; i++) {
      const y = ih - ih * i / 4;
      g.appendChild(el("line", { x1: 0, y1: y, x2: iw, y2: y, class: "grid" }));
      g.appendChild(el("text", { x: -8, y: y + 4, class: "axis end" },
        fmtNum(maxV * i / 4, 1) + "%"));
    }
    const bw = iw / obs.length;
    obs.forEach((v, i) => {
      const h = (v / maxV) * ih;
      const x = i * bw + bw * 0.22, w = bw * 0.56;
      const rect = el("rect", { x: x, y: ih - h, width: w, height: Math.max(0, h), rx: 2, fill: opt.color || "#38bdf8" });
      bindTip(rect, `<b>${labels[i]}</b><br>實際：${fmtNum(v, 2)}%<br>期望：${fmtNum(exp[i], 2)}%`);
      g.appendChild(rect);
      g.appendChild(el("text", { x: x + w / 2, y: ih + 18, class: "axis mid" }, labels[i]));
    });
    if (exp.length) {
      const d = exp.map((v, i) => `${i ? "L" : "M"}${(i * bw + bw / 2).toFixed(1)},${(ih - (v / maxV) * ih).toFixed(1)}`).join(" ");
      g.appendChild(el("path", { d: d, fill: "none", stroke: "#f59e0b", "stroke-width": 2.2, "stroke-dasharray": "5 3" }));
      exp.forEach((v, i) => g.appendChild(el("circle", { cx: i * bw + bw / 2, cy: ih - (v / maxV) * ih, r: 3, fill: "#f59e0b" })));
    }
    s.appendChild(g);
    const lg = el("g", { transform: `translate(${m.l + 6},6)` });
    lg.appendChild(el("rect", { x: 0, y: 0, width: 10, height: 10, rx: 2, fill: opt.color || "#38bdf8" }));
    lg.appendChild(el("text", { x: 15, y: 9, class: "axis" }, opt.obsLabel || "實際分布"));
    lg.appendChild(el("line", { x1: 96, y1: 5, x2: 118, y2: 5, stroke: "#f59e0b", "stroke-width": 2.2, "stroke-dasharray": "5 3" }));
    lg.appendChild(el("text", { x: 124, y: 9, class: "axis" }, opt.expLabel || "期望分布"));
    s.appendChild(lg);
    return mount(host, s);
  }

  /* ------------------------------------------------------ 貢獻度瀑布圖 */
  function waterfall(host, opt) {
    const items = opt.items || [];
    const rowH = 28;
    const W = 760, H = Math.max(60, items.length * rowH + 26);
    const labelW = opt.labelW || 230;
    const s = svg(W, H);
    const iw = W - labelW - 110;
    const maxV = Math.max(...items.map(d => Math.abs(d.value)), 0.0001);
    const zero = labelW + iw / 2;
    s.appendChild(el("line", { x1: zero, y1: 8, x2: zero, y2: H - 12, class: "grid" }));
    items.forEach((d, i) => {
      const y = 12 + i * rowH;
      const w = (Math.abs(d.value) / maxV) * (iw / 2);
      const pos = d.value >= 0;
      const rect = el("rect", {
        x: pos ? zero : zero - w, y: y + 4, width: Math.max(1, w), height: rowH - 12, rx: 3,
        fill: pos ? "#e8590c" : "#2f9e44"
      });
      bindTip(rect, `<b>${d.label}</b><br>貢獻：${d.value >= 0 ? "+" : ""}${fmtNum(d.value, 3)}` + (d.note ? `<br>${d.note}` : ""));
      s.appendChild(rect);
      s.appendChild(el("text", { x: labelW - 8, y: y + rowH * 0.6, class: "axis end lbl" },
        d.label.length > 16 ? d.label.slice(0, 15) + "…" : d.label));
      s.appendChild(el("text", {
        x: pos ? zero + w + 6 : zero - w - 6, y: y + rowH * 0.6,
        class: "axis" + (pos ? "" : " end")
      }, (d.value >= 0 ? "+" : "") + fmtNum(d.value, 3)));
    });
    return mount(host, s);
  }

  /* -------------------------------------------------------- 熱力方格圖 */
  function heat(host, opt) {
    const cells = opt.cells || [];
    const cols = opt.cols || 6;
    const cw = 122, ch = 54, gap = 6;
    const rows = Math.ceil(cells.length / cols);
    const W = cols * (cw + gap), H = rows * (ch + gap) + 6;
    const s = svg(W, H);
    const maxV = Math.max(...cells.map(c => c.value), 0.0001);
    cells.forEach((c, i) => {
      const x = (i % cols) * (cw + gap), y = Math.floor(i / cols) * (ch + gap);
      const t = Math.max(0, Math.min(1, c.value / maxV));
      const g = el("g", {});
      const rect = el("rect", {
        x: x, y: y, width: cw, height: ch, rx: 6,
        fill: opt.colorOf ? opt.colorOf(c) : `rgba(56,189,248,${0.12 + t * 0.72})`
      });
      bindTip(rect, `<b>${c.label}</b><br>${opt.valueLabel || "平均風險"}：${fmtNum(c.value, 1)}<br>${c.note || ""}`);
      if (opt.onClick) { rect.style.cursor = "pointer"; rect.addEventListener("click", () => opt.onClick(c)); }
      g.appendChild(rect);
      g.appendChild(el("text", { x: x + 10, y: y + 21, class: "cell-lbl" }, c.label));
      g.appendChild(el("text", { x: x + 10, y: y + 42, class: "cell-val" }, fmtNum(c.value, 1) + (opt.unit || "")));
      if (c.badge) g.appendChild(el("text", { x: x + cw - 10, y: y + 42, class: "cell-badge end" }, c.badge));
      s.appendChild(g);
    });
    return mount(host, s);
  }

  /* ---------------------------------------------------------- 詞雲 (HTML) */
  function wordcloud(host, opt) {
    if (typeof host === "string") host = document.getElementById(host);
    if (!host) return;
    const words = opt.words || [];
    const max = Math.max(...words.map(w => w.count), 1);
    const min = Math.min(...words.map(w => w.count), 0);
    host.className = "wordcloud";
    host.innerHTML = words.map(w => {
      const t = (w.count - min) / Math.max(1, max - min);
      const size = 13 + t * 22;
      const weight = t > 0.6 ? 700 : t > 0.3 ? 600 : 500;
      const opacity = 0.62 + t * 0.38;
      const cls = w.source === "新詞發現" ? "wc-new" : "wc-lex";
      return `<span class="wc ${cls}" style="font-size:${size.toFixed(1)}px;font-weight:${weight};opacity:${opacity.toFixed(2)}" title="${w.topic || ""}｜出現 ${w.count} 次">${w.word}</span>`;
    }).join("");
  }

  /* ------------------------------------------------------- 時間軸 (HTML) */
  function timeline(host, opt) {
    if (typeof host === "string") host = document.getElementById(host);
    if (!host) return;
    const items = opt.items || [];
    if (!items.length) { host.innerHTML = '<div class="empty">無紀錄</div>'; return; }
    host.className = "timeline";
    host.innerHTML = items.map(it => `
      <div class="tl-item ${it.level || ""}">
        <div class="tl-dot"></div>
        <div class="tl-body">
          <div class="tl-head"><span class="tl-date">${it.date || ""}</span>
            <span class="tl-title">${it.title || ""}</span>
            ${it.tag ? `<span class="tag ${it.tagClass || ""}">${it.tag}</span>` : ""}</div>
          ${it.text ? `<div class="tl-text">${it.text}</div>` : ""}
        </div>
      </div>`).join("");
  }

  global.KChart = {
    bar, hbar, line, donut, radar, scatter, box, digitBars, waterfall, heat,
    wordcloud, timeline, PALETTE, fmtNum
  };
})(window);
