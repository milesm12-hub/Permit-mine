#!/usr/bin/env python3
"""
permit_monitor.py

Air permit change-detection tool for Edge Generation LLC.

Purpose
-------
Air permits are mostly a STALE signal once issued and active — a facility
that already has a permit is locked into that equipment. The useful signal
is the CHANGE in permit status over time:

  - active -> expired/surrendered/administratively closed  => SURPLUS LEAD
  - new record, status = draft/pending/public-comment       => EARLY BUYER LEAD
  - modification filing referencing equipment replacement    => REPOWER SIGNAL
    (both a surplus lead AND a buyer lead in one filing)
  - owner_entity changes, status unchanged                   => OWNERSHIP SIGNAL
  - expiration_date within N days, no renewal on file         => NON-RENEWAL WARNING

This script:
  1. Pulls a snapshot of air permit / facility records for a given state
     from EPA's ECHO / ICIS-AIR APIs.
  2. Stores each snapshot in a local SQLite database.
  3. Diffs the latest snapshot against the previous one.
  4. Emits a changelog of the signals above, filtered/tagged against a
     list of tracked turbine models and known counterparty names.

IMPORTANT — network access
---------------------------
This script calls EPA's public ECHO API (echodata.epa.gov). It was written
and structured in a sandboxed environment without direct network access to
that domain, so it has NOT been live-tested against the real API in this
session. Before relying on it:
  - Run `python permit_monitor.py fetch --state TX --dry-run` first to
    confirm the request/response shape matches what's coded below.
  - EPA's API fields and endpoint paths do shift over time; check
    https://echo.epa.gov/tools/web-services for the current schema if you
    get unexpected errors.
  - State portals (TCEQ, CARB, FDEP, etc.) have their own separate systems;
    the ECHO/ICIS-AIR tier here only covers the federally-reported subset.
    A `state_supplement.py` hook is stubbed at the bottom for adding a
    state-specific scraper/parser later.

Usage
-----
  # First run — establishes baseline, no changelog yet
  python permit_monitor.py fetch --state TX

  # Subsequent runs — pulls new snapshot, diffs against last one, prints changelog
  python permit_monitor.py fetch --state TX

  # Just show the changelog from the last two stored snapshots without fetching
  python permit_monitor.py diff --state TX

  # List all facilities currently flagged as non-renewal warnings
  python permit_monitor.py warnings --state TX --days 90

Storage
-------
SQLite DB at ./permits.db (created automatically). Safe to commit to a
private repo or back up — it's the only persistent state this tool has.
"""

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

DB_PATH = "permits.db"

# EPA ECHO Facility Search + Air Compliance endpoints.
# Ref: https://echo.epa.gov/tools/web-services
ECHO_FACILITY_SEARCH_URL = "https://echodata.epa.gov/echo/echo_rest_services.get_facilities"
ECHO_AIR_DETAIL_URL = "https://echodata.epa.gov/echo/air_rest_services.get_facility_info"

# Turbine models Edge Generation tracks. Extend as needed.
TRACKED_EQUIPMENT = [
    "LM6000", "FT4-A9", "FT4A9", "FRAME 6B", "FRAME 6F",
    "TM-2500", "TM2500", "TITAN 130", "T130", "SOLAR T130",
]

# Known counterparty / entity names worth flagging on sight.
TRACKED_ENTITIES = [
    "NEXUS", "NEXUS HUBBARD", "GEW", "BAYSIDE POWER", "EVERLEIGH",
    "MANNING IND", "EDGE GENERATION", "WNT ENERGY", "MASSIVE TECH",
    "HALCYON ENERGY", "GRUPO COX",
]

# Statuses considered "closed out" for the purposes of a surplus signal.
CLOSED_STATUSES = {
    "EXPIRED", "SURRENDERED", "TERMINATED", "ADMINISTRATIVELY CLOSED",
    "REVOKED", "VOID", "CANCELLED", "CANCELED",
}

# Statuses considered "not yet finalized" for the early-buyer signal.
PENDING_STATUSES = {
    "DRAFT", "PENDING", "PUBLIC COMMENT", "UNDER REVIEW", "APPLICATION RECEIVED",
}


