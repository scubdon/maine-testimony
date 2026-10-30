/* Charts for the findings page. All numbers come from data/site.json, built by
   scripts/build_site_data.py from the project's local database (aggregates only). */
(async function () {
  "use strict";

  const D = await fetch("data/site.json").then((r) => r.json());
  const root = document.documentElement;
  const cssVar = (n) => getComputedStyle(root).getPropertyValue(n).trim();

  const fmt = d3.format(",");
  const pct0 = d3.format(".0%");
  const pct1 = d3.format(".1%");
  const compact = (v) => (v >= 1000 ? d3.format(".3~s")(v).replace("k", "K") : fmt(v));
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const shorten = (s, n) => (s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s);

  /* ---------------- headline numbers in the prose ---------------- */
  const T = D.totals;
  const S = D.sessions;
  const sFirst = S[0];
  const sLast = S[S.length - 1];
  const cat = T.categories;
  const vol500 = D.volume.find((v) => v.bucket === "500+");
  const rep = Object.fromEntries(D.repeat.map((r) => [r.times, r]));
  const O = D.outcomes;
  const opp = O.by_opposition;
  const counties = D.places.counties.slice().sort((a, b) => b.per_1k - a.per_1k);
  const mma = D.organizations.find((o) => o.name === "Maine Municipal Association");
  const pts = (v) => `${Math.round(Math.abs(v) * 100)} points`;
  const K = {
    span: "2013–2026",
    submissions: fmt(T.submissions),
    bills: fmt(T.bills),
    hearings: fmt(T.hearings),
    orgs: fmt(T.organizations),
    towns: fmt(T.towns),
    s_first_label: sFirst.label,
    s_last_label: sLast.label,
    s_first_n: fmt(sFirst.submissions),
    s_last_n: fmt(sLast.submissions),
    med_first: sFirst.median_per_bill,
    med_last: sLast.median_per_bill,
    top1_first: pct0(sFirst.top1pct_share),
    top1_last: pct0(sLast.top1pct_share),
    vol_500_oppose: pct0(vol500.oppose),
    vol_500_bills: vol500.bills,
    rep_11_people: pct1(rep["11-25"].people_share + rep["26+"].people_share),
    rep_11_share: pct0(rep["11-25"].testimony_share + rep["26+"].testimony_share),
    rep_once: pct0(rep["1"].people_share),
    rep_26: `${fmt(rep["26+"].people)} people`,
    rep_26_share: pct0(rep["26+"].testimony_share),
    share_ind: pct0((cat.town + (cat.place_other || 0)) / T.submissions),
    share_org: pct0(cat.organization / T.submissions),
    share_gov: pct0(cat.government / T.submissions),
    share_leg: pct0(cat.legislator / T.submissions),
    camp_letters: fmt(D.campaigns.letters),
    camp_groups: fmt(D.campaigns.groups),
    camp_max: fmt(D.campaigns.largest[0].size),
    mma_bills: mma ? fmt(mma.bills) : "—",
    county_top: counties[0].county,
    county_bottom: counties[counties.length - 1].county,
    county_ratio: (counties[0].per_1k / counties[counties.length - 1].per_1k).toFixed(1),
    mapped_ind: fmt(d3.sum(D.places.counties, (c) => c.n)),
    pass_all: pct0(O.pass_rate_all),
    closed_pos: fmt(O.closed_positioned),
    closed5: fmt(O.closed_5_voices),
    pass_lo: pct0(opp[0].passed),
    pass_hi: pct0(opp[opp.length - 1].passed),
    vs_lo: pts(opp[0].vs_cell),
    vs_hi: pts(opp[opp.length - 1].vs_cell),
    maj_pass: pct0(O.party_totals.majority.passed),
    min_pass: pct0(O.party_totals.minority.passed),
    maj_opp: pct0(O.party_totals.majority.oppose_share),
    min_opp: pct0(O.party_totals.minority.oppose_share),
    acc_sub: pct1(D.accuracy.submitters.accuracy),
    acc_pos: pct1(D.accuracy.positions[1].accuracy),
    acc_pos_rx: pct1(D.accuracy.positions[0].accuracy),
    acc_prec: pct1(D.accuracy.positions[0].precision),
    generated: D.generated,
  };
  document.querySelectorAll("[data-k]").forEach((el) => {
    const v = K[el.dataset.k];
    if (v !== undefined) el.textContent = v;
  });

  /* ---------------- shared pieces ---------------- */
  const GROUPS = [
    { key: "Individuals", color: "--g-ind" },
    { key: "Organizations", color: "--g-org" },
    { key: "Government", color: "--g-gov" },
    { key: "Legislators", color: "--g-leg" },
    { key: "Unlabeled", color: "--g-other" },
  ];
  const POS = [
    { key: "support", label: "Support", color: "--support" },
    { key: "neither", label: "Neither for nor against", color: "--neither" },
    { key: "oppose", label: "Oppose", color: "--oppose" },
    { key: "unknown", label: "Unclear", color: "--unknown" },
  ];
  const BIN_LABELS = { "<10%": "<10%", "10-25%": "10–25%", "25-50%": "25–50%", "50-75%": "50–75%", "75-90%": "75–90%", "90%+": "90%+" };

  const LEGENDS = {
    groups: GROUPS.map((g) => [g.key, g.color]),
    positions: POS.map((p) => [p.label, p.color]),
    positions3: POS.slice(0, 3).map((p) => [p.label, p.color]),
    positions2: [["Support", "--support"], ["Oppose", "--oppose"]],
    repeat: [["Share of people", "--minority"], ["Share of individuals' letters", "--g-ind"]],
    outcome: [["Became law", "--law", true], ["Committee recommended against", "--ontp", true]],
    party: [["Majority-party sponsor", "--majority", true], ["Minority-party sponsor", "--minority", true]],
    passed: [["Became law", "--law"], ["Did not", "--neither"]],
  };
  document.querySelectorAll("[data-legend]").forEach((el) => {
    const name = el.dataset.legend;
    if (name === "agree") {
      el.innerHTML = `<span>Opposite sides</span><span style="display:inline-block;width:140px;height:10px;border-radius:3px;background:linear-gradient(90deg,var(--oppose),var(--neither),var(--support))"></span><span>Same side</span>`;
      return;
    }
    el.innerHTML = (LEGENDS[name] || [])
      .map(([label, color, line]) => `<span><i class="${line ? "line" : ""}" style="background:var(${color})"></i>${esc(label)}</span>`)
      .join("");
  });

  /* tooltip */
  const tip = document.getElementById("tip");
  function showTip(html, ev) {
    tip.innerHTML = html;
    tip.style.opacity = 1;
    const pad = 14;
    const r = tip.getBoundingClientRect();
    let x = ev.pageX + pad;
    let y = ev.pageY + pad;
    if (x + r.width > window.scrollX + document.documentElement.clientWidth - 8) x = ev.pageX - r.width - pad;
    if (x < window.scrollX + 8) x = window.scrollX + 8;
    if (y + r.height > window.scrollY + window.innerHeight - 8) y = ev.pageY - r.height - pad;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  const hideTip = () => (tip.style.opacity = 0);
  const row = (label, value, color) =>
    `<div class="row"><span>${color ? `<i class="sw" style="background:var(${color})"></i>` : ""}${esc(label)}</span><b>${esc(value)}</b></div>`;

  function svgFor(el, w, h) {
    const plot = el.querySelector(".plot") || el;
    plot.innerHTML = "";
    return d3.select(plot).append("svg").attr("viewBox", `0 0 ${w} ${h}`).attr("width", "100%").attr("role", "img");
  }
  const widthOf = (el) => Math.max(280, (el.querySelector(".plot") || el).clientWidth);

  // A bar whose data end is rounded (4px) and whose baseline end is square.
  function barPath(x, y, w, h, dir = "up", r = 4) {
    r = Math.min(r, w / 2, h / 2);
    if (h <= 0 || w <= 0) return "";
    if (dir === "up")
      return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
    // "right": baseline on the left
    return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
  }

  function yGrid(g, y, w, ticks, format) {
    const ax = g.append("g").attr("class", "axis gridline").call(d3.axisLeft(y).ticks(ticks).tickSize(-w).tickFormat(format));
    ax.select(".domain").remove();
    ax.selectAll("text").attr("x", -6);
    return ax;
  }

  /* ---------------- generic: column chart ---------------- */
  function columns(el, { cats, values, color, yFmt = compact, label = yFmt, tipFn, height = 240, yMax }) {
    const w = widthOf(el);
    const m = { t: 22, r: 8, b: 30, l: 40 };
    const svg = svgFor(el, w, height);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const ih = height - m.t - m.b;
    const x = d3.scaleBand().domain(cats).range([0, iw]).padding(0.3);
    const y = d3.scaleLinear().domain([0, yMax ?? d3.max(values) * 1.08]).nice().range([ih, 0]);
    yGrid(g, y, iw, 4, yFmt);
    g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).tickSize(0).tickPadding(8)).select(".domain").attr("stroke", "var(--axis)");
    const bw = Math.min(28, x.bandwidth());
    const bars = g.selectAll("path.bar").data(cats).join("path").attr("class", "bar")
      .attr("d", (c, i) => barPath(x(c) + (x.bandwidth() - bw) / 2, y(values[i]), bw, ih - y(values[i])))
      .style("fill", `var(${color})`);
    g.selectAll("text.cap").data(cats).join("text").attr("class", "lbl")
      .attr("x", (c) => x(c) + x.bandwidth() / 2).attr("y", (c, i) => y(values[i]) - 6)
      .attr("text-anchor", "middle").text((c, i) => (values[i] == null ? "" : label(values[i])));
    if (tipFn)
      g.selectAll("rect.hit").data(cats).join("rect").attr("x", (c) => x(c)).attr("width", x.bandwidth())
        .attr("y", 0).attr("height", ih).attr("fill", "transparent")
        .on("pointermove", (ev, c) => showTip(tipFn(cats.indexOf(c)), ev)).on("pointerleave", hideTip);
    return bars;
  }

  /* ---------------- generic: lines over ordered bins ---------------- */
  function lines(el, { cats, series, height = 260, yMax = 1, tipFn }) {
    const w = widthOf(el);
    const m = { t: 16, r: 46, b: 44, l: 40 };
    const svg = svgFor(el, w, height);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const ih = height - m.t - m.b;
    const x = d3.scalePoint().domain(cats).range([0, iw]).padding(0.3);
    const y = d3.scaleLinear().domain([0, yMax]).range([ih, 0]);
    yGrid(g, y, iw, 4, pct0);
    g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(x).tickSize(0).tickPadding(8).tickFormat((c) => (iw < 420 ? (BIN_LABELS[c] || c).replace("%", "") : BIN_LABELS[c] || c))).select(".domain").attr("stroke", "var(--axis)");
    g.append("text").attr("class", "lbl-muted").attr("x", iw / 2).attr("y", ih + 38).attr("text-anchor", "middle").text(iw < 420 ? "Share of public testimony opposed (%) →" : "Share of public testimony opposed →");
    const line = d3.line().defined((d) => d != null).x((d, i) => x(cats[i])).y((d) => y(d));
    for (const s of series) {
      g.append("path").attr("d", line(s.values)).style("fill", "none").style("stroke", `var(${s.color})`).attr("stroke-width", 2).attr("stroke-linejoin", "round").attr("stroke-linecap", "round");
      g.selectAll(null).data(s.values).join("circle").attr("cx", (d, i) => x(cats[i])).attr("cy", (d) => y(d ?? 0)).attr("r", 4.5)
        .style("fill", `var(${s.color})`).style("stroke", "var(--surface)").attr("stroke-width", 2).attr("display", (d) => (d == null ? "none" : null));
      // Label the two ends: the last point to its right, the first above (or, for the
      // lower series, below) its dot so it stays clear of the y-axis labels.
      const last = s.values.length - 1;
      const lowest = series.every((o) => o === s || (o.values[0] ?? 0) >= (s.values[0] ?? 0));
      if (s.values[0] != null)
        g.append("text").attr("class", "lbl-strong").attr("x", x(cats[0])).attr("y", y(s.values[0]) + (lowest && series.length > 1 ? 20 : -10))
          .attr("text-anchor", "middle").text(pct0(s.values[0]));
      if (s.values[last] != null)
        g.append("text").attr("class", "lbl-strong").attr("x", x(cats[last]) + 8).attr("y", y(s.values[last]) + 4).text(pct0(s.values[last]));
    }
    if (tipFn) {
      const step = x.step();
      g.selectAll("rect.hit").data(cats).join("rect").attr("x", (c) => x(c) - step / 2).attr("width", step).attr("y", 0).attr("height", ih).attr("fill", "transparent")
        .on("pointermove", (ev, c) => showTip(tipFn(cats.indexOf(c)), ev)).on("pointerleave", hideTip);
    }
  }

  /* ---------------- generic: 100% stacked horizontal bars ---------------- */
  function stacked100(el, { rows, keys, labelOf, tipFn, labelW = 220 }) {
    const w = widthOf(el);
    const narrow = w < 560;
    const lw = narrow ? 0 : labelW;
    const rowH = narrow ? 40 : 28;
    const barH = 16;
    const m = { t: 4, r: 4, b: 22, l: lw };
    const h = m.t + m.b + rows.length * rowH;
    const svg = svgFor(el, w, h);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const x = d3.scaleLinear().domain([0, 1]).range([0, iw]);
    g.append("g").attr("class", "axis gridline").attr("transform", `translate(0,${rows.length * rowH})`)
      .call(d3.axisBottom(x).ticks(4).tickFormat(pct0).tickSize(-rows.length * rowH)).select(".domain").remove();
    rows.forEach((r, i) => {
      const total = d3.sum(keys, (k) => r[k.key] || 0);
      const top = i * rowH + (narrow ? 18 : (rowH - barH) / 2);
      const label = labelOf(r);
      if (narrow) g.append("text").attr("class", "lbl").attr("x", 0).attr("y", i * rowH + 13).text(shorten(label, 48));
      else g.append("text").attr("class", "lbl").attr("x", -10).attr("y", top + barH / 2 + 4).attr("text-anchor", "end").text(shorten(label, 34));
      let acc = 0;
      keys.forEach((k, j) => {
        const v = (r[k.key] || 0) / total;
        const x0 = x(acc);
        const width = Math.max(0, x(acc + v) - x0 - (j < keys.length - 1 ? 2 : 0));
        acc += v;
        if (width <= 0) return;
        g.append("rect").attr("x", x0).attr("y", top).attr("width", width).attr("height", barH).attr("rx", 2).style("fill", `var(${k.color})`);
        if (width > 36 && (k.key === "support" || k.key === "oppose"))
          g.append("text").attr("x", x0 + (k.key === "support" ? 6 : width - 6)).attr("y", top + barH / 2 + 4)
            .attr("text-anchor", k.key === "support" ? "start" : "end").style("fill", "#fff").style("font", "600 11px var(--sans)").text(pct0(v));
      });
      g.append("rect").attr("x", -lw).attr("y", i * rowH).attr("width", iw + lw).attr("height", rowH).attr("fill", "transparent")
        .on("pointermove", (ev) => showTip(tipFn(r), ev)).on("pointerleave", hideTip);
    });
  }
  const posTip = (title, r, extra = "") => {
    const total = d3.sum(POS, (p) => r[p.key] || 0);
    return `<b>${esc(title)}</b>${extra}` + POS.filter((p) => r[p.key] != null).map((p) => row(p.label, pct0((r[p.key] || 0) / total), p.color)).join("");
  };

  /* ================= charts ================= */
  const charts = {};

  charts["fig-sessions"] = (el) => {
    const w = widthOf(el);
    const h = 300;
    const m = { t: 22, r: 8, b: 40, l: 44 };
    const svg = svgFor(el, w, h);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const ih = h - m.t - m.b;
    const x = d3.scaleBand().domain(S.map((s) => s.label)).range([0, iw]).padding(0.32);
    const y = d3.scaleLinear().domain([0, d3.max(S, (s) => s.submissions) * 1.06]).nice().range([ih, 0]);
    yGrid(g, y, iw, 5, compact);
    const xa = g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).tickSize(0).tickPadding(8));
    xa.select(".domain").attr("stroke", "var(--axis)");
    if (x.bandwidth() >= 44) xa.selectAll(".tick").append("text").attr("y", 26).attr("fill", "var(--muted)").style("font-size", "11px")
      .text((l) => (S.find((s) => s.label === l).years || "").replace(/^20(\d\d)-20(\d\d)$/, "’$1–’$2"));
    const bw = Math.min(44, x.bandwidth());
    S.forEach((s) => {
      let acc = 0;
      const x0 = x(s.label) + (x.bandwidth() - bw) / 2;
      const present = GROUPS.filter((gr) => (s.by_group[gr.key] || 0) > 0);
      present.forEach((gr, j) => {
        const v = s.by_group[gr.key];
        const y0 = y(acc);
        const y1 = y(acc + v);
        acc += v;
        const isTop = j === present.length - 1;
        const hgt = Math.max(0, y0 - y1 - (j > 0 ? 2 : 0));
        g.append("path").attr("d", isTop ? barPath(x0, y1, bw, hgt) : `M${x0},${y1}h${bw}v${hgt}h${-bw}Z`).style("fill", `var(${gr.color})`)
          .on("pointermove", (ev) =>
            showTip(`<b>${s.label} Legislature</b> (${esc(s.years)})` + row(gr.key, fmt(v), gr.color) + row("Share of session", pct0(v / s.submissions)) + row("All submissions", fmt(s.submissions)) + row("Hearing days", fmt(s.hearings)), ev))
          .on("pointerleave", hideTip);
      });
      g.append("text").attr("class", "lbl-strong").attr("x", x0 + bw / 2).attr("y", y(s.submissions) - 6).attr("text-anchor", "middle").text(compact(s.submissions));
    });
  };

  charts["fig-monthly"] = (el) => {
    const w = widthOf(el);
    const h = 220;
    const m = { t: 10, r: 10, b: 26, l: 44 };
    const svg = svgFor(el, w, h);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const ih = h - m.t - m.b;
    const pts = D.monthly.map((d) => ({ t: d3.timeParse("%Y-%m")(d.month), n: d.n }));
    const x = d3.scaleTime().domain(d3.extent(pts, (d) => d.t)).range([0, iw]);
    const y = d3.scaleLinear().domain([0, d3.max(pts, (d) => d.n)]).nice().range([ih, 0]);
    yGrid(g, y, iw, 4, compact);
    g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`)
      .call(d3.axisBottom(x).ticks(d3.timeYear.every(w < 600 ? 2 : 1)).tickFormat(d3.timeFormat("%Y")).tickSizeOuter(0)).select(".domain").attr("stroke", "var(--axis)");
    g.append("path").datum(pts).attr("d", d3.area().x((d) => x(d.t)).y0(ih).y1((d) => y(d.n)).curve(d3.curveStepAfter)).style("fill", "var(--g-ind)").style("opacity", 0.12);
    g.append("path").datum(pts).attr("d", d3.line().x((d) => x(d.t)).y((d) => y(d.n)).curve(d3.curveStepAfter)).style("fill", "none").style("stroke", "var(--g-ind)").attr("stroke-width", 1.5);
    const peak = pts.reduce((a, b) => (b.n > a.n ? b : a));
    g.append("text").attr("class", "lbl").attr("x", x(peak.t) - 8).attr("y", y(peak.n) + 10).attr("text-anchor", "end").text(`Peak: ${fmt(peak.n)} in ${d3.timeFormat("%b %Y")(peak.t)}`);
    const cross = g.append("line").attr("y1", 0).attr("y2", ih).style("stroke", "var(--axis)").attr("display", "none");
    const bis = d3.bisector((d) => d.t).right;
    g.append("rect").attr("width", iw).attr("height", ih).attr("fill", "transparent")
      .on("pointermove", (ev) => {
        const [mx] = d3.pointer(ev);
        const d = pts[Math.max(0, bis(pts, x.invert(mx)) - 1)];
        cross.attr("display", null).attr("x1", x(d.t)).attr("x2", x(d.t));
        showTip(`<b>${d3.timeFormat("%B %Y")(d.t)}</b>` + row("Submissions", fmt(d.n)), ev);
      })
      .on("pointerleave", () => { cross.attr("display", "none"); hideTip(); });
  };

  charts["fig-top1"] = (el) =>
    columns(el, {
      cats: S.map((s) => s.label), values: S.map((s) => s.top1pct_share), color: "--accent", yFmt: pct0, height: 220, yMax: 0.35,
      tipFn: (i) => `<b>${S[i].label} Legislature</b>` + row("Busiest 1% of bills", `${Math.ceil(S[i].bills / 100)} bills`) + row("Their share of testimony", pct1(S[i].top1pct_share)),
    });

  charts["fig-formshare"] = (el) =>
    columns(el, {
      cats: S.map((s) => s.label), values: S.map((s) => s.form_letter_share_public), color: "--g-ind", yFmt: pct0, height: 220, yMax: 0.25,
      tipFn: (i) => `<b>${S[i].label} Legislature</b>` + row("Form-letter share", pct1(S[i].form_letter_share_public)),
    });

  charts["fig-posgroup"] = (el) =>
    stacked100(el, {
      rows: D.positions_by_group, keys: POS, labelOf: (r) => r.group, labelW: 130,
      tipFn: (r) => posTip(r.group, r, row("Letters", fmt(r.support + r.oppose + r.neither + r.unknown))),
    });

  charts["fig-committees"] = (el) => {
    const rows = D.committees;
    const w = widthOf(el);
    const dotW = Math.min(180, w * 0.32);
    const rowH = 38;
    const m = { t: 40, r: 10, b: 8, l: 0 };
    const h = m.t + m.b + rows.length * rowH;
    const svg = svgFor(el, w, h);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const barW = w - dotW - 30 - m.r;
    const x = d3.scaleLinear().domain([0, d3.max(rows, (r) => r.submissions)]).range([0, barW - 44]);
    const xd = d3.scaleLinear().domain([0, 0.6]).range([barW + 30, barW + 30 + dotW]);
    g.append("g").attr("class", "axis gridline").attr("transform", `translate(0,-4)`).call(d3.axisTop(xd).ticks(3).tickFormat(pct0).tickSize(-(rows.length * rowH))).select(".domain").remove();
    g.append("text").attr("class", "lbl-muted").attr("x", xd(0.3)).attr("y", -26).attr("text-anchor", "middle").text("Share opposed");
    rows.forEach((r, i) => {
      const y0 = i * rowH;
      g.append("text").attr("class", "lbl").attr("x", 0).attr("y", y0 + 11).text(shorten(r.name, Math.max(24, Math.floor(barW / 6.4))));
      g.append("path").attr("d", barPath(0, y0 + 17, x(r.submissions), 12, "right")).style("fill", "var(--minority)");
      g.append("text").attr("class", "lbl").attr("x", x(r.submissions) + 6).attr("y", y0 + 27).text(compact(r.submissions));
      g.append("circle").attr("cx", xd(r.oppose)).attr("cy", y0 + 23).attr("r", 5).style("fill", "var(--oppose)").style("stroke", "var(--surface)").attr("stroke-width", 2);
      g.append("rect").attr("x", 0).attr("y", y0).attr("width", w).attr("height", rowH).attr("fill", "transparent")
        .on("pointermove", (ev) => showTip(`<b>${esc(r.name)}</b>` + row("Submissions", fmt(r.submissions)) + row("Support", pct0(r.support), "--support") + row("Oppose", pct0(r.oppose), "--oppose") + row("From individuals", pct0(r.individuals), "--g-ind"), ev))
        .on("pointerleave", hideTip);
    });
  };

  charts["fig-volume"] = (el) =>
    stacked100(el, {
      rows: D.volume, keys: POS.slice(0, 3), labelOf: (r) => `${r.bucket} letters`, labelW: 120,
      tipFn: (r) => posTip(`Bills with ${r.bucket} letters`, r, row("Bills", fmt(r.bills)) + row("Bills mostly opposed", pct0(r.mostly_opposed_bills))),
    });

  charts["fig-campaigns"] = (el) => {
    const rows = D.campaigns.largest;
    const w = widthOf(el);
    const rowH = 40;
    const h = rows.length * rowH + 4;
    const svg = svgFor(el, w, h);
    const x = d3.scaleLinear().domain([0, rows[0].size]).range([0, w - 50]);
    rows.forEach((r, i) => {
      const y0 = i * rowH;
      const col = r.position === "oppose" ? "--oppose" : r.position === "support" ? "--support" : "--neither";
      svg.append("text").attr("class", "lbl").attr("x", 0).attr("y", y0 + 12).text(shorten(`LD ${r.ld ?? "—"} (${ORD(r.session)}) · ${r.title}`, Math.floor(w / 6.6)));
      svg.append("path").attr("d", barPath(0, y0 + 18, Math.max(3, x(r.size)), 12, "right")).style("fill", `var(${col})`);
      svg.append("text").attr("class", "lbl-strong").attr("x", x(r.size) + 6).attr("y", y0 + 28).text(fmt(r.size));
      svg.append("rect").attr("width", w).attr("y", y0).attr("height", rowH).attr("fill", "transparent")
        .on("pointermove", (ev) => showTip(`<b>LD ${r.ld} · ${ORD(r.session)} Legislature</b><div>${esc(r.title)}</div>` + row("Copies", fmt(r.size)) + row("Position", r.position, col), ev))
        .on("pointerleave", hideTip);
    });
  };

  function ORD(s) {
    const found = S.find((x) => x.session === s);
    return found ? found.label : String(s);
  }
  const OUTCOME_LABEL = { law: "Became law", died: "Died", ontp: "Ought not to pass", veto: "Vetoed", withdrawn: "Withdrawn", other: "Other", open: "Still open" };

  function table(el, head, rows) {
    const t = el.querySelector("table");
    t.innerHTML = `<thead><tr>${head.map(([h, c]) => `<th class="${c || ""}">${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`;
  }
  const splitBar = (s, o) => {
    const tot = s + o || 1;
    return `<div class="bar100" title="${fmt(s)} support, ${fmt(o)} oppose"><i style="width:${(100 * s) / tot}%;background:var(--support)"></i><i style="width:${(100 * o) / tot}%;background:var(--oppose)"></i></div>`;
  };

  charts["fig-topbills"] = (el) =>
    table(el, [["Bill"], ["Letters", "r"], ["For / against"], ["Outcome"]],
      D.top_bills.map((b) =>
        `<tr><td><div class="t">${esc(shorten(b.title, 90))}</div><div class="m">LD ${b.ld ?? "—"} · ${ORD(b.session)} · ${esc(b.committee)}</div></td>` +
        `<td class="r">${fmt(b.submissions)}</td><td><span class="m">${pct0(b.oppose / (b.support + b.oppose || 1))} opposed</span>${splitBar(b.support, b.oppose)}</td>` +
        `<td><span class="badge ${b.outcome === "law" ? "law" : ""}">${OUTCOME_LABEL[b.outcome] || "—"}</span></td></tr>`));

  charts["fig-orgs"] = (el) =>
    stacked100(el, {
      rows: D.organizations.slice().sort((a, b) => b.support - a.support), keys: POS.slice(0, 3), labelOf: (r) => r.name, labelW: 250,
      tipFn: (r) => posTip(r.name, r, row("Letters", fmt(r.submissions)) + row("Bills", fmt(r.bills)) + row("Committees", r.committees)),
    });

  charts["fig-pairs"] = (el) => {
    const P = D.org_pairs;
    const names = P.order;
    const n = names.length;
    const look = new Map();
    P.all.forEach((p) => { look.set(p.a + "|" + p.b, p); look.set(p.b + "|" + p.a, p); });
    const w = widthOf(el);
    const lw = w < 560 ? 130 : 240;
    const cell = Math.max(13, Math.min(20, Math.floor((w - lw - 4) / n)));
    const h = cell * n + 4;
    const natural = lw + cell * n + 4;
    const svg = svgFor(el, Math.max(w, natural), h);
    // On a phone the matrix keeps a readable cell size and scrolls sideways inside its card.
    const plot = el.querySelector(".plot");
    plot.style.overflowX = natural > w ? "auto" : "";
    if (natural > w) svg.attr("width", natural).style("width", natural + "px").style("max-width", "none");
    const color = d3.scaleLinear().domain([0, 0.5, 1]).range([cssVar("--oppose"), cssVar("--neither"), cssVar("--support")]).interpolate(d3.interpolateLab);
    const g = svg.append("g").attr("transform", `translate(${lw},2)`);
    names.forEach((a, i) => {
      g.append("text").attr("class", "lbl").attr("x", -8).attr("y", i * cell + cell / 2 + 4).attr("text-anchor", "end")
        .style("font-size", cell < 16 ? "11px" : "12px").text(shorten(a, lw < 200 ? 20 : 36));
      names.forEach((b, j) => {
        const p = look.get(a + "|" + b);
        const r = g.append("rect").attr("x", j * cell + 0.5).attr("y", i * cell + 0.5).attr("width", cell - 1).attr("height", cell - 1).attr("rx", 1.5);
        if (i === j) r.style("fill", "var(--axis)");
        else if (!p) r.style("fill", "none").style("stroke", "var(--grid)");
        else
          r.style("fill", color(p.agree))
            .on("pointermove", (ev) => showTip(`<b>${esc(a)}</b><br>and <b>${esc(b)}</b>` + row("Same side", pct0(p.agree)) + row("Shared bills", fmt(p.shared)), ev))
            .on("pointerleave", hideTip);
      });
    });
  };

  let mapLayer = "both";
  let topo = null;
  charts["fig-map"] = async (el) => {
    if (!topo) {
      try {
        topo = await fetch("https://cdn.jsdelivr.net/npm/us-atlas@3.0.1/counties-10m.json").then((r) => r.json());
      } catch (e) {
        el.querySelector(".plot").textContent = "Map boundaries didn't load.";
        return;
      }
    }
    const feats = topojson.feature(topo, topo.objects.counties).features.filter((f) => String(f.id).startsWith("23"));
    const fc = { type: "FeatureCollection", features: feats };
    const byFips = new Map(D.places.counties.map((c) => [c.fips, c]));
    const w = widthOf(el);
    const wide = w >= 700;
    const mw = wide ? Math.round(w * 0.58) : w;
    const mh = Math.round(Math.min(mw * 1.3, 640));
    const listH = D.places.counties.length * 24 + 40;
    const h = wide ? Math.max(mh, listH) : mh + listH + 10;
    const svg = svgFor(el, w, h);
    const proj = d3.geoConicConformal().parallels([43, 47]).rotate([69, 0]).fitSize([mw - 10, mh - 40], fc);
    const path = d3.geoPath(proj);
    const maxPer = d3.max(D.places.counties, (c) => c.per_1k);
    const seq = d3.scaleLinear().domain([0, maxPer]).range([cssVar("--seq-0"), cssVar("--seq-1")]).interpolate(d3.interpolateLab);
    const gm = svg.append("g").attr("transform", "translate(4,36)");
    gm.selectAll("path").data(feats).join("path").attr("d", path)
      .style("fill", (f) => seq(byFips.get(String(f.id))?.per_1k ?? 0)).style("stroke", "var(--surface)").attr("stroke-width", 1.5)
      .on("pointermove", (ev, f) => {
        const c = byFips.get(String(f.id));
        if (c) showTip(`<b>${c.county} County</b>` + row("Letters per 1,000 residents", c.per_1k) + row("Letters from individuals", fmt(c.n)) + row("Residents (2020)", fmt(c.pop)) + row("Share opposed", pct0(c.oppose_share), "--oppose"), ev);
      })
      .on("pointerleave", hideTip);
    // gradient legend
    const lg = svg.append("g").attr("transform", "translate(4,6)");
    const gid = "seqgrad";
    const defs = svg.append("defs").append("linearGradient").attr("id", gid);
    defs.append("stop").attr("offset", "0%").attr("stop-color", seq(0));
    defs.append("stop").attr("offset", "100%").attr("stop-color", seq(maxPer));
    lg.append("rect").attr("width", 140).attr("height", 8).attr("rx", 2).attr("fill", `url(#${gid})`);
    lg.append("text").attr("class", "lbl-muted").attr("y", 22).text("0");
    lg.append("text").attr("class", "lbl-muted").attr("x", 140).attr("y", 22).attr("text-anchor", "end").text(`${Math.round(maxPer)} per 1,000`);
    if (mapLayer === "both") {
      const towns = D.places.towns;
      const rs = d3.scaleSqrt().domain([0, d3.max(towns, (t) => t.n)]).range([0, Math.max(12, mw / 22)]);
      gm.append("g").selectAll("circle").data(towns.slice().sort((a, b) => b.n - a.n)).join("circle")
        .attr("cx", (t) => proj([t.lon, t.lat])[0]).attr("cy", (t) => proj([t.lon, t.lat])[1]).attr("r", (t) => Math.max(1.2, rs(t.n)))
        .style("fill", "var(--g-ind)").style("fill-opacity", 0.55).style("stroke", "var(--surface)").attr("stroke-width", 0.75)
        .on("pointermove", (ev, t) => showTip(`<b>${esc(t.name)}</b> · ${esc(t.county)} County` + row("Letters from residents", fmt(t.n)), ev))
        .on("pointerleave", hideTip);
      towns.filter((t) => ["Portland", "Bangor", "Lewiston", "Augusta"].includes(t.name)).forEach((t) => {
        const [px, py] = proj([t.lon, t.lat]);
        gm.append("text").attr("class", "lbl-strong").attr("x", px + rs(t.n) + 4).attr("y", py + 4).text(t.name);
      });
    }
    // ranked county list
    const list = svg.append("g").attr("transform", wide ? `translate(${mw + 20},36)` : `translate(0,${mh + 20})`);
    const lwid = (wide ? w - mw - 20 : w) - 8;
    const xl = d3.scaleLinear().domain([0, maxPer]).range([0, lwid - 140]);
    list.append("text").attr("class", "lbl-muted").attr("y", -14).text("Letters per 1,000 residents");
    counties.forEach((c, i) => {
      const y0 = i * 24;
      list.append("text").attr("class", "lbl").attr("x", 0).attr("y", y0 + 12).text(c.county);
      list.append("path").attr("d", barPath(100, y0 + 3, xl(c.per_1k), 12, "right")).style("fill", seq(c.per_1k));
      list.append("text").attr("class", "lbl").attr("x", 100 + xl(c.per_1k) + 6).attr("y", y0 + 13).text(c.per_1k.toFixed(0));
    });
  };
  document.querySelectorAll("#fig-map [data-layer]").forEach((b) =>
    b.addEventListener("click", () => {
      mapLayer = b.dataset.layer;
      document.querySelectorAll("#fig-map [data-layer]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      charts["fig-map"](document.getElementById("fig-map"));
    }));

  charts["fig-repeat"] = (el) => {
    const rows = D.repeat;
    const w = widthOf(el);
    const h = 240;
    const m = { t: 22, r: 8, b: 40, l: 40 };
    const svg = svgFor(el, w, h);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const ih = h - m.t - m.b;
    const x = d3.scaleBand().domain(rows.map((r) => r.times)).range([0, iw]).padding(0.25);
    const x1 = d3.scaleBand().domain(["people_share", "testimony_share"]).range([0, x.bandwidth()]).padding(0.08);
    const y = d3.scaleLinear().domain([0, 0.7]).range([ih, 0]);
    yGrid(g, y, iw, 4, pct0);
    g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).tickSize(0).tickPadding(8)).select(".domain").attr("stroke", "var(--axis)");
    g.append("text").attr("class", "lbl-muted").attr("x", iw / 2).attr("y", ih + 34).attr("text-anchor", "middle").text("Letters sent, 2013–2026");
    const bw = Math.min(24, x1.bandwidth());
    rows.forEach((r) => {
      [["people_share", "--minority"], ["testimony_share", "--g-ind"]].forEach(([k, col]) => {
        const bx = x(r.times) + x1(k) + (x1.bandwidth() - bw) / 2;
        g.append("path").attr("d", barPath(bx, y(r[k]), bw, ih - y(r[k]))).style("fill", `var(${col})`);
        g.append("text").attr("class", "lbl").attr("x", bx + bw / 2).attr("y", y(r[k]) - 5).attr("text-anchor", "middle").style("font-size", "11px").text(pct0(r[k]));
      });
      g.append("rect").attr("x", x(r.times)).attr("width", x.bandwidth()).attr("height", ih).attr("fill", "transparent")
        .on("pointermove", (ev) => showTip(`<b>Sent ${r.times} letter${r.times === "1" ? "" : "s"}</b>` + row("People", fmt(r.people)) + row("Share of people", pct1(r.people_share), "--minority") + row("Share of letters", pct1(r.testimony_share), "--g-ind"), ev))
        .on("pointerleave", hideTip);
    });
  };

  const bins = opp.map((b) => b.bin);
  charts["fig-outcome"] = (el) =>
    lines(el, {
      cats: bins, height: 280,
      series: [
        { label: "Became law", color: "--law", values: opp.map((b) => b.passed) },
        { label: "Committee recommended against", color: "--ontp", values: opp.map((b) => b.ontp_report) },
      ],
      tipFn: (i) => {
        const b = opp[i];
        const sign = b.vs_cell >= 0 ? "+" : "−";
        return `<b>${BIN_LABELS[b.bin]} opposed</b>` + row("Bills", fmt(b.bills)) + row("Became law", pct0(b.passed), "--law") + row("Committee recommended against", pct0(b.ontp_report), "--ontp") + row("vs. same committee + session", `${sign}${Math.abs(b.vs_cell * 100).toFixed(1)} pts`);
      },
    });

  charts["fig-scatter"] = (el) => {
    const w = widthOf(el);
    const h = Math.min(420, Math.max(300, w * 0.5));
    const m = { t: 10, r: 12, b: 40, l: 46 };
    const svg = svgFor(el, w, h);
    const g = svg.append("g").attr("transform", `translate(${m.l},${m.t})`);
    const iw = w - m.l - m.r;
    const ih = h - m.t - m.b;
    // deterministic jitter so bills at exactly 0% / 100% don't pile into one column
    const rand = (i) => { const v = Math.sin(i * 12.9898 + 78.233) * 43758.5453; return v - Math.floor(v) - 0.5; };
    const pts = O.points.map(([s, v, passed, rep], i) => ({
      s, v, passed, rep,
      jx: Math.min(1, Math.max(0, s + rand(i) * 0.03)),
      jy: Math.max(4.4, v * Math.exp(rand(i + 99991) * 0.22)),
    }));
    const x = d3.scaleLinear().domain([0, 1]).range([0, iw]);
    const y = d3.scaleLog().domain([4.2, d3.max(pts, (p) => p.v) * 1.1]).range([ih, 0]);
    g.append("g").attr("class", "axis gridline").call(d3.axisLeft(y).tickValues([5, 10, 20, 50, 100, 200, 500, 1000].filter((t) => t <= y.domain()[1])).tickFormat(compact).tickSize(-iw)).select(".domain").remove();
    g.append("g").attr("class", "axis").attr("transform", `translate(0,${ih})`).call(d3.axisBottom(x).ticks(5).tickFormat(pct0)).select(".domain").attr("stroke", "var(--axis)");
    g.append("text").attr("class", "lbl-muted").attr("x", iw / 2).attr("y", ih + 34).attr("text-anchor", "middle").text("Share of public testimony opposed →");
    g.append("text").attr("class", "lbl-muted").attr("transform", `translate(-36,${ih / 2}) rotate(-90)`).attr("text-anchor", "middle").text("Public voices (log)");
    const order = pts.slice().sort((a, b) => a.passed - b.passed);
    g.append("g").selectAll("circle").data(order).join("circle").attr("cx", (p) => x(p.jx)).attr("cy", (p) => y(p.jy)).attr("r", 2.4)
      .style("fill", (p) => (p.passed ? "var(--law)" : "var(--neither)")).style("fill-opacity", (p) => (p.passed ? 0.7 : 0.55));
    const del = d3.Delaunay.from(order, (p) => x(p.jx), (p) => y(p.jy));
    const hl = g.append("circle").attr("r", 5).style("fill", "none").style("stroke", "var(--ink)").attr("stroke-width", 1.5).attr("display", "none");
    const REP = { otp: "Ought to pass", ontp: "Ought not to pass", divided: "Split", none: "No report", other: "Other" };
    g.append("rect").attr("width", iw).attr("height", ih).attr("fill", "transparent")
      .on("pointermove", (ev) => {
        const [mx, my] = d3.pointer(ev);
        const p = order[del.find(mx, my)];
        hl.attr("display", null).attr("cx", x(p.jx)).attr("cy", y(p.jy));
        showTip(row("Share opposed", pct0(p.s)) + row("Public voices", fmt(p.v)) + row("Committee report", REP[p.rep] || p.rep) + row("Outcome", p.passed ? "Became law" : "Did not", p.passed ? "--law" : "--neither"), ev);
      })
      .on("pointerleave", () => { hl.attr("display", "none"); hideTip(); });
  };

  charts["fig-byreport"] = (el) => {
    const box = el.querySelector(".multiples");
    box.innerHTML = "";
    const groups = [...new Set(O.by_report.map((r) => r.group))];
    // Build every panel first so each one measures its final grid width.
    const panels = groups.map((grp) => {
      const rs = O.by_report.filter((r) => r.group === grp);
      const div = document.createElement("div");
      div.innerHTML = `<h4>${esc(grp)}</h4><div class="n">${fmt(d3.sum(rs, (r) => r.bills))} bills · ${pct0(d3.sum(rs, (r) => r.passed * r.bills) / d3.sum(rs, (r) => r.bills))} became law</div><div class="plot"></div>`;
      box.appendChild(div);
      return [grp, rs, div];
    });
    panels.forEach(([grp, rs, div]) => {
      columns(div, {
        cats: rs.map((r) => BIN_LABELS[r.bin]), values: rs.map((r) => (r.bills >= 10 ? r.passed : null)), color: "--law", yFmt: pct0, yMax: 1, height: 190,
        label: () => "",
        tipFn: (i) => `<b>${esc(grp)}, ${BIN_LABELS[rs[i].bin]} opposed</b>` + row("Bills", fmt(rs[i].bills)) + row("Became law", rs[i].bills >= 10 ? pct0(rs[i].passed) : "too few bills", "--law"),
      });
    });
  };

  const party = (key) => (el) => {
    const side = (s) => O.by_party.filter((r) => r.side === s);
    lines(el, {
      cats: bins, height: 240,
      series: [
        { label: "Majority", color: "--majority", values: side("majority").map((r) => r[key]) },
        { label: "Minority", color: "--minority", values: side("minority").map((r) => r[key]) },
      ],
      tipFn: (i) => {
        const a = side("majority")[i];
        const b = side("minority")[i];
        return `<b>${BIN_LABELS[a.bin]} opposed</b>` + row(`Majority (${fmt(a.bills)} bills)`, pct0(a[key]), "--majority") + row(`Minority (${fmt(b.bills)} bills)`, pct0(b[key]), "--minority");
      },
    });
  };
  charts["fig-party-pass"] = party("passed");
  charts["fig-party-ontp"] = party("ontp_report");

  charts["fig-volume-pass"] = (el) =>
    columns(el, {
      cats: O.by_volume.map((r) => r.voices), values: O.by_volume.map((r) => r.passed), color: "--law", yFmt: pct0, yMax: 0.4, height: 220,
      tipFn: (i) => `<b>${O.by_volume[i].voices} public voices</b>` + row("Bills (75%+ opposed)", fmt(O.by_volume[i].bills)) + row("Became law", pct0(O.by_volume[i].passed), "--law"),
    });

  charts["fig-despite"] = (el) =>
    table(el, [["Bill"], ["For", "r"], ["Against", "r"]],
      O.passed_despite.map((b) =>
        `<tr><td><div class="t">${esc(shorten(b.title, 80))}</div><div class="m">LD ${b.ld} · ${ORD(b.session)} · report: ${b.report === "divided" ? "split" : esc(b.report).toUpperCase()}</div></td><td class="r">${fmt(b.support)}</td><td class="r">${fmt(b.oppose)}</td></tr>`));

  /* ---------------- draw, and redraw on resize / theme change ---------------- */
  const drawAll = () => Object.entries(charts).forEach(([id, fn]) => {
    const el = document.getElementById(id);
    if (el) fn(el);
  });
  drawAll();
  let lastW = window.innerWidth;
  let timer;
  window.addEventListener("resize", () => {
    if (window.innerWidth === lastW) return;
    lastW = window.innerWidth;
    clearTimeout(timer);
    timer = setTimeout(drawAll, 150);
  });
  // The heatmap and map interpolate colors in JS, so they need a redraw when the theme flips.
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    ["fig-pairs", "fig-map"].forEach((id) => charts[id](document.getElementById(id)));
  });
})();
