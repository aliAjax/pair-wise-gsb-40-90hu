"""Maritime search-and-rescue coordination service."""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "maritime_sar.db"
ACTIVE_INCIDENT = {"reported", "coordinating", "recovering"}
CLOSED_INCIDENT = {"closed", "cancelled", "duplicate"}
ENDED_AREA = {"completed", "abandoned"}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def destination_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> tuple[float, float]:
    """Return the point reached by sailing a bearing/distance from the origin."""
    radius = 6371.0088
    b = math.radians(bearing_deg)
    d = distance_km / radius
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), ((math.degrees(l2) + 540) % 360) - 180


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def validate_position(lat: Any, lon: Any) -> tuple[float, float]:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError) as exc:
        raise DomainError("经纬度必须是数值") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise DomainError("经纬度超出有效范围")
    return lat, lon


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class MaritimeSARService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    vessel_name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    uncertainty_km REAL NOT NULL,
                    drift_direction REAL NOT NULL DEFAULT 0,
                    drift_speed_kn REAL NOT NULL DEFAULT 0,
                    sea_state INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reported',
                    lead_org TEXT NOT NULL,
                    duplicate_of INTEGER REFERENCES incidents(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    capabilities TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    speed_kn REAL NOT NULL,
                    range_km REAL NOT NULL,
                    max_sea_state INTEGER NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS search_areas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    code TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    radius_km REAL NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL DEFAULT 'planned',
                    assigned_asset_id INTEGER REFERENCES assets(id),
                    note TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    area_id INTEGER REFERENCES search_areas(id),
                    client_event_id TEXT NOT NULL UNIQUE,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'unverified',
                    distance_from_incident_km REAL NOT NULL,
                    reporter TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL,
                    merged_at TEXT
                );
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_batch_id TEXT NOT NULL UNIQUE,
                    actor TEXT NOT NULL,
                    status TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    merged_at TEXT,
                    summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER REFERENCES incidents(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    area_id INTEGER NOT NULL REFERENCES search_areas(id),
                    code TEXT NOT NULL UNIQUE,
                    sequence INTEGER NOT NULL,
                    bearing_start REAL NOT NULL,
                    bearing_end REAL NOT NULL,
                    center_lat REAL NOT NULL,
                    center_lon REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    covered_asset_id INTEGER REFERENCES assets(id),
                    covered_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(area_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS sector_coverage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sector_id INTEGER NOT NULL REFERENCES sectors(id),
                    area_id INTEGER NOT NULL REFERENCES search_areas(id),
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    client_event_id TEXT NOT NULL UNIQUE,
                    asset_id INTEGER REFERENCES assets(id),
                    actor TEXT NOT NULL,
                    latitude REAL,
                    longitude REAL,
                    sea_state INTEGER,
                    distance_km REAL,
                    source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'accepted',
                    batch_id TEXT,
                    reported_at TEXT NOT NULL,
                    accepted_at TEXT
                );
                CREATE TABLE IF NOT EXISTS review_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER REFERENCES incidents(id),
                    area_id INTEGER REFERENCES search_areas(id),
                    sector_id INTEGER REFERENCES sectors(id),
                    coverage_id INTEGER REFERENCES sector_coverage(id),
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    details TEXT NOT NULL DEFAULT '',
                    resolved_by TEXT,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_clues_incident ON clues(incident_id, recorded_at);
                CREATE INDEX IF NOT EXISTS idx_timeline_incident ON timeline(incident_id, id);
                CREATE INDEX IF NOT EXISTS idx_sectors_area ON sectors(area_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_coverage_sector ON sector_coverage(sector_id, status);
                CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status, id);
                """
            )

    def _audit(self, conn: sqlite3.Connection, incident_id: int | None, actor: str, action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(incident_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (incident_id, actor, action, json_dump(details), utcnow()),
        )

    def create_incident(self, actor: str, role: str, code: str, vessel_name: str,
                        latitude: float, longitude: float, uncertainty_km: float,
                        sea_state: int, lead_org: str, drift_direction: float = 0,
                        drift_speed_kn: float = 0, description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "operator"}, "创建遇险事件")
        code, vessel_name, lead_org = code.strip(), vessel_name.strip(), lead_org.strip()
        if not code or not vessel_name or not lead_org:
            raise DomainError("事件编号、船名和负责机构不能为空")
        lat, lon = validate_position(latitude, longitude)
        try:
            uncertainty_km = float(uncertainty_km)
            sea_state = int(sea_state)
            drift_direction = float(drift_direction)
            drift_speed_kn = float(drift_speed_kn)
        except (TypeError, ValueError) as exc:
            raise DomainError("不确定半径、海况和漂移参数必须是数值") from exc
        if uncertainty_km <= 0 or uncertainty_km > 1000:
            raise DomainError("不确定半径应在 0 到 1000 公里之间")
        if not 0 <= sea_state <= 9 or drift_speed_kn < 0:
            raise DomainError("海况或漂移速度无效")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            duplicate = conn.execute(
                "SELECT * FROM incidents WHERE vessel_name=? AND status IN ('reported','coordinating','recovering') ORDER BY id DESC",
                (vessel_name,),
            ).fetchall()
            duplicate_of = None
            for row in duplicate:
                if haversine_km(lat, lon, row["latitude"], row["longitude"]) <= max(20.0, uncertainty_km + row["uncertainty_km"]):
                    duplicate_of = row["id"]
                    break
            status = "duplicate" if duplicate_of else "reported"
            try:
                cur = conn.execute(
                    """INSERT INTO incidents(code,vessel_name,description,latitude,longitude,uncertainty_km,
                       drift_direction,drift_speed_kn,sea_state,status,lead_org,duplicate_of,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (code, vessel_name, description.strip(), lat, lon, uncertainty_km, drift_direction, drift_speed_kn,
                     sea_state, status, lead_org, duplicate_of, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("事件编号已存在", 409) from exc
            incident_id = int(cur.lastrowid)
            self._audit(conn, incident_id, actor, "incident.reported", {"duplicate_of": duplicate_of})
            if duplicate_of:
                self._audit(conn, duplicate_of, actor, "incident.duplicate_detected", {"duplicate_incident": code})
            return dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def list_assets(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM assets ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def add_asset(self, actor: str, role: str, name: str, kind: str,
                  capabilities: list[str], latitude: float, longitude: float,
                  speed_kn: float, range_km: float, max_sea_state: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "登记搜救资源")
        lat, lon = validate_position(latitude, longitude)
        name, kind = name.strip(), kind.strip()
        caps = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        if not name or not kind or not caps:
            raise DomainError("资源名称、类型和能力不能为空")
        try:
            speed_kn, range_km, max_sea_state = float(speed_kn), float(range_km), int(max_sea_state)
        except (TypeError, ValueError) as exc:
            raise DomainError("速度和航程参数必须是数值") from exc
        if speed_kn <= 0 or range_km <= 0 or not 0 <= max_sea_state <= 9:
            raise DomainError("速度、航程或适用海况无效")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO assets(name,kind,capabilities,latitude,longitude,speed_kn,range_km,max_sea_state,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (name, kind, json_dump(caps), lat, lon, speed_kn, range_km, max_sea_state, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("资源名称已存在", 409) from exc
            self._audit(conn, None, actor, "asset.registered", {"asset_id": cur.lastrowid, "name": name})
            return dict(conn.execute("SELECT * FROM assets WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_search_area(self, actor: str, role: str, incident_id: int, code: str,
                           kind: str, center_lat: float, center_lon: float,
                           radius_km: float, priority: int = 3, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "创建搜索区域")
        lat, lon = validate_position(center_lat, center_lon)
        kind, code = kind.strip(), code.strip()
        if not kind or not code:
            raise DomainError("区域类型和编号不能为空")
        try:
            radius_km, priority = float(radius_km), int(priority)
        except (TypeError, ValueError) as exc:
            raise DomainError("半径和优先级必须是数值") from exc
        if radius_km <= 0 or not 1 <= priority <= 5:
            raise DomainError("搜索半径或优先级无效")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("当前事件不能创建搜索区域", 409)
            try:
                cur = conn.execute(
                    """INSERT INTO search_areas(incident_id,code,kind,center_lat,center_lon,radius_km,priority,note,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (incident_id, code, kind, lat, lon, radius_km, priority, note.strip(), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("搜索区域编号已存在", 409) from exc
            self._audit(conn, incident_id, actor, "area.created", {"area_id": cur.lastrowid, "code": code})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign_area(self, actor: str, role: str, area_id: int, asset_id: int,
                    expected_asset_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "分配搜索任务")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not area or not asset:
                raise DomainError("搜索区域或资源不存在", 404)
            if area["assigned_asset_id"] is not None:
                raise DomainError("搜索区域已经分配", 409)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (area["incident_id"],)).fetchone()
            if not incident or incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("事件当前不可分配", 409)
            if expected_asset_version is not None and asset["version"] != int(expected_asset_version):
                raise DomainError("资源状态已变化，请刷新后重试", 409)
            if asset["status"] != "available":
                raise DomainError("资源当前不可用", 409)
            if incident["sea_state"] > asset["max_sea_state"]:
                raise DomainError("海况超出资源能力", 409)
            capabilities = json.loads(asset["capabilities"])
            if area["kind"] not in capabilities:
                raise DomainError("资源不具备该搜索区域能力", 409)
            distance = haversine_km(asset["latitude"], asset["longitude"], area["center_lat"], area["center_lon"])
            if distance > asset["range_km"]:
                raise DomainError("搜索区域超出资源航程", 409)
            now = utcnow()
            changed = conn.execute(
                "UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available' AND version=?",
                (now, asset_id, asset["version"]),
            )
            if changed.rowcount != 1:
                raise DomainError("资源已被其他任务占用", 409)
            conn.execute(
                "UPDATE search_areas SET assigned_asset_id=?,status='assigned',version=version+1,updated_at=? WHERE id=?",
                (asset_id, now, area_id),
            )
            self._audit(conn, area["incident_id"], actor, "area.assigned", {"area_id": area_id, "asset_id": asset_id, "distance_km": round(distance, 2)})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone())

    def record_clue(self, actor: str, role: str, incident_id: int, client_event_id: str,
                    latitude: float, longitude: float, confidence: float, source: str,
                    area_id: int | None = None, details: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "operator", "field"}, "记录搜索线索")
        lat, lon = validate_position(latitude, longitude)
        event_id, source = client_event_id.strip(), source.strip()
        if not event_id or not source:
            raise DomainError("事件幂等编号和线索来源不能为空")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError) as exc:
            raise DomainError("线索置信度必须是数值") from exc
        if not 0 <= confidence <= 1:
            raise DomainError("置信度应在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM clues WHERE client_event_id=?", (event_id,)).fetchone()
            if existing:
                return dict(existing)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] in CLOSED_INCIDENT:
                raise DomainError("已结束事件不能新增线索", 409)
            if area_id is not None:
                area = conn.execute("SELECT * FROM search_areas WHERE id=? AND incident_id=?", (area_id, incident_id)).fetchone()
                if not area:
                    raise DomainError("搜索区域不属于该事件", 409)
            distance = haversine_km(incident["latitude"], incident["longitude"], lat, lon)
            status = "unverified" if distance <= incident["uncertainty_km"] * 3 else "invalid"
            cur = conn.execute(
                """INSERT INTO clues(incident_id,area_id,client_event_id,latitude,longitude,confidence,source,status,
                   distance_from_incident_km,reporter,details,recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (incident_id, area_id, event_id, lat, lon, confidence, source, status, distance, actor, details.strip(), utcnow()),
            )
            self._audit(conn, incident_id, actor, "clue.recorded", {"clue_id": cur.lastrowid, "status": status, "event_id": event_id})
            return dict(conn.execute("SELECT * FROM clues WHERE id=?", (cur.lastrowid,)).fetchone())

    def verify_clue(self, actor: str, role: str, clue_id: int, status: str,
                    expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "analyst"}, "核验线索")
        if status not in {"verified", "rejected", "unverified"}:
            raise DomainError("线索状态无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clue = conn.execute("SELECT * FROM clues WHERE id=?", (clue_id,)).fetchone()
            if not clue:
                raise DomainError("线索不存在", 404)
            if status == "verified" and clue["status"] == "invalid" and role != "coordinator":
                raise DomainError("异常位置线索只能由协调员确认", 403)
            conn.execute("UPDATE clues SET status=?,merged_at=? WHERE id=?", (status, utcnow(), clue_id))
            self._audit(conn, clue["incident_id"], actor, "clue.reviewed", {"clue_id": clue_id, "status": status})
            return dict(conn.execute("SELECT * FROM clues WHERE id=?", (clue_id,)).fetchone())

    def withdraw_asset(self, actor: str, role: str, asset_id: int, reason: str,
                       expected_asset_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "撤回资源")
        if not reason.strip():
            raise DomainError("撤回原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not asset:
                raise DomainError("资源不存在", 404)
            if asset["version"] != int(expected_asset_version):
                raise DomainError("资源状态已变化，请刷新后重试", 409)
            if asset["status"] == "available":
                raise DomainError("资源当前未分配", 409)
            now = utcnow()
            areas = conn.execute("SELECT id,incident_id FROM search_areas WHERE assigned_asset_id=? AND status IN ('assigned','active')", (asset_id,)).fetchall()
            for area in areas:
                conn.execute("UPDATE search_areas SET assigned_asset_id=NULL,status='planned',version=version+1,updated_at=? WHERE id=?", (now, area["id"]))
                self._audit(conn, area["incident_id"], actor, "area.unassigned", {"area_id": area["id"], "reason": reason.strip()})
            conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?", (now, asset_id))
            self._audit(conn, None, actor, "asset.withdrawn", {"asset_id": asset_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone())

    def transfer_incident(self, actor: str, role: str, incident_id: int, new_org: str,
                          expected_version: int, note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "移交事件")
        new_org = new_org.strip()
        if not new_org:
            raise DomainError("接收机构不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["status"] in CLOSED_INCIDENT:
                raise DomainError("已结束事件不能移交", 409)
            if incident["version"] != int(expected_version):
                raise DomainError("事件已变化，请刷新后重试", 409)
            conn.execute(
                "UPDATE incidents SET lead_org=?,version=version+1,updated_at=? WHERE id=? AND version=?",
                (new_org, utcnow(), incident_id, expected_version),
            )
            self._audit(conn, incident_id, actor, "incident.transferred", {"from": incident["lead_org"], "to": new_org, "note": note.strip()})
            return dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def complete_area(self, actor: str, role: str, area_id: int, outcome: str,
                      expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "结束搜索区域")
        if outcome not in {"completed", "abandoned"}:
            raise DomainError("区域结束结论无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            if not area:
                raise DomainError("搜索区域不存在", 404)
            if area["status"] in {"completed", "abandoned"}:
                raise DomainError("搜索区域已经结束", 409)
            if expected_version is not None and area["version"] != int(expected_version):
                raise DomainError("搜索区域已变化，请刷新后重试", 409)
            if area["assigned_asset_id"] is not None:
                conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?", (utcnow(), area["assigned_asset_id"]))
            conn.execute("UPDATE search_areas SET status=?,assigned_asset_id=NULL,version=version+1,updated_at=? WHERE id=?", (outcome, utcnow(), area_id))
            self._audit(conn, area["incident_id"], actor, "area." + outcome, {"area_id": area_id})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone())

    def split_area(self, actor: str, role: str, area_id: int, sector_count: int,
                   expected_version: int | None = None) -> list[dict[str, Any]]:
        """Split a search area into adjacent sectors (even bearing wedges)."""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "拆分搜索扇区")
        try:
            sector_count = int(sector_count)
        except (TypeError, ValueError) as exc:
            raise DomainError("扇区数量必须是整数") from exc
        if not 1 <= sector_count <= 36:
            raise DomainError("扇区数量应在 1 到 36 之间")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            if not area:
                raise DomainError("搜索区域不存在", 404)
            if expected_version is not None and area["version"] != int(expected_version):
                raise DomainError("搜索区域已变化，请刷新后重试", 409)
            existing = conn.execute("SELECT COUNT(*) AS c FROM sectors WHERE area_id=?", (area_id,)).fetchone()["c"]
            if existing:
                raise DomainError("搜索区域已拆分，不能重复拆分", 409)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (area["incident_id"],)).fetchone()
            if not incident or incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("当前事件不能拆分搜索区域", 409)
            width = 360.0 / sector_count
            created = []
            for sequence in range(sector_count):
                bearing_start = round(sequence * width, 4)
                bearing_end = round((sequence + 1) * width, 4)
                center_lat, center_lon = destination_point(
                    area["center_lat"], area["center_lon"], bearing_start + width / 2, area["radius_km"] * 0.5
                )
                code = "%s-S%02d" % (area["code"], sequence + 1)
                cur = conn.execute(
                    """INSERT INTO sectors(area_id,code,sequence,bearing_start,bearing_end,center_lat,center_lon,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (area_id, code, sequence + 1, bearing_start, bearing_end, center_lat, center_lon, now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM sectors WHERE id=?", (cur.lastrowid,)).fetchone()))
            conn.execute("UPDATE search_areas SET version=version+1,updated_at=? WHERE id=?", (now, area_id))
            self._audit(conn, area["incident_id"], actor, "area.split",
                        {"area_id": area_id, "sector_count": sector_count, "width_deg": round(width, 2)})
            return created

    def _locate_sector(self, conn: sqlite3.Connection, incident_id: int,
                       area_id: int, sector_id: int | None, sequence: int | None) -> sqlite3.Row:
        if sector_id is not None:
            sector = conn.execute(
                """SELECT s.* FROM sectors s JOIN search_areas a ON a.id=s.area_id
                   WHERE s.id=? AND a.incident_id=?""",
                (sector_id, incident_id),
            ).fetchone()
        else:
            sector = conn.execute(
                """SELECT s.* FROM sectors s JOIN search_areas a ON a.id=s.area_id
                   WHERE a.id=? AND s.sequence=? AND a.incident_id=?""",
                (area_id, sequence, incident_id),
            ).fetchone()
        if not sector:
            raise DomainError("扇区不存在或不属于该事件", 404)
        return sector

    def _add_review(self, conn: sqlite3.Connection, incident_id: int, area_id: int,
                    sector_id: int, kind: str, reason: str, details: dict[str, Any],
                    coverage_id: int | None = None) -> int:
        cur = conn.execute(
            """INSERT INTO review_items(incident_id,area_id,sector_id,coverage_id,kind,reason,details,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (incident_id, area_id, sector_id, coverage_id, kind, reason, json_dump(details), utcnow()),
        )
        return int(cur.lastrowid)

    def _refresh_sector_status(self, conn: sqlite3.Connection, sector_id: int, now: str) -> None:
        accepted = conn.execute(
            "SELECT COUNT(*) AS c, MIN(reported_at) AS first_at, asset_id FROM sector_coverage WHERE sector_id=? AND status='accepted'",
            (sector_id,),
        ).fetchone()
        if accepted["c"]:
            conn.execute(
                "UPDATE sectors SET status='covered',covered_at=COALESCE(covered_at,?),covered_asset_id=COALESCE(covered_asset_id,?),version=version+1,updated_at=? WHERE id=?",
                (accepted["first_at"], accepted["asset_id"], now, sector_id),
            )
        else:
            conn.execute(
                "UPDATE sectors SET status='pending',covered_at=NULL,covered_asset_id=NULL,version=version+1,updated_at=? WHERE id=?",
                (now, sector_id),
            )

    def _apply_coverage_report(self, conn: sqlite3.Connection, incident_id: int, client_event_id: str,
                               actor: str, area_id: int | None, sector_id: int | None, sequence: int | None,
                               asset_id: int, latitude: Any, longitude: Any, source: str,
                               batch_id: str | None) -> dict[str, Any]:
        """Idempotently ingest one coverage report; unqualified reports only queue for review."""
        event_id = str(client_event_id or "").strip()
        if not event_id:
            raise DomainError("覆盖上报缺少 client_event_id")
        duplicate = conn.execute("SELECT * FROM sector_coverage WHERE client_event_id=?", (event_id,)).fetchone()
        if duplicate:
            return {"client_event_id": event_id, "idempotent": True, "coverage_id": duplicate["id"],
                    "status": duplicate["status"], "sector_id": duplicate["sector_id"],
                    "area_id": duplicate["area_id"], "incident_id": duplicate["incident_id"]}
        incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
        if not incident:
            raise DomainError("事件不存在", 404)
        area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone() if area_id is not None else None
        if not area or area["incident_id"] != incident_id:
            raise DomainError("搜索区域不存在或不属于该事件", 404)
        asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset:
            raise DomainError("搜救资源不存在", 404)
        sector = self._locate_sector(conn, incident_id, area["id"], sector_id, sequence)
        if latitude is not None and longitude is not None:
            lat, lon = validate_position(latitude, longitude)
        else:
            lat, lon = asset["latitude"], asset["longitude"]
        distance = haversine_km(asset["latitude"], asset["longitude"], sector["center_lat"], sector["center_lon"])
        now = utcnow()
        qualified = True
        reasons: list[str] = []
        if incident["sea_state"] > asset["max_sea_state"]:
            qualified = False
            reasons.append("海况超出资源能力")
        if distance > asset["range_km"]:
            qualified = False
            reasons.append("扇区超出资源航程")
        status = "accepted" if qualified else "pending_review"
        cur = conn.execute(
            """INSERT INTO sector_coverage(sector_id,area_id,incident_id,client_event_id,asset_id,actor,
               latitude,longitude,sea_state,distance_km,source,status,batch_id,reported_at,accepted_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (sector["id"], area["id"], incident_id, event_id, asset_id, actor, lat, lon,
             incident["sea_state"], round(distance, 2), str(source or "").strip(), status, batch_id, now,
             now if qualified else None),
        )
        coverage_id = int(cur.lastrowid)
        result = {"client_event_id": event_id, "idempotent": False, "coverage_id": coverage_id,
                  "status": status, "sector_id": sector["id"], "area_id": area["id"]}
        if qualified:
            self._refresh_sector_status(conn, sector["id"], now)
        else:
            review_id = self._add_review(
                conn, incident_id, area["id"], sector["id"], "coverage_unqualified", "；".join(reasons),
                {"event_id": event_id, "asset_id": asset_id, "distance_km": round(distance, 2),
                 "sea_state": incident["sea_state"], "max_sea_state": asset["max_sea_state"]},
                coverage_id,
            )
            result["review_id"] = review_id
        if area["status"] in ENDED_AREA:
            review_id = self._add_review(
                conn, incident_id, area["id"], sector["id"], "ended_area_coverage",
                "覆盖上报到达时区域已结束：%s" % area["status"],
                {"event_id": event_id, "coverage_id": coverage_id, "qualified": qualified,
                 "area_status": area["status"], "applied": qualified},
                coverage_id,
            )
            result["conflict_review_id"] = review_id
            result["ended_area"] = True
        self._audit(conn, incident_id, actor, "sector.coverage_reported",
                    {"sector_id": sector["id"], "event_id": event_id, "status": status, "batch_id": batch_id})
        return result

    def report_sector_coverage(self, actor: str, role: str, incident_id: int, client_event_id: str,
                               asset_id: int, area_id: int | None = None, sector_id: int | None = None,
                               sequence: int | None = None, latitude: float | None = None,
                               longitude: float | None = None, source: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "operator", "field"}, "上报扇区覆盖")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = self._apply_coverage_report(
                conn, int(incident_id), client_event_id, actor, int(area_id) if area_id is not None else None,
                int(sector_id) if sector_id is not None else None,
                int(sequence) if sequence is not None else None,
                int(asset_id), latitude, longitude, source, None,
            )
            recomputed = self._recompute_area_coverage(conn, result["area_id"], actor, force=True)
            if recomputed:
                result["area_coverage"] = recomputed
            return result

    def _recompute_area_coverage(self, conn: sqlite3.Connection, area_id: int, actor: str,
                                 force: bool = False) -> dict[str, Any] | None:
        area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
        if not area:
            return None
        if not force and area["status"] not in ENDED_AREA:
            return None
        total = conn.execute("SELECT COUNT(*) AS c FROM sectors WHERE area_id=?", (area_id,)).fetchone()["c"]
        if not total:
            return None
        covered = conn.execute(
            "SELECT COUNT(DISTINCT s.id) AS c FROM sectors s JOIN sector_coverage c ON c.sector_id=s.id AND c.status='accepted' WHERE s.area_id=?",
            (area_id,),
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM review_items WHERE area_id=? AND status='pending'", (area_id,)
        ).fetchone()["c"]
        stats = {"area_id": area_id, "total_sectors": total, "covered_sectors": covered,
                 "coverage_ratio": round(covered / total, 4), "pending_reviews": pending}
        self._audit(conn, area["incident_id"], actor, "area.coverage_recomputed", stats)
        return stats

    def reassign_area(self, actor: str, role: str, area_id: int, asset_id: int,
                      expected_area_version: int) -> dict[str, Any]:
        """Reassign an already assigned area to another asset (optimistic lock on the area)."""
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "改派搜索任务")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            area = conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone()
            asset = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not area or not asset:
                raise DomainError("搜索区域或资源不存在", 404)
            if area["version"] != int(expected_area_version):
                raise DomainError("搜索区域已被其他协调员改派，请刷新后重试", 409)
            if area["status"] in ENDED_AREA:
                raise DomainError("搜索区域已经结束，不能改派", 409)
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (area["incident_id"],)).fetchone()
            if not incident or incident["status"] not in ACTIVE_INCIDENT:
                raise DomainError("事件当前不可分配", 409)
            old_asset_id = area["assigned_asset_id"]
            if old_asset_id is None:
                raise DomainError("搜索区域尚未分配，请使用分配接口", 409)
            if old_asset_id == asset_id:
                raise DomainError("新资源与当前资源相同，无需改派", 409)
            if asset["status"] != "available":
                raise DomainError("资源当前不可用", 409)
            if incident["sea_state"] > asset["max_sea_state"]:
                raise DomainError("海况超出资源能力", 409)
            capabilities = json.loads(asset["capabilities"])
            if area["kind"] not in capabilities:
                raise DomainError("资源不具备该搜索区域能力", 409)
            distance = haversine_km(asset["latitude"], asset["longitude"], area["center_lat"], area["center_lon"])
            if distance > asset["range_km"]:
                raise DomainError("搜索区域超出资源航程", 409)
            claimed = conn.execute(
                "UPDATE assets SET status='assigned',version=version+1,updated_at=? WHERE id=? AND status='available'",
                (now, asset_id),
            )
            if claimed.rowcount != 1:
                raise DomainError("资源已被其他任务占用", 409)
            moved = conn.execute(
                """UPDATE search_areas SET assigned_asset_id=?,status='assigned',version=version+1,updated_at=?
                   WHERE id=? AND version=? AND assigned_asset_id=?""",
                (asset_id, now, area_id, expected_area_version, old_asset_id),
            )
            if moved.rowcount != 1:
                raise DomainError("搜索区域已被其他协调员改派，请刷新后重试", 409)
            conn.execute("UPDATE assets SET status='available',version=version+1,updated_at=? WHERE id=?", (now, old_asset_id))
            self._audit(conn, area["incident_id"], actor, "area.reassigned",
                        {"area_id": area_id, "from_asset_id": old_asset_id, "to_asset_id": asset_id,
                         "distance_km": round(distance, 2)})
            return dict(conn.execute("SELECT * FROM search_areas WHERE id=?", (area_id,)).fetchone())

    def list_review_items(self, status: str = "pending") -> list[dict[str, Any]]:
        if status not in {"pending", "resolved", "all"}:
            raise DomainError("待核清单状态无效")
        query = "SELECT * FROM review_items"
        params: tuple[Any, ...] = ()
        if status != "all":
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY id DESC LIMIT 500"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def resolve_review_item(self, actor: str, role: str, review_id: int, decision: str,
                            note: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator", "analyst"}, "复核待核项")
        if decision not in {"confirmed", "rejected"}:
            raise DomainError("复核结论无效")
        now = utcnow()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT * FROM review_items WHERE id=?", (review_id,)).fetchone()
            if not item:
                raise DomainError("待核项不存在", 404)
            if item["status"] != "pending":
                raise DomainError("待核项已处理", 409)
            conn.execute(
                "UPDATE review_items SET status=?,resolved_by=?,resolved_at=?,details=? WHERE id=?",
                ("confirmed" if decision == "confirmed" else "rejected", actor, now,
                 json_dump({"note": note.strip(), "original": item["details"]}), review_id),
            )
            coverage = conn.execute("SELECT * FROM sector_coverage WHERE id=?", (item["coverage_id"],)).fetchone() if item["coverage_id"] else None
            if coverage and item["kind"] == "coverage_unqualified":
                if decision == "confirmed":
                    conn.execute("UPDATE sector_coverage SET status='accepted',accepted_at=? WHERE id=? AND status='pending_review'", (now, coverage["id"]))
                    self._refresh_sector_status(conn, coverage["sector_id"], now)
                else:
                    conn.execute("UPDATE sector_coverage SET status='rejected' WHERE id=? AND status='pending_review'", (coverage["id"],))
                    self._refresh_sector_status(conn, coverage["sector_id"], now)
            self._audit(conn, item["incident_id"], actor, "review.resolved",
                        {"review_id": review_id, "kind": item["kind"], "decision": decision})
            stats = self._recompute_area_coverage(conn, item["area_id"], actor, force=True) if item["area_id"] else None
            result = dict(conn.execute("SELECT * FROM review_items WHERE id=?", (review_id,)).fetchone())
            if stats:
                result["area_coverage"] = stats
            return result

    def close_incident(self, actor: str, role: str, incident_id: int, outcome: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"coordinator"}, "结束事件")
        if outcome not in {"resolved", "cancelled", "false_alarm"}:
            raise DomainError("结束结论无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if not incident:
                raise DomainError("事件不存在", 404)
            if incident["version"] != int(expected_version):
                raise DomainError("事件已变化，请刷新后重试", 409)
            active_area = conn.execute(
                "SELECT COUNT(*) AS c FROM search_areas WHERE incident_id=? AND status IN ('planned','assigned','active')",
                (incident_id,),
            ).fetchone()["c"]
            if active_area and outcome != "false_alarm":
                raise DomainError("仍有未结束搜索区域，不能关闭事件", 409)
            status = "closed" if outcome == "resolved" else "cancelled"
            conn.execute("UPDATE incidents SET status=?,version=version+1,updated_at=? WHERE id=?", (status, utcnow(), incident_id))
            self._audit(conn, incident_id, actor, "incident.closed", {"outcome": outcome})
            return dict(conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone())

    def merge_offline_batch(self, actor: str, role: str, client_batch_id: str,
                            events: list[dict[str, Any]]) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"operator", "field", "coordinator"}, "合并离线记录")
        batch_id = client_batch_id.strip()
        if not batch_id or not isinstance(events, list):
            raise DomainError("批次编号和事件列表不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM offline_batches WHERE client_batch_id=?", (batch_id,)).fetchone()
            if existing:
                return {"batch_id": batch_id, "idempotent": True, "status": existing["status"], "summary": json.loads(existing["summary"])}
            results = []
            for event in events:
                event_id = str(event.get("client_event_id", "")).strip()
                try:
                    if not event_id:
                        raise DomainError("离线事件缺少 client_event_id")
                    if event.get("type") == "clue":
                        existing_clue = conn.execute("SELECT id FROM clues WHERE client_event_id=?", (event_id,)).fetchone()
                        if existing_clue:
                            results.append({"client_event_id": event_id, "status": "merged", "record_id": existing_clue["id"], "idempotent": True})
                            continue
                        incident_id = int(event["incident_id"])
                        lat, lon = validate_position(event["latitude"], event["longitude"])
                        confidence = float(event["confidence"])
                        if not 0 <= confidence <= 1:
                            raise DomainError("置信度应在 0 到 1 之间")
                        incident = conn.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
                        if not incident:
                            raise DomainError("事件不存在", 404)
                        if incident["status"] in CLOSED_INCIDENT:
                            raise DomainError("已结束事件不能新增线索", 409)
                        area_id = event.get("area_id")
                        if area_id is not None and not conn.execute(
                            "SELECT 1 FROM search_areas WHERE id=? AND incident_id=?", (area_id, incident_id)
                        ).fetchone():
                            raise DomainError("搜索区域不属于该事件", 409)
                        distance = haversine_km(incident["latitude"], incident["longitude"], lat, lon)
                        status = "unverified" if distance <= incident["uncertainty_km"] * 3 else "invalid"
                        cur = conn.execute(
                            """INSERT INTO clues(incident_id,area_id,client_event_id,latitude,longitude,confidence,source,status,
                               distance_from_incident_km,reporter,details,recorded_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (incident_id, area_id, event_id, lat, lon, confidence,
                             str(event.get("source", "offline")).strip(), status, distance, actor,
                             str(event.get("details", "")).strip(), utcnow()),
                        )
                        self._audit(conn, incident_id, actor, "clue.recorded", {"clue_id": cur.lastrowid, "status": status, "event_id": event_id})
                        results.append({"client_event_id": event_id, "status": "merged", "record_id": cur.lastrowid})
                    elif event.get("type") == "timeline":
                        incident_id = int(event["incident_id"])
                        if not conn.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone():
                            raise DomainError("事件不存在", 404)
                        self._audit(conn, incident_id, actor, event.get("action", "offline.note"), event.get("details", {}))
                        results.append({"client_event_id": event_id, "status": "merged", "record_id": None})
                    elif event.get("type") == "sector_coverage":
                        ingested = self._apply_coverage_report(
                            conn,
                            int(event["incident_id"]),
                            event_id,
                            str(event.get("actor") or actor),
                            int(event["area_id"]),
                            int(event["sector_id"]) if event.get("sector_id") is not None else None,
                            int(event["sequence"]) if event.get("sequence") is not None else None,
                            int(event["asset_id"]),
                            event.get("latitude"),
                            event.get("longitude"),
                            str(event.get("source", "offline")),
                            batch_id,
                        )
                        entry = {"client_event_id": event_id, "record_id": ingested["coverage_id"],
                                 "sector_id": ingested.get("sector_id"), "idempotent": bool(ingested["idempotent"])}
                        if ingested["idempotent"]:
                            entry["status"] = "merged"
                        elif ingested["status"] == "pending_review":
                            entry["status"] = "pending_review"
                        else:
                            entry["status"] = "merged"
                        if ingested.get("ended_area"):
                            entry["status"] = "conflict"
                            entry["review_id"] = ingested.get("conflict_review_id")
                        if ingested.get("review_id"):
                            entry["qualification_review_id"] = ingested["review_id"]
                        results.append(entry)
                    else:
                        raise DomainError("不支持的离线事件类型")
                except (DomainError, KeyError, TypeError, ValueError) as exc:
                    results.append({"client_event_id": event_id, "status": "rejected", "error": str(exc)})
            ended_area_ids = {
                int(item["area_id"]) for item in results
                if item.get("status") == "conflict" and item.get("area_id") is not None
            }
            # Also recompute ended areas touched by accepted coverage, in case the
            # area ended while the vessel was out of communication.
            for item in results:
                if item["status"] in {"merged", "conflict"} and item.get("sector_id") is not None:
                    row = conn.execute("SELECT a.id AS area_id, a.status AS status FROM sectors s JOIN search_areas a ON a.id=s.area_id WHERE s.id=?",
                                       (item["sector_id"],)).fetchone()
                    if row and row["status"] in ENDED_AREA:
                        ended_area_ids.add(int(row["area_id"]))
            recomputed = []
            for area_id in sorted(ended_area_ids):
                stats = self._recompute_area_coverage(conn, area_id, actor)
                if stats:
                    recomputed.append(stats)
            summary = {
                "accepted": sum(1 for item in results if item["status"] == "merged"),
                "rejected": sum(1 for item in results if item["status"] == "rejected"),
                "pending_review": sum(1 for item in results if item["status"] == "pending_review"),
                "conflicts": sum(1 for item in results if item["status"] == "conflict"),
                "events": results,
                "ended_areas_recomputed": recomputed,
            }
            now = utcnow()
            conn.execute(
                "INSERT INTO offline_batches(client_batch_id,actor,status,received_at,merged_at,summary) VALUES(?,?,?,?,?,?)",
                (batch_id, actor, "merged", now, now, json_dump(summary)),
            )
            self._audit(conn, None, actor, "offline.batch_merged", {"batch_id": batch_id, **{k: summary[k] for k in ("accepted", "rejected", "pending_review", "conflicts")}})
            return {"batch_id": batch_id, "idempotent": False, "status": "merged", "summary": summary}

    def state(self, actor: str = "", role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            incidents = [dict(r) for r in conn.execute("SELECT * FROM incidents ORDER BY id DESC").fetchall()]
            areas = [dict(r) for r in conn.execute("SELECT * FROM search_areas ORDER BY priority,id").fetchall()]
            clues = [dict(r) for r in conn.execute("SELECT * FROM clues ORDER BY id DESC LIMIT 200").fetchall()]
            assets = [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY id").fetchall()]
            sectors = [dict(r) for r in conn.execute("SELECT * FROM sectors ORDER BY area_id,sequence").fetchall()]
            coverage = [dict(r) for r in conn.execute("SELECT * FROM sector_coverage ORDER BY id DESC LIMIT 500").fetchall()]
            reviews = [dict(r) for r in conn.execute("SELECT * FROM review_items WHERE status='pending' ORDER BY id DESC LIMIT 200").fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 300").fetchall()]
        return {"incidents": incidents, "assets": assets, "search_areas": areas, "clues": clues,
                "sectors": sectors, "sector_coverage": coverage, "review_items": reviews, "timeline": timeline}

    def incident_timeline(self, incident_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM timeline WHERE incident_id=? ORDER BY id", (incident_id,)).fetchall()
        return [dict(r) for r in rows]

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM incidents").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        incident = self.create_incident("coord-demo", "coordinator", "SAR-2026-001", "远星号", 31.2, 122.5, 15.0, 3, "东海搜救中心", description="演示遇险事件")
        self.add_asset("coord-demo", "coordinator", "海巡01", "vessel", ["surface", "night"], 31.0, 122.0, 22.0, 180.0, 6)
        self.add_asset("coord-demo", "coordinator", "救助直升机", "aircraft", ["air", "night"], 30.8, 122.1, 180.0, 260.0, 5)
        area = self.create_search_area("coord-demo", "coordinator", incident["id"], "AREA-A", "surface", 31.2, 122.5, 20.0, 1, "首要搜索区")
        sectors = self.split_area("coord-demo", "coordinator", area["id"], 6, area["version"])
        self.assign_area("coord-demo", "coordinator", area_id=area["id"], asset_id=self._asset_by_name("海巡01")["id"])
        self.report_sector_coverage(
            "coord-demo", "coordinator", incident["id"], "demo-cov-1",
            self._asset_by_name("海巡01")["id"], area_id=area["id"], sequence=1, source="demo",
        )
        return {"seeded": True, "incident_id": incident["id"], "sectors": len(sectors)}

    def _asset_by_name(self, name: str) -> dict[str, Any]:
        with self.connect() as conn:
            return dict(conn.execute("SELECT * FROM assets WHERE name=?", (name,)).fetchone())


class ApiHandler(BaseHTTPRequestHandler):
    service: MaritimeSARService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _actor(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "viewer")

    def _json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise DomainError("Content-Length 无效") from exc
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(data, dict):
            raise DomainError("JSON 请求体必须是对象")
        return data

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/health":
                self._send(200, {"status": "ok", "service": "maritime-sar"})
                return
            if path == "/api/state":
                self._send(200, self.service.state(*self._actor()))
                return
            if path.startswith("/api/reviews"):
                status = parse_qs(urlparse(self.path).query).get("status", ["pending"])[0]
                self._send(200, {"review_items": self.service.list_review_items(status)})
                return
            if path.startswith("/api/incidents/") and path.endswith("/timeline"):
                incident_id = int(path.split("/")[3])
                self._send(200, {"timeline": self.service.incident_timeline(incident_id)})
                return
            self._send(404, {"error": "接口不存在"})
        except (DomainError, ValueError) as exc:
            self._send(getattr(exc, "status", 400), {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            data, (actor, role) = self._json(), self._actor()
            if path == "/api/incidents":
                result = self.service.create_incident(actor, role, **data)
            elif path == "/api/assets":
                result = self.service.add_asset(actor, role, **data)
            elif path == "/api/areas":
                result = self.service.create_search_area(actor, role, **data)
            elif path == "/api/areas/split":
                result = self.service.split_area(actor, role, **data)
            elif path == "/api/areas/reassign":
                result = self.service.reassign_area(actor, role, **data)
            elif path == "/api/sectors/coverage":
                result = self.service.report_sector_coverage(actor, role, **data)
            elif path == "/api/reviews/resolve":
                result = self.service.resolve_review_item(actor, role, **data)
            elif path == "/api/assignments":
                result = self.service.assign_area(actor, role, **data)
            elif path == "/api/clues":
                result = self.service.record_clue(actor, role, **data)
            elif path == "/api/clues/verify":
                result = self.service.verify_clue(actor, role, **data)
            elif path == "/api/assets/withdraw":
                result = self.service.withdraw_asset(actor, role, **data)
            elif path == "/api/areas/complete":
                result = self.service.complete_area(actor, role, **data)
            elif path == "/api/incidents/transfer":
                result = self.service.transfer_incident(actor, role, **data)
            elif path == "/api/incidents/close":
                result = self.service.close_incident(actor, role, **data)
            elif path == "/api/offline/batch":
                result = self.service.merge_offline_batch(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: MaritimeSARService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Maritime SAR service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="海上搜救协调服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8206)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = MaritimeSARService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
