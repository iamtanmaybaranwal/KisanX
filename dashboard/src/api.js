import { useCallback, useEffect, useRef, useState } from "react";

export async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${res.status})`);
  }
  return data;
}

/** Live farm state: WebSocket push every 2 s, with polling as a fallback. */
export function useLive() {
  const [state, setState] = useState(null);
  const [connected, setConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(0);
  const wsRef = useRef(null);

  const accept = useCallback((s) => { setState(s); setLastUpdate(Date.now()); }, []);

  const refresh = useCallback(async () => {
    try { accept(await api("/api/state")); } catch { /* keep last state */ }
  }, [accept]);

  useEffect(() => {
    let closed = false, retry;
    const open = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws`);
      wsRef.current = ws;
      ws.onopen = () => setConnected(true);
      ws.onmessage = (e) => { try { accept(JSON.parse(e.data)); } catch { /* ignore */ } };
      ws.onclose = () => { setConnected(false); if (!closed) retry = setTimeout(open, 2000); };
      ws.onerror = () => ws.close();
    };
    open();
    refresh();
    const poll = setInterval(() => {               // fallback when the socket is down
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) refresh();
    }, 4000);
    const ping = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) wsRef.current.send("ping");
    }, 20000);
    return () => { closed = true; clearTimeout(retry); clearInterval(poll); clearInterval(ping); wsRef.current?.close(); };
  }, [accept, refresh]);

  const stale = lastUpdate > 0 && Date.now() - lastUpdate > 12000;
  return { state, connected, stale, refresh };
}

/** Readings history, refreshed every 30 s. */
export function useHistory(hours, zone, enabled = true) {
  const [data, setData] = useState(null);
  useEffect(() => {
    if (!enabled) return undefined;
    let alive = true;
    const load = async () => {
      try {
        const q = zone ? `&zone=${zone}` : "";
        const d = await api(`/api/history?hours=${hours}${q}`);
        if (alive) setData(d);
      } catch { /* keep previous */ }
    };
    load();
    const id = setInterval(load, 30000);
    return () => { alive = false; clearInterval(id); };
  }, [hours, zone, enabled]);
  return data;
}

/** Small persistent preference (localStorage may be unavailable - never fatal). */
export function usePref(key, initial) {
  const [v, setV] = useState(() => {
    try { const s = localStorage.getItem(key); return s == null ? initial : JSON.parse(s); } catch { return initial; }
  });
  const set = useCallback((nv) => {
    setV(nv);
    try { localStorage.setItem(key, JSON.stringify(nv)); } catch { /* ignore */ }
  }, [key]);
  return [v, set];
}
