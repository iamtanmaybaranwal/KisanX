import { useEffect, useMemo, useRef, useState } from "react";
import QRCode from "qrcode";
import { api, useHistory, useLive, usePref } from "./api.js";
import { ForecastChart, HistoryChart } from "./charts.jsx";
import { LANGUAGES, cropName, langMeta, reasonFor, translator } from "./i18n.js";

const QS = new URLSearchParams(location.search);
const STATUS_TONE = { ok: "good", soon: "warning", dry: "serious", fault: "critical", unknown: "" };
const LOCATIONS = [
  ["New Delhi, Delhi", 28.61, 77.21], ["Lucknow, Uttar Pradesh", 26.85, 80.95], ["Varanasi, Uttar Pradesh", 25.32, 82.97],
  ["Meerut, Uttar Pradesh", 28.98, 77.71], ["Patna, Bihar", 25.59, 85.14], ["Muzaffarpur, Bihar", 26.12, 85.39],
  ["Ludhiana, Punjab", 30.9, 75.85], ["Amritsar, Punjab", 31.63, 74.87], ["Karnal, Haryana", 29.69, 76.99],
  ["Hisar, Haryana", 29.15, 75.72],
];

/* ---- small pieces ----------------------------------------------------------------------- */
function StatusPill({ status, tr }) {
  return <span className="status-pill"><span className={`dot ${STATUS_TONE[status] || ""}`} />{tr(`st_${status}`)}</span>;
}

function Meter({ value, thr, tr }) {
  const v = value == null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v)}>
      <div className="fill" style={{ width: `${v}%` }} />
      {thr && <div className="ideal-band" title={tr("ideal")}
        style={{ insetInlineStart: `${thr.min_pct}%`, width: `${Math.max(thr.target_pct - thr.min_pct, 0)}%` }} />}
      {thr && <div className="tick" style={{ insetInlineStart: `${thr.min_pct}%` }}><span>{Math.round(thr.min_pct)}%</span></div>}
      {thr && <div className="tick" style={{ insetInlineStart: `${thr.target_pct}%`, opacity: 0.55 }}><span>{Math.round(thr.target_pct)}%</span></div>}
    </div>
  );
}

function Tile({ label, value, unit, sub, up }) {
  return (
    <div className="card tile">
      <div className="label">{label}</div>
      <div className="value">{value}{unit && <small>{unit}</small>}</div>
      {sub && <div className={`sub${up ? " up" : ""}`}>{sub}</div>}
    </div>
  );
}

/* ---- zone card ---------------------------------------------------------------------------- */
function ZoneCard({ z, lang, tr, selected, onSelect, act }) {
  const busy = z.session != null;
  return (
    <div className={`card zone${selected ? " selected" : ""}`} role="button" tabIndex={0}
      onClick={onSelect} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect()}>
      <div className="zone-head">
        <div className="names">
          <div className="crop">{cropName(lang, z.crop, z.crop_label)}</div>
          <div className="zname">{tr("zone")} <bdi>{String(z.zone).padStart(2, "0")}</bdi></div>
        </div>
        <span className="status"><StatusPill status={z.status} tr={tr} /></span>
      </div>
      <div className="zone-mid">
        <div className="big">{z.moisture == null ? "–" : z.moisture.toFixed(1)}<small>%</small></div>
        <div className="moist-label">{tr("moisture")}</div>
      </div>
      <Meter value={z.moisture} thr={z.thresholds} tr={tr} />
      {z.thresholds && <div className="ideal-line"><span className="ideal-key" />{tr("ideal")}{" "}
        <bdi>{Math.round(z.thresholds.min_pct)}–{Math.round(z.thresholds.target_pct)}%</bdi></div>}
      <div className="reason">{reasonFor(z, tr, lang)}</div>
      <div className="zone-foot" onClick={(e) => e.stopPropagation()}>
        {z.pump_on && <span className="pump"><span className="drop" />{tr("pumpOn")}</span>}
        <span style={{ marginInlineStart: "auto" }} className="btn-row">
          {z.fault ? <button className="btn danger" onClick={() => act(`/api/zones/${z.zone}/reset_fault`)}>{tr("resetFault")}</button>
            : busy ? <button className="btn" onClick={() => act(`/api/zones/${z.zone}/stop`)}>{tr("stop")}</button>
              : <button className="btn primary" disabled={z.moisture == null}
                  onClick={() => act(`/api/zones/${z.zone}/irrigate`, {})}>{tr("irrigateNow")}</button>}
        </span>
      </div>
    </div>
  );
}