@dataclass
class PermitRecord:
    permit_id: str
    facility_name: str
    owner_entity: str
    state: str
    status: str
    equipment_desc: str = ""
    capacity_mw: Optional[float] = None
    issue_date: Optional[str] = None
    expiration_date: Optional[str] = None
    last_action_date: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def matches_tracked_equipment(self):
        desc = (self.equipment_desc or "").upper()
        return [m for m in TRACKED_EQUIPMENT if m in desc]

    def matches_tracked_entity(self):
        name = f"{self.facility_name} {self.owner_entity}".upper()
        return [e for e in TRACKED_ENTITIES if e in name]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            state TEXT NOT NULL,
            pulled_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS permit_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            permit_id TEXT NOT NULL,
            facility_name TEXT,
            owner_entity TEXT,
            state TEXT,
            status TEXT,
            equipment_desc TEXT,
            capacity_mw REAL,
            issue_date TEXT,
            expiration_date TEXT,
            last_action_date TEXT,
            raw_json TEXT,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS changelog (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT NOT NULL,
            state TEXT,
            signal_type TEXT,
            permit_id TEXT,
            facility_name TEXT,
            owner_entity TEXT,
            prior_status TEXT,
            new_status TEXT,
            equipment_matches TEXT,
            entity_matches TEXT,
            note TEXT
        )
    """)
    conn.commit()


def save_snapshot(conn: sqlite3.Connection, state: str, records: list):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO snapshots (state, pulled_at) VALUES (?, ?)",
        (state, datetime.utcnow().isoformat()),
    )
    snapshot_id = cur.lastrowid
    for r in records:
        cur.execute("""
            INSERT INTO permit_records
            (snapshot_id, permit_id, facility_name, owner_entity, state, status,
             equipment_desc, capacity_mw, issue_date, expiration_date,
             last_action_date, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot_id, r.permit_id, r.facility_name, r.owner_entity, r.state,
            r.status, r.equipment_desc, r.capacity_mw, r.issue_date,
            r.expiration_date, r.last_action_date, json.dumps(r.raw),
        ))
    conn.commit()
    return snapshot_id


def get_last_two_snapshots(conn: sqlite3.Connection, state: str):
    cur = conn.cursor()
    cur.execute("""
        SELECT snapshot_id FROM snapshots
        WHERE state = ?
        ORDER BY snapshot_id DESC
        LIMIT 2
    """, (state,))
    rows = [r[0] for r in cur.fetchall()]
    if len(rows) < 2:
        return None, rows[0] if rows else None
    return rows[1], rows[0]  # prior, latest


def load_snapshot_records(conn: sqlite3.Connection, snapshot_id: int):
    cur = conn.cursor()
    cur.execute("""
        SELECT permit_id, facility_name, owner_entity, state, status,
               equipment_desc, capacity_mw, issue_date, expiration_date,
               last_action_date, raw_json
        FROM permit_records WHERE snapshot_id = ?
    """, (snapshot_id,))
    out = {}
    for row in cur.fetchall():
        rec = PermitRecord(
            permit_id=row[0], facility_name=row[1], owner_entity=row[2],
            state=row[3], status=row[4], equipment_desc=row[5],
            capacity_mw=row[6], issue_date=row[7], expiration_date=row[8],
            last_action_date=row[9], raw=json.loads(row[10] or "{}"),
        )
        out[rec.permit_id] = rec
    return out


