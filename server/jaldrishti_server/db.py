"""SQLite storage (standard library only). One file, WAL mode, safe on an SD card."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts REAL NOT NULL, zone INTEGER NOT NULL, raw REAL, pct REAL);
CREATE INDEX IF NOT EXISTS ix_readings ON readings (zone, ts);

CREATE TABLE IF NOT EXISTS env (
    ts REAL NOT NULL, temp_c REAL, rh_pct REAL);
CREATE INDEX IF NOT EXISTS ix_env ON env (ts);

CREATE TABLE IF NOT EXISTS decisions (
    ts REAL NOT NULL, zone INTEGER NOT NULL, action TEXT, code TEXT,
    seconds REAL, reason_en TEXT, reason_hi TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS ix_decisions ON decisions (ts);

CREATE TABLE IF NOT EXISTS pump_events (
    ts REAL NOT NULL, zone INTEGER NOT NULL, seconds REAL, liters REAL,
    source TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_pump ON pump_events (ts);

CREATE TABLE IF NOT EXISTS zones (
    zone INTEGER PRIMARY KEY, name TEXT, crop TEXT, stage TEXT, soil TEXT,
    depth_mm REAL, raw_dry REAL, raw_wet REAL, calibrated INTEGER DEFAULT 0,
    flow_l_per_min REAL, rain_exposed INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY, value TEXT);
"""

ZONE_FIELDS = ("zone", "name", "crop", "stage", "soil", "depth_mm", "raw_dry", "raw_wet",
               "calibrated", "flow_l_per_min", "rain_exposed", "enabled")


class Database:
    def __init__(self, path: Path | str = settings.DB_PATH):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._seed_zones()

    def _seed_zones(self):
        for z in settings.DEFAULT_ZONES:
            self.conn.execute(
                "INSERT OR IGNORE INTO zones (zone, name, crop, stage, soil, depth_mm, raw_dry, raw_wet,"
                " calibrated, flow_l_per_min, rain_exposed, enabled) VALUES (?,?,?,?,?,?,?,?,0,?,0,1)",
                (z["zone"], z["name"], z["crop"], z["stage"], z["soil"], z["depth_mm"],
                 settings.DEFAULT_RAW_DRY, settings.DEFAULT_RAW_WET, settings.DEFAULT_FLOW_L_PER_MIN))
        # zones never calibrated follow the current default sensor range
        self.conn.execute("UPDATE zones SET raw_dry=?, raw_wet=? WHERE calibrated=0",
                          (settings.DEFAULT_RAW_DRY, settings.DEFAULT_RAW_WET))
        # a database made with more zones (earlier 4-zone layout) keeps only the active ones
        self.conn.execute("DELETE FROM zones WHERE zone > ?", (settings.N_ZONES,))
        self.conn.commit()

    # ---- zones -------------------------------------------------------------------
    def zones(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM zones ORDER BY zone")]

    def update_zone(self, zone: int, **fields) -> None:
        bad = set(fields) - set(ZONE_FIELDS[1:])
        if bad:
            raise ValueError(f"unknown zone fields {bad}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE zones SET {cols} WHERE zone=?", (*fields.values(), zone))
        self.conn.commit()

    # ---- key/value (settings, learning state, weather cache) -----------------------
    def get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def put(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
        self.conn.commit()

    # ---- time series ---------------------------------------------------------------------
    def add_readings(self, rows: list[tuple]) -> None:
        """rows: (ts, zone, raw, pct)"""
        self.conn.executemany("INSERT INTO readings VALUES (?,?,?,?)", rows)
        self.conn.commit()

    def add_env(self, ts, temp_c, rh_pct) -> None:
        self.conn.execute("INSERT INTO env VALUES (?,?,?)", (ts, temp_c, rh_pct))
        self.conn.commit()

    def add_decision(self, ts, zone, d: dict) -> None:
        self.conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?)",
                          (ts, zone, d["action"], d["code"], d.get("seconds"), d["reason_en"],
                           d["reason_hi"], json.dumps(d.get("details", {}))))
        self.conn.commit()

    def add_pump_event(self, ts, zone, seconds, liters, source, note="") -> int:
        cur = self.conn.execute("INSERT INTO pump_events VALUES (?,?,?,?,?,?)",
                                (ts, zone, seconds, liters, source, note))
        self.conn.commit()
        return cur.lastrowid

    def shorten_pump_event(self, rowid: int, seconds: float, liters: float) -> None:
        """A pulse was stopped early: record what was really pumped."""
        self.conn.execute("UPDATE pump_events SET seconds=?, liters=?, note='stopped early' WHERE rowid=?",
                          (seconds, liters, rowid))
        self.conn.commit()

    def readings(self, zone: int, since: float) -> list[tuple[float, float]]:
        return [(r["ts"], r["pct"]) for r in self.conn.execute(
            "SELECT ts, pct FROM readings WHERE zone=? AND ts>=? AND pct IS NOT NULL ORDER BY ts",
            (zone, since))]

    def env_history(self, since: float) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM env WHERE ts>=? ORDER BY ts", (since,))]

    def decisions(self, limit: int = 50, zone: int | None = None) -> list[dict]:
        q = "SELECT * FROM decisions" + (" WHERE zone=?" if zone else "") + " ORDER BY ts DESC LIMIT ?"
        args = (zone, limit) if zone else (limit,)
        out = []
        for r in self.conn.execute(q, args):
            d = dict(r)
            d["details"] = json.loads(d["details"] or "{}")
            out.append(d)
        return out

    def pump_events(self, since: float) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM pump_events WHERE ts>=? ORDER BY ts", (since,))]

    def water_used(self, since: float) -> dict[int, float]:
        rows = self.conn.execute("SELECT zone, SUM(liters) AS l FROM pump_events WHERE ts>=? GROUP BY zone",
                                 (since,))
        return {r["zone"]: r["l"] or 0.0 for r in rows}

    def first_ts(self) -> float | None:
        row = self.conn.execute("SELECT MIN(ts) AS t FROM readings").fetchone()
        return row["t"]

    def prune(self, now: float) -> None:
        cutoff = now - settings.HISTORY_KEEP_DAYS * 86400
        for table in ("readings", "env", "decisions"):
            self.conn.execute(f"DELETE FROM {table} WHERE ts<?", (cutoff,))
        self.conn.commit()

    def close(self):
        self.conn.close()