/* ---- zone detail ---------------------------------------------------------------------------- */
function ZoneDetail({ z, now, lang, tr, act, options }) {
  const hist = useHistory(6, z.zone);
  const [form, setForm] = useState(null);
  const [editing, setEditing] = useState(QS.get("edit") === "1");
  const [region, setRegion] = useState("");
  useEffect(() => {
    setForm({ crop: z.crop, stage: z.stage, soil: z.soil, depth_mm: z.depth_mm, flow_l_per_min: z.flow_l_per_min });
  }, [z.zone, z.crop, z.stage, z.soil, z.depth_mm, z.flow_l_per_min]);
  useEffect(() => { setEditing(QS.get("edit") === "1"); setRegion(QS.get("region") || ""); }, [z.zone]);
  const regionInfo = options?.regions?.[region];
  const common = regionInfo ? regionInfo.crops.filter((c) => options.crops[c]) : [];
  const soilLabel = (s) => options?.soils?.[s] || s.replace("_", " ");
  const thr = z.thresholds;
  const ai = z.ai?.learning || {};
  const until = z.forecast?.hours_until_min;
  return (
    <section className="card detail" aria-label={`${tr("zone")} ${z.zone}`}>
      <div>
        <h2>{cropName(lang, z.crop, z.crop_label)} · {tr("zone")} {String(z.zone).padStart(2, "0")}</h2>
        <p className="sub-ink">{tr("forecastTitle")}</p>
        <ForecastChart history={hist?.zones?.[String(z.zone)]} forecast={z.session ? null : z.forecast}
          thresholds={z.thresholds} now={now} tr={tr} />
        <p className="note">{z.session ? tr("fcAfterWatering") : tr("forecastNote")}</p>
      </div>
      <div>
        <h3 style={{ marginTop: 0 }}>{tr("crop")}</h3>
        <div className="crop-box">
          <div className="crop-now">
            <div className="crop-name">{cropName(lang, z.crop, z.crop_label)}</div>
            <div className="note">{soilLabel(z.soil)} · {tr("stages")?.[z.stage] || z.stage}</div>
            {thr && <div className="crop-ideal">{tr("idealRange")}: <b><bdi>{Math.round(thr.min_pct)}–{Math.round(thr.target_pct)}%</bdi></b>
              <div className="note">{tr("startsBelow", { m: Math.round(thr.min_pct) })}</div></div>}
          </div>
          {!editing && <button className="btn" onClick={() => setEditing(true)}>{tr("changeCrop")}</button>}
        </div>
        {editing && form && options && (
          <form className="form crop-form" onSubmit={(e) => {
            e.preventDefault(); act(`/api/zones/${z.zone}/config`, form, tr("saved")); setEditing(false); }}>
            <label>{tr("region")}
              <select value={region} onChange={(e) => {
                const r = e.target.value; setRegion(r);
                if (r && options.regions[r]) setForm({ ...form, soil: options.regions[r].soil });
              }}>
                <option value="">{tr("anyRegion")}</option>
                {Object.keys(options.regions || {}).map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label>{tr("crop")}
              <select value={form.crop} onChange={(e) => setForm({ ...form, crop: e.target.value })}>
                {common.length > 0 && (
                  <optgroup label={`${tr("commonIn")} ${region}`}>
                    {common.map((c) => <option key={c} value={c}>{cropName(lang, c, options.crops[c])}</option>)}
                  </optgroup>)}
                <optgroup label={tr("allCrops")}>
                  {Object.keys(options.crops).map((c) => <option key={c} value={c}>{cropName(lang, c, options.crops[c])}</option>)}
                </optgroup>
              </select>
            </label>
            <label>{tr("stage")}
              <select value={form.stage} onChange={(e) => setForm({ ...form, stage: e.target.value })}>
                {options.stages.map((s) => <option key={s} value={s}>{tr("stages")?.[s] || s}</option>)}
              </select>
            </label>
            <label>{tr("soil")}
              <select value={form.soil} onChange={(e) => setForm({ ...form, soil: e.target.value })}>
                {Object.keys(options.soils).map((s) => <option key={s} value={s}>{soilLabel(s)}</option>)}
              </select>
            </label>
            <label>{tr("depth")}
              <input type="number" min="30" max="1000" value={form.depth_mm}
                onChange={(e) => setForm({ ...form, depth_mm: Number(e.target.value) })} />
            </label>
            <label>{tr("flow")}
              <input type="number" step="0.1" min="0.05" max="20" value={form.flow_l_per_min}
                onChange={(e) => setForm({ ...form, flow_l_per_min: Number(e.target.value) })} />
            </label>
            <div className="btn-row" style={{ gridColumn: "1 / -1" }}>
              <button className="btn primary" type="submit">{tr("save")}</button>
              <button className="btn" type="button" onClick={() => setEditing(false)}>{tr("cancel")}</button>
            </div>
          </form>
        )}

        <h3>{tr("decision")}</h3>
        <div className="callout">
          <strong>{tr(`a_${z.decision?.action}`)}</strong>
          {reasonFor(z, tr, lang)}
        </div>
        <h3>{tr("ai")}</h3>
        <dl className="kv">
          <dt>{tr("forecast")} 6 h</dt>
          <dd>{z.forecast && !z.session ? `${Math.round(z.forecast.horizons.find((h) => h.h === 6)?.moisture ?? 0)}%` : "–"}</dd>
          {until != null && until > 0 && !z.session && <><dt>{tr("needsWater")}</dt>
            <dd>{tr("inHours", { h: until >= 2 ? Math.round(until) : until })}</dd></>}
          <dt>{tr("checks")}</dt><dd>{ai.checks ?? 0}</dd>
          <dt>{tr("avgErr")}</dt><dd>{ai.avg_error_pct != null ? `±${ai.avg_error_pct}%` : "–"}</dd>
          <dt>{tr("skill")}</dt><dd>{ai.skill_vs_no_change_pct != null ? `${Math.round(ai.skill_vs_no_change_pct)}%` : "–"}</dd>
          <dt>{tr("correction")}</dt><dd>{ai.correction_active ? tr("active") : tr("learning")}</dd>
          <dt>{tr("pumpGain")}</dt><dd>{z.ai?.pump_gain_pct_per_s} {tr("perSec")}</dd>
        </dl>

        <p className="note" style={{ marginTop: 14 }}>{tr("sensorRaw")}: <bdi>{z.raw == null ? "–" : Math.round(z.raw)}</bdi></p>
      </div>
    </section>
  );
}

function ThemeIcon({ theme }) {
  const p = { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8,
    strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true };
  if (theme === "dark") return <svg {...p}><path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" /></svg>;
  if (theme === "light") return (
    <svg {...p}><circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>);
  return <svg {...p}><circle cx="12" cy="12" r="8" /><path d="M12 4v16" /><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" /></svg>;
}

/* ---- language picker: themed, readable in light + dark, native + English names ---- */
function LanguagePicker({ lang, setLang, label, startOpen }) {
  const [open, setOpen] = useState(!!startOpen);
  const box = useRef(null);
  const current = langMeta(lang);
  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (box.current && !box.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    box.current?.querySelector('[aria-selected="true"]')?.focus();
    return () => { document.removeEventListener("pointerdown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open]);
  return (
    <div className="lang" ref={box}>
      <button className="lang-btn" aria-haspopup="listbox" aria-expanded={open} aria-label={label}
        onClick={() => setOpen(!open)}>
        <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.8">
          <circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z" />
        </svg>
        <span>{current.native}</span>
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden><path d="M2.5 4.5L6 8l3.5-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" /></svg>
      </button>
      {open && (
        <div className="lang-panel" role="listbox" aria-label={label}>
          {LANGUAGES.map((l) => (
            <button key={l.code} role="option" aria-selected={l.code === lang} className="lang-item" dir={l.dir || "ltr"}
              onClick={() => { setLang(l.code); setOpen(false); }}>
              <span className="lang-native">{l.native}</span>
              <span className="lang-en">{l.english}</span>
              {l.code === lang && <span className="lang-check" aria-hidden>✓</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---- views ------------------------------------------------------------------------------------------------ */
function FarmView({ s, lang, tr, act, options, selected, setSelected }) {
  const w = s.water || {};
  const we = s.weather || {};
  const z = s.zones.find((x) => x.zone === selected);
  return (
    <>
      <div className="tiles">
        <Tile label={tr("waterToday")} value={(w.today_l ?? 0).toFixed(1)} unit=" L" />
        <Tile label={tr("waterSaved")} value={w.saved_pct == null ? "–" : `${Math.round(w.saved_pct)}`} unit={w.saved_pct == null ? "" : "%"}
          sub={w.saved_l > 0 ? <><bdi>{w.saved_l.toFixed(1)} L</bdi> {tr("vsTimer")}</> : tr("vsTimer")} up={w.saved_l > 0} />
        <Tile label={tr("rain6h")} value={(we.rain_next_6h_mm ?? 0).toFixed(1)} unit=" mm"
          sub={<><bdi>{Math.round(we.rain_prob_next_6h ?? 0)}%</bdi> {tr("chance")}</>} />
        <Tile label={tr("temp")} value={s.device?.temp ?? we.temp_c ?? "–"} unit="°C"
          sub={(s.device?.rh ?? we.rh_pct) != null ? <><bdi>{Math.round(s.device?.rh ?? we.rh_pct)}%</bdi> {tr("humidity")}</> : ""} />
      </div>
      <div className="zones">
        {s.zones.map((zone) => (
          <ZoneCard key={zone.zone} z={zone} lang={lang} tr={tr} act={act} selected={zone.zone === selected}
            onSelect={() => setSelected(zone.zone === selected ? null : zone.zone)} />
        ))}
      </div>
      {z && <ZoneDetail z={z} now={s.time} lang={lang} tr={tr} act={act} options={options} />}
    </>
  );
}

function HistoryView({ s, lang, tr }) {
  const h = useHistory(24, null);
  const [log, setLog] = useState([]);
  useEffect(() => {
    let alive = true;
    const load = () => api("/api/decisions?limit=40").then((d) => alive && setLog(d)).catch(() => {});
    load();
    const id = setInterval(load, 15000);
    return () => { alive = false; clearInterval(id); };
  }, []);
  const zones = s.zones.map((z) => ({ zone: z.zone, label: `${cropName(lang, z.crop, z.crop_label)} (${z.zone})`,
    short: cropName(lang, z.crop, z.crop_label) }));
  const crop = Object.fromEntries(s.zones.map((z) => [z.zone, cropName(lang, z.crop, z.crop_label)]));
  return (
    <div className="grid2" style={{ gridTemplateColumns: "minmax(0,1fr)" }}>
      <div className="card pad">
        <h2>{tr("historyTitle")}</h2>
        <HistoryChart series={h?.zones} zones={zones} now={s.time} tr={tr} />
      </div>
      <div className="card pad">
        <h2>{tr("decisions")}</h2>
        <ul className="log">
          {log.map((d, i) => (
            <li key={i}>
              <time>{new Date(d.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</time>
              <span className="zcol">{crop[d.zone] || d.zone}</span>
              <span><b>{d.action === "SESSION_END" ? tr("sessionEnd") : tr(`a_${d.action}`)}</b> · {lang === "hi" ? d.reason_hi : d.reason_en}</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

function DemoView({ s, tr, act, options, lang }) {
  const [vals, setVals] = useState({});
  const sim = s.weather?.simulation;
  const scen = options?.scenarios || {};
  const loc = s.weather?.location;
  return (
    <div className="grid2">
      <div className="card pad">
        <h2>{tr("simRain")}</h2>
        <p className="note">{tr("demoNote")}</p>
        <div className="btn-row" style={{ marginTop: 10 }}>
          <button className={`btn${!sim ? " primary" : ""}`} onClick={() => act("/api/weather/simulate")}>{tr("simNone")}</button>
          {Object.entries(scen).map(([k, label]) => (
            <button key={k} className={`btn${sim === label ? " primary" : ""}`} onClick={() => act(`/api/weather/simulate?scenario=${k}`)}>
              {label}</button>
          ))}
        </div>
        <h2 style={{ marginTop: 22 }}>{tr("location")}</h2>
        <div className="select" style={{ marginTop: 6 }}>
          <select style={{ maxWidth: "none" }} value={loc?.name || ""} onChange={(e) => {
            const l = LOCATIONS.find((x) => x[0] === e.target.value);
            if (l) act("/api/weather/location", { name: l[0], lat: l[1], lon: l[2] });
          }}>
            {loc && !LOCATIONS.some((l) => l[0] === loc.name) && <option value={loc.name}>{loc.name}</option>}
            {LOCATIONS.map((l) => <option key={l[0]} value={l[0]}>{l[0]}</option>)}
          </select>
        </div>
      </div>
      <div className="card pad">
        <h2>{tr("demoSoil")}</h2>
        {!options?.simulated_device && <p className="note">{tr("realDevice")}</p>}
        {options?.simulated_device && (
          <>
            {s.zones.map((z) => {
              const v = vals[z.zone] ?? Math.round(z.moisture ?? 50);
              return (
                <div className="demo-zone" key={z.zone}>
                  <span>{cropName(lang, z.crop, z.crop_label)}</span>
                  <input type="range" min="5" max="90" value={v} aria-label={`${tr("moisture")} ${z.zone}`}
                    onChange={(e) => setVals({ ...vals, [z.zone]: Number(e.target.value) })}
                    onPointerUp={() => act(`/api/sim/zones/${z.zone}/moisture/${v}`)}
                    onKeyUp={() => act(`/api/sim/zones/${z.zone}/moisture/${v}`)} />
                  <b>{v}%</b>
                </div>
              );
            })}
            <div className="btn-row" style={{ marginTop: 12 }}>
              <button className="btn" onClick={() => act("/api/sim/tank/0")}>{tr("tank")}: 0</button>
              <button className="btn" onClick={() => act("/api/sim/tank/15")}>{tr("tank")}: 15</button>
              <button className="btn" onClick={() => act("/api/sim/zones/1/blocked/true")}>{tr("blockPipe")} (1)</button>
              <button className="btn" onClick={() => act("/api/sim/zones/1/blocked/false")}>{tr("fixPipe")} (1)</button>
              <button className="btn" onClick={() => act("/api/sim/zones/2/unplugged/true")}>{tr("unplug")} (2)</button>
              <button className="btn" onClick={() => act("/api/sim/zones/2/unplugged/false")}>{tr("plugIn")} (2)</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function QR({ text, label, sub }) {
  const [src, setSrc] = useState("");
  useEffect(() => {
    QRCode.toDataURL(text, { margin: 1, width: 440, color: { dark: "#1f2b20", light: "#ffffff" } }).then(setSrc).catch(() => {});
  }, [text]);
  return (
    <div className="card pad qr">
      <h2>{label}</h2>
      {src && <img src={src} alt={label} />}
      <code>{sub}</code>
    </div>
  );
}

function PhoneView({ tr, options, act }) {
  const wifi = options?.share_wifi || { ssid: "", password: "" };
  const [edit, setEdit] = useState(false);
  const [form, setForm] = useState(wifi);
  useEffect(() => setForm(wifi), [wifi.ssid, wifi.password]); // eslint-disable-line react-hooks/exhaustive-deps
  // phones often cannot open ".local" names, so the QR always uses the Pi's IP address
  const port = location.port && location.port !== "80" ? `:${location.port}` : "";
  const host = options?.server_ip && location.hostname.endsWith(".local") ? `${options.server_ip}${port}` : location.host;
  const url = `${location.protocol}//${host}`;
  const esc = (v) => v.replace(/([\\;,:"])/g, "\\$1");      // Wi-Fi QR escaping
  return (
    <>
      <p className="sub-ink" style={{ fontSize: 15 }}>{tr("shareTitle")}</p>
      <div className="grid2">
        <div>
          <QR text={`WIFI:T:WPA;S:${esc(wifi.ssid)};P:${esc(wifi.password)};;`} label={tr("step1")} sub={`${wifi.ssid} · ${wifi.password}`} />
          <div style={{ marginTop: 8, textAlign: "center" }}>
            {!edit ? <button className="table-toggle" onClick={() => setEdit(true)}>{tr("editWifi")}</button> : (
              <form className="form" style={{ marginTop: 6 }} onSubmit={(e) => {
                e.preventDefault(); act("/api/share/wifi", form, tr("saved")).then(() => setEdit(false)); }}>
                <label>{tr("wifiName")}<input value={form.ssid} onChange={(e) => setForm({ ...form, ssid: e.target.value })} /></label>
                <label>{tr("wifiPass")}<input value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
                <label>&nbsp;<button className="btn primary" type="submit">{tr("save")}</button></label>
              </form>
            )}
          </div>
        </div>
        <QR text={url} label={tr("step2")} sub={url} />
      </div>
      <p className="note" style={{ marginTop: 12, textAlign: "center" }}>{tr("shareNote")}</p>
    </>
  );
}

/* ---- app --------------------------------------------------------------------------------------------------- */

export default function App() {
  const { state: s, connected, stale, refresh } = useLive();
  const [lang, setLang] = usePref("jd.lang", "en");
  const [theme, setTheme] = usePref("jd.theme", "auto");
  const [tab, setTab] = usePref("jd.tab", "farm");
  const [selected, setSelected] = useState(QS.get("zone") ? Number(QS.get("zone")) : null);
  useEffect(() => {                       // ?lang=bho&theme=light&tab=history (e.g. from a QR code)
    if (QS.get("lang") && LANGUAGES.some((l) => l.code === QS.get("lang"))) setLang(QS.get("lang"));
    if (["auto", "light", "dark"].includes(QS.get("theme"))) setTheme(QS.get("theme"));
    if (["farm", "history", "demo", "phone"].includes(QS.get("tab"))) setTab(QS.get("tab"));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const [toast, setToast] = useState(null);
  const [options, setOptions] = useState(null);
  const tr = useMemo(() => translator(lang), [lang]);

  useEffect(() => {
    const meta = langMeta(lang);
    document.documentElement.lang = lang;
    document.documentElement.dir = meta.dir || "ltr";
  }, [lang]);
  useEffect(() => {
    if (theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
  }, [theme]);
  useEffect(() => { api("/api/options").then(setOptions).catch(() => {}); }, []);
  useEffect(() => { if (!toast) return undefined; const id = setTimeout(() => setToast(null), 3500); return () => clearTimeout(id); }, [toast]);

  const act = async (path, body, okMsg) => {
    try {
      await api(path, { method: "POST", body });
      if (okMsg) setToast(okMsg);
      refresh();
    } catch (e) {
      setToast(e.message);
    }
  };

  const tabs = [["farm", tr("farm")], ["history", tr("history")], ["demo", tr("demo")], ["phone", tr("phone")]];
  const dev = s?.device;
  const we = s?.weather;
  const weatherLabel = we?.source === "simulated" ? tr("weatherSim") : we?.source === "live" ? tr("weatherLive") : tr("weatherCached");

  return (
    <div className="wrap">
      <header className="top">
        <div className="brand">
          <img src="/icon.svg" alt="" />
          <div><h1>Jal-Drishti</h1><p>{tr("tagline")}</p></div>
        </div>
        <div className="controls">
          <LanguagePicker lang={lang} setLang={setLang} label={tr("language")} startOpen={QS.get("langmenu") === "open"} />
          {s && (
            <div className="seg" role="group">
              <button aria-pressed={s.mode === "auto"} onClick={() => act("/api/mode/auto")}>{tr("auto")}</button>
              <button aria-pressed={s.mode === "manual"} onClick={() => act("/api/mode/manual")}>{tr("manual")}</button>
            </div>
          )}
          <button className="icon-btn" aria-label="Theme" title="Theme"
            onClick={() => setTheme(theme === "auto" ? "light" : theme === "light" ? "dark" : "auto")}>
            <ThemeIcon theme={theme} />
          </button>
        </div>
      </header>

      {s && (
        <div className="statusbar">
          <span className="chip"><span className={`dot ${dev?.online ? "good" : "critical"}`} />{dev?.online ? tr("online") : tr("offline")}</span>
          <span className="chip"><span className={`dot ${we?.source === "live" ? "good" : we?.source === "simulated" ? "warning" : ""}`} />{weatherLabel}{we?.location?.name ? ` · ${we.location.name.split(",")[0]}` : ""}</span>
        </div>
      )}
      {(!connected || stale) && s && <div className="banner"><span className="dot warning" />{tr("stale")}</div>}

      <nav className="tabs" role="tablist">
        {tabs.map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}>{label}</button>
        ))}
      </nav>

      {!s ? <div className="banner">{tr("connecting")}</div> : (
        <main>
          {tab === "farm" && <FarmView s={s} lang={lang} tr={tr} act={act} options={options} selected={selected} setSelected={setSelected} />}
          {tab === "history" && <HistoryView s={s} lang={lang} tr={tr} />}
          {tab === "demo" && <DemoView s={s} tr={tr} act={act} options={options} lang={lang} />}
          {tab === "phone" && <PhoneView tr={tr} options={options} act={async (p, b, m) => { await act(p, b, m); setOptions(await api("/api/options")); }} />}
        </main>
      )}
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