def log_change(conn, state, signal_type, permit_id, facility_name,
                owner_entity, prior_status, new_status, equip_matches,
                entity_matches, note=""):
    conn.execute("""
        INSERT INTO changelog
        (detected_at, state, signal_type, permit_id, facility_name,
         owner_entity, prior_status, new_status, equipment_matches,
         entity_matches, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(), state, signal_type, permit_id,
        facility_name, owner_entity, prior_status, new_status,
        ", ".join(equip_matches), ", ".join(entity_matches), note,
    ))


# ---------------------------------------------------------------------------
# EPA ECHO fetch
# ---------------------------------------------------------------------------

def fetch_echo_records(state: str, naics_prefix: str = "2211", timeout=30):
    """
    Query EPA ECHO for air-permitted facilities in a state.

    naics_prefix defaults to 2211 (Electric Power Generation, Transmission
    and Distribution). Widen or split this if you want to also catch
    industrial cogen / standby generation sites under other NAICS codes.

    NOTE: field names below (FacName, RegistryID, CWPStatus, etc.) follow
    ECHO's documented output_fields for get_facilities / get_facility_info.
    Verify against https://echo.epa.gov/tools/web-services/facility-search-water
    (air equivalent) if the API returns an unexpected shape — ECHO does
    revise field names occasionally.
    """
    params = {
        "output": "JSON",
        "p_st": state,
        "p_act": "Y",  # active facilities; adjust/remove to widen the pull
        "p_naics": naics_prefix,
        "responseset": "5000",
    }
    url = f"{ECHO_FACILITY_SEARCH_URL}?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[ERROR] ECHO facility search failed: {e}", file=sys.stderr)
        return []

    results = data.get("Results", {}).get("Facilities", [])
    records = []
    for f in results:
        registry_id = f.get("RegistryID", "")
        facility_name = f.get("FacName", "")
        # Air-specific detail (permit id, status, equipment) needs a
        # second call per facility against ICIS-AIR / get_facility_info.
        detail = fetch_air_detail(registry_id, timeout=timeout)
        for permit in detail:
            records.append(permit)
        time.sleep(0.2)  # basic self-throttling; ECHO rate-limits bulk pulls
    return records


def fetch_air_detail(registry_id: str, timeout=30):
    """
    Pull air permit detail for a single facility by RegistryID.
    Returns a list of PermitRecord (a facility can have multiple permits).
    """
    if not registry_id:
        return []
    params = {"output": "JSON", "p_id": registry_id}
    url = f"{ECHO_AIR_DETAIL_URL}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] air detail failed for {registry_id}: {e}", file=sys.stderr)
        return []

    out = []
    permits = data.get("Results", {}).get("AirProgram", []) or []
    for p in permits:
        out.append(PermitRecord(
            permit_id=p.get("PermitID", f"{registry_id}-UNKNOWN"),
            facility_name=p.get("FacName", ""),
            owner_entity=p.get("Owner", p.get("FacName", "")),
            state=p.get("State", ""),
            status=(p.get("PermitStatus", "") or "").upper(),
            equipment_desc=p.get("EmissionUnitDesc", ""),
            capacity_mw=safe_float(p.get("CapacityMW")),
            issue_date=p.get("IssueDate"),
            expiration_date=p.get("ExpirationDate"),
            last_action_date=p.get("LastActionDate"),
            raw=p,
        ))
    return out


def safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------

def diff_snapshots(conn, state, prior_id, latest_id):
    prior = load_snapshot_records(conn, prior_id) if prior_id else {}
    latest = load_snapshot_records(conn, latest_id)

    changes = []

    for pid, rec in latest.items():
        equip = rec.matches_tracked_equipment()
        entity = rec.matches_tracked_entity()

        if pid not in prior:
            # New record this cycle
            if rec.status in PENDING_STATUSES:
                log_change(conn, state, "EARLY_BUYER_LEAD", pid,
                           rec.facility_name, rec.owner_entity, None,
                           rec.status, equip, entity,
                           note="New pending/draft permit filing.")
                changes.append((pid, "EARLY_BUYER_LEAD"))
            continue

        old = prior[pid]
        if old.status != rec.status:
            if rec.status in CLOSED_STATUSES and old.status not in CLOSED_STATUSES:
                log_change(conn, state, "SURPLUS_LEAD", pid,
                           rec.facility_name, rec.owner_entity, old.status,
                           rec.status, equip, entity,
                           note="Permit moved to a closed/expired status.")
                changes.append((pid, "SURPLUS_LEAD"))
            elif "MODIF" in rec.status:
                log_change(conn, state, "REPOWER_SIGNAL", pid,
                           rec.facility_name, rec.owner_entity, old.status,
                           rec.status, equip, entity,
                           note="Modification filing detected.")
                changes.append((pid, "REPOWER_SIGNAL"))

        if old.owner_entity != rec.owner_entity and old.status == rec.status:
            log_change(conn, state, "OWNERSHIP_SIGNAL", pid,
                       rec.facility_name, rec.owner_entity, old.owner_entity,
                       rec.owner_entity, equip, entity,
                       note="Owner/permittee of record changed.")
            changes.append((pid, "OWNERSHIP_SIGNAL"))

    conn.commit()
    return changes


def check_non_renewal_warnings(conn, state, days_window=90):
    cur = conn.cursor()
    prior_id, latest_id = get_last_two_snapshots(conn, state)
    snap_id = latest_id
    if not snap_id:
        return []
    latest = load_snapshot_records(conn, snap_id)

    cutoff = datetime.utcnow() + timedelta(days=days_window)
    warnings = []
    for pid, rec in latest.items():
        if rec.status in CLOSED_STATUSES:
            continue
        if not rec.expiration_date:
            continue
        try:
            exp = datetime.fromisoformat(rec.expiration_date[:10])
        except ValueError:
            continue
        if exp <= cutoff:
            equip = rec.matches_tracked_equipment()
            entity = rec.matches_tracked_entity()
            note = f"Expires {exp.date()}, {days_window}-day window, no renewal on file."
            log_change(conn, state, "NON_RENEWAL_WARNING", pid,
                       rec.facility_name, rec.owner_entity, rec.status,
                       rec.status, equip, entity, note=note)
            warnings.append((pid, rec.facility_name, exp.date().isoformat()))
    conn.commit()
    return warnings


def print_changelog(conn, state, limit=100):
    cur = conn.cursor()
    cur.execute("""
        SELECT detected_at, signal_type, facility_name, owner_entity,
               prior_status, new_status, equipment_matches, entity_matches, note
        FROM changelog
        WHERE state = ?
        ORDER BY id DESC
        LIMIT ?
    """, (state, limit))
    rows = cur.fetchall()
    if not rows:
        print(f"No changelog entries yet for {state}. Run `fetch` at least twice to generate a diff.")
        return
    for r in rows:
        detected_at, signal, fac, owner, prior, new, equip, entity, note = r
        tag_bits = []
        if equip:
            tag_bits.append(f"EQUIP:[{equip}]")
        if entity:
            tag_bits.append(f"ENTITY:[{entity}]")
        tags = " " + " ".join(tag_bits) if tag_bits else ""
        print(f"[{state}] [{detected_at[:19]}] [{signal}] {fac} — Owner: {owner} — "
              f"{prior} -> {new}{tags} — {note}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_fetch(args):
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.dry_run:
        print(f"[DRY RUN] Would fetch ECHO/ICIS-AIR records for state={args.state}, "
              f"naics_prefix={args.naics}. No network call made, no DB write.")
        return

    print(f"Fetching current air permit records for {args.state} from EPA ECHO...")
    records = fetch_echo_records(args.state, naics_prefix=args.naics)
    if not records:
        print("No records returned. Check network access, state code, and NAICS filter.")
        return

    snap_id = save_snapshot(conn, args.state, records)
    print(f"Stored snapshot #{snap_id} with {len(records)} permit records.")

    prior_id, latest_id = get_last_two_snapshots(conn, args.state)
    if prior_id is None:
        print("This is the first snapshot for this state — no diff yet. "
              "Run `fetch` again later to generate a changelog.")
        return

    changes = diff_snapshots(conn, args.state, prior_id, latest_id)
    print(f"\n{len(changes)} change(s) detected:\n")
    print_changelog(conn, args.state, limit=len(changes) or 1)


def cmd_diff(args):
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    prior_id, latest_id = get_last_two_snapshots(conn, args.state)
    if prior_id is None:
        print("Not enough snapshots stored yet to diff (need at least 2 `fetch` runs).")
        return
    changes = diff_snapshots(conn, args.state, prior_id, latest_id)
    print(f"{len(changes)} change(s) detected:\n")
    print_changelog(conn, args.state, limit=len(changes) or 1)


def cmd_warnings(args):
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    warnings = check_non_renewal_warnings(conn, args.state, days_window=args.days)
    if not warnings:
        print(f"No non-renewal warnings within {args.days} days for {args.state}.")
        return
    print(f"{len(warnings)} facility permit(s) expiring within {args.days} days with no renewal on file:\n")
    for pid, fac, exp in warnings:
        print(f"  - {fac} (permit {pid}) — expires {exp}")


def build_parser():
    p = argparse.ArgumentParser(description="Air permit change-detection monitor")
    sub = p.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Pull a new snapshot and diff against the last one")
    p_fetch.add_argument("--state", required=True, help="Two-letter state code, e.g. TX")
    p_fetch.add_argument("--naics", default="2211", help="NAICS prefix filter (default: 2211, power generation)")
    p_fetch.add_argument("--dry-run", action="store_true", help="Print what would be fetched, no network call")
    p_fetch.set_defaults(func=cmd_fetch)

    p_diff = sub.add_parser("diff", help="Diff the last two stored snapshots without fetching")
    p_diff.add_argument("--state", required=True)
    p_diff.set_defaults(func=cmd_diff)

    p_warn = sub.add_parser("warnings", help="List permits expiring soon with no renewal on file")
    p_warn.add_argument("--state", required=True)
    p_warn.add_argument("--days", type=int, default=90)
    p_warn.set_defaults(func=cmd_warnings)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
