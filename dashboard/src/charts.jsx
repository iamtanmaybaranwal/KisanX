// Hand-built SVG charts (small, fast on a Pi-served page, full control of the specs):
// 2 px lines, hairline grid, dashed line = forecast, 10 % band = uncertainty,
// crosshair + tooltip on hover/touch, and a table view for every chart.
import { useEffect, useMemo, useRef, useState } from "react";

function useWidth() {
  const ref = useRef(null);
  const [w, setW] = useState(640);
  useEffect(() => {
    if (!ref.current) return undefined;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, w];
}

const niceStep = (span) => (span > 60 ? 20 : span > 25 ? 10 : 5);

function yDomain(values) {
  const v = values.filter((x) => x != null && Number.isFinite(x));
  if (!v.length) return [0, 100];
  let lo = Math.min(...v) - 4, hi = Math.max(...v) + 4;
  const step = niceStep(hi - lo);
  lo = Math.max(0, Math.floor(lo / step) * step);
  hi = Math.min(100, Math.ceil(hi / step) * step);
  if (hi - lo < 10) hi = Math.min(100, lo + 10);
  return [lo, hi];
}

const relH = (t, now) => {
  const h = (t - now) / 3600;
  if (Math.abs(h) < 0.05) return 0;
  return Math.round(h * 10) / 10;
};

function Tooltip({ x, y, width, children }) {
  const left = x > width - 170 ? x - 160 : x + 12;
  return <div className="tooltip" style={{ left, top: Math.max(0, y - 10) }}>{children}</div>;
}

/* ===================================================================================== */
/** One zone: last 6 h measured + AI forecast (1/3/6/12 h) with band and thresholds. */
export function ForecastChart({ history, forecast, thresholds, now, tr }) {
  const [ref, W] = useWidth();
  const [hover, setHover] = useState(null);
  const [table, setTable] = useState(false);
  const H = 250, m = { l: 38, r: 64, t: 14, b: 28 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const t0 = now - 6 * 3600, t1 = now + 12 * 3600;

  const hist = useMemo(() => (history || []).filter(([t]) => t >= t0 && t <= now + 60), [history, t0, now]);
  const fc = useMemo(() => {
    if (!forecast) return [];
    const t = forecast.made_at || now;            // anchor at the moment the forecast was made
    return [{ t, v: forecast.m_now, lo: forecast.m_now, hi: forecast.m_now, h: 0 },
      ...forecast.horizons.map((h) => ({ t: t + h.h * 3600, v: h.moisture, lo: h.low, hi: h.high, h: h.h }))];
  }, [forecast, now]);

  const [ylo, yhi] = yDomain([...hist.map((p) => p[1]), ...fc.flatMap((p) => [p.lo, p.hi]),
    thresholds?.min_pct, thresholds?.target_pct]);
  const X = (t) => m.l + ((t - t0) / (t1 - t0)) * iw;
  const Y = (v) => m.t + (1 - (v - ylo) / (yhi - ylo)) * ih;
  const step = niceStep(yhi - ylo);
  const yticks = [];
  for (let v = ylo; v <= yhi + 1e-9; v += step) yticks.push(v);
  const xticks = [-6, -3, 0, 3, 6, 9, 12];

  const histPath = hist.map(([t, v], i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join("");
  const fcPath = fc.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.v).toFixed(1)}`).join("");
  const band = fc.length
    ? fc.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.hi).toFixed(1)}`).join("") +
      [...fc].reverse().map((p) => `L${X(p.t).toFixed(1)},${Y(p.lo).toFixed(1)}`).join("") + "Z"
    : "";
  const last = fc[fc.length - 1];

  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = t0 + ((px - m.l) / iw) * (t1 - t0);
    let best = null;
    for (const [ht, hv] of hist) if (!best || Math.abs(ht - t) < Math.abs(best.t - t)) best = { t: ht, v: hv, kind: "m" };
    for (const p of fc.slice(1)) if (!best || Math.abs(p.t - t) < Math.abs(best.t - t)) best = { ...p, kind: "f" };
    setHover(best);
  };

  const label = (t) => { const h = relH(t, now); return h === 0 ? tr("now") : `${h > 0 ? "+" : ""}${h} h`; };

  return (
    <div className="chart" ref={ref}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={tr("forecastTitle")}
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {yticks.map((v) => (
          <g key={v}>
            <line className="gridline" x1={m.l} x2={W - m.r} y1={Y(v)} y2={Y(v)} />
            <text className="tick-label" x={m.l - 8} y={Y(v) + 4} textAnchor="end">{v}%</text>
          </g>
        ))}
        {thresholds && thresholds.min_pct > ylo && (
          <g>
            <rect x={m.l} width={iw} y={Y(thresholds.min_pct)} height={Math.max(0, Y(ylo) - Y(thresholds.min_pct))}
              fill="var(--serious)" opacity="0.09" />
            <text className="ref-label" x={m.l + 6} y={Y(thresholds.min_pct) + 14}>
              {tr("needsBelow")} {Math.round(thresholds.min_pct)}%
            </text>
          </g>
        )}
        {thresholds && thresholds.target_pct < yhi && (
          <g>
            <line x1={m.l} x2={W - m.r} y1={Y(thresholds.target_pct)} y2={Y(thresholds.target_pct)}
              stroke="var(--ink-2)" strokeWidth="1" opacity="0.45" />
            <text className="ref-label" x={m.l + 6} y={Y(thresholds.target_pct) - 5}>
              {tr("target")} {Math.round(thresholds.target_pct)}%
            </text>
          </g>
        )}
        <line className="axis" x1={m.l} x2={W - m.r} y1={H - m.b} y2={H - m.b} />
        {xticks.map((h) => (
          <text key={h} className="tick-label" x={X(now + h * 3600)} y={H - 8} textAnchor="middle">
            {h === 0 ? tr("now") : `${h > 0 ? "+" : ""}${h} h`}
          </text>
        ))}
        <line x1={X(now)} x2={X(now)} y1={m.t} y2={H - m.b} stroke="var(--axis)" strokeWidth="1" />
        {band && <path d={band} fill="var(--s1)" opacity="0.12" />}
        {histPath && <path d={histPath} fill="none" stroke="var(--s1)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />}
        {fcPath && <path d={fcPath} fill="none" stroke="var(--s1)" strokeWidth="2" strokeDasharray="5 4" strokeLinecap="round" />}
        {last && (
          <g>
            <circle cx={X(last.t)} cy={Y(last.v)} r="6" fill="var(--surface)" />
            <circle cx={X(last.t)} cy={Y(last.v)} r="4" fill="var(--s1)" />
            <text className="end-label" x={X(last.t) + 9} y={Y(last.v) + 4}>{Math.round(last.v)}%</text>
          </g>
        )}
        {hover && (
          <g>
            <line className="crosshair" x1={X(hover.t)} x2={X(hover.t)} y1={m.t} y2={H - m.b} />
            <circle cx={X(hover.t)} cy={Y(hover.v)} r="6" fill="var(--surface)" />
            <circle cx={X(hover.t)} cy={Y(hover.v)} r="4" fill="var(--s1)" />
          </g>
        )}
        <rect x={m.l} y={m.t} width={iw} height={ih} fill="transparent" />
      </svg>
      {hover && (
        <Tooltip x={(X(hover.t) / W) * (ref.current?.clientWidth || W)} y={(Y(hover.v) / H) * (ref.current?.clientHeight || H)}
          width={ref.current?.clientWidth || W}>
          <div className="t">{label(hover.t)} · {hover.kind === "f" ? tr("forecast") : tr("measured")}</div>
          <div className="row"><span className="key" style={{ background: "var(--s1)" }} /><b>{hover.v.toFixed(1)}%</b></div>
          {hover.kind === "f" && <div className="t">{tr("range")}: {Math.round(hover.lo)}–{Math.round(hover.hi)}%</div>}
        </Tooltip>
      )}
      <button className="table-toggle" onClick={() => setTable(!table)}>{table ? tr("hideTable") : tr("showTable")}</button>
      {table && (
        <table className="data">
          <thead><tr><th>{tr("horizon")}</th><th>{tr("forecast")}</th><th>{tr("range")}</th></tr></thead>
          <tbody>
            {fc.map((p) => (
              <tr key={p.h}><td>{p.h === 0 ? tr("now") : `+${p.h} h`}</td><td>{p.v.toFixed(1)}%</td>
                <td>{Math.round(p.lo)}–{Math.round(p.hi)}%</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

/* ===================================================================================== */
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];

/** All zones, last 24 h. Colour follows the zone (fixed order), legend + end labels. */
export function HistoryChart({ series, zones, now, tr }) {
  const [ref, W] = useWidth();
  const [hover, setHover] = useState(null);
  const [table, setTable] = useState(false);
  const H = 260, m = { l: 38, r: 96, t: 12, b: 28 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const t0 = now - 24 * 3600, t1 = now;
  const lines = zones.map((z, i) => ({ z, color: SERIES[i % 4], pts: (series?.[String(z.zone)] || []).filter(([t]) => t >= t0) }));
  const [ylo, yhi] = yDomain(lines.flatMap((l) => l.pts.map((p) => p[1])));
  const X = (t) => m.l + ((t - t0) / (t1 - t0)) * iw;
  const Y = (v) => m.t + (1 - (v - ylo) / (yhi - ylo)) * ih;
  const step = niceStep(yhi - ylo);
  const yticks = [];
  for (let v = ylo; v <= yhi + 1e-9; v += step) yticks.push(v);

  // end labels only when they do not collide (else the legend + tooltip carry identity)
  const ends = lines.filter((l) => l.pts.length).map((l) => ({ ...l, y: Y(l.pts[l.pts.length - 1][1]) })).sort((a, b) => a.y - b.y);
  const labelsFit = ends.every((e, i) => i === 0 || e.y - ends[i - 1].y >= 14);

  const nearest = (pts, t) => {
    let lo = 0, hi = pts.length - 1;
    if (hi < 0) return null;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (pts[mid][0] < t) lo = mid + 1; else hi = mid; }
    const c = [pts[lo], pts[lo - 1]].filter(Boolean);
    return c.reduce((a, b) => (Math.abs(b[0] - t) < Math.abs(a[0] - t) ? b : a));
  };
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = t0 + ((((e.clientX - r.left) / r.width) * W - m.l) / iw) * (t1 - t0);
    const tt = Math.min(Math.max(t, t0), t1);
    setHover({ t: tt, rows: lines.map((l) => ({ ...l, p: nearest(l.pts, tt) })) });
  };
  const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  const hourly = [];
  for (let h = 24; h >= 0; h -= 3) hourly.push(now - h * 3600);

  return (
    <div className="chart" ref={ref}>
      <div className="legend">
        {lines.map((l) => <span key={l.z.zone}><i style={{ background: l.color }} />{l.z.label}</span>)}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={tr("historyTitle")}
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        {yticks.map((v) => (
          <g key={v}>
            <line className="gridline" x1={m.l} x2={W - m.r} y1={Y(v)} y2={Y(v)} />
            <text className="tick-label" x={m.l - 8} y={Y(v) + 4} textAnchor="end">{v}%</text>
          </g>
        ))}
        <line className="axis" x1={m.l} x2={W - m.r} y1={H - m.b} y2={H - m.b} />
        {[-24, -18, -12, -6, 0].map((h) => (
          <text key={h} className="tick-label" x={X(now + h * 3600)} y={H - 8} textAnchor="middle">
            {h === 0 ? tr("now") : `${h} h`}
          </text>
        ))}
        {lines.map((l) => l.pts.length > 1 && (
          <path key={l.z.zone} fill="none" stroke={l.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"
            d={l.pts.map(([t, v], i) => `${i ? "L" : "M"}${X(t).toFixed(1)},${Y(v).toFixed(1)}`).join("")} />
        ))}
        {ends.map((e) => (
          <g key={e.z.zone}>
            <circle cx={X(e.pts[e.pts.length - 1][0])} cy={e.y} r="6" fill="var(--surface)" />
            <circle cx={X(e.pts[e.pts.length - 1][0])} cy={e.y} r="4" fill={e.color} />
            {labelsFit && <text className="end-label" x={X(e.pts[e.pts.length - 1][0]) + 10} y={e.y + 4}>{e.z.short}</text>}
          </g>
        ))}
        {hover && <line className="crosshair" x1={X(hover.t)} x2={X(hover.t)} y1={m.t} y2={H - m.b} />}
        <rect x={m.l} y={m.t} width={iw} height={ih} fill="transparent" />
      </svg>
      {hover && (
        <Tooltip x={(X(hover.t) / W) * (ref.current?.clientWidth || W)} y={40} width={ref.current?.clientWidth || W}>
          <div className="t">{clock(hover.t)}</div>
          {hover.rows.map((r) => r.p && (
            <div className="row" key={r.z.zone}><span className="key" style={{ background: r.color }} />
              <b>{r.p[1].toFixed(1)}%</b><span className="t" style={{ margin: 0 }}>{r.z.label}</span></div>
          ))}
        </Tooltip>
      )}
      <button className="table-toggle" onClick={() => setTable(!table)}>{table ? tr("hideTable") : tr("showTable")}</button>
      {table && (
        <table className="data">
          <thead><tr><th>{tr("time")}</th>{lines.map((l) => <th key={l.z.zone}>{l.z.label}</th>)}</tr></thead>
          <tbody>
            {hourly.map((t) => (
              <tr key={t}><td>{clock(t)}</td>
                {lines.map((l) => { const p = nearest(l.pts, t); return <td key={l.z.zone}>{p && Math.abs(p[0] - t) < 1800 ? `${p[1].toFixed(1)}%` : "–"}</td>; })}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
