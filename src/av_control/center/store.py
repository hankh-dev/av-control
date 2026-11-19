"""관제 기록 저장 (SQLite).

여러 스레드에서 불러도 되도록 잠금으로 한 번에 하나씩 처리한다.
add_*와 open/clear_alert는 commit하지 않는다. 메시지를 묶어서 처리한 뒤 commit()을 부른다.
"""

import functools
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from av_control.center.alerts import AlertRaised
from av_control.messages import Command, CommandResult, Route, Status, Telemetry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    vehicle_id TEXT NOT NULL, t REAL NOT NULL, x REAL, y REAL, speed REAL, accel REAL,
    heading REAL, road TEXT, destination TEXT
);
CREATE INDEX IF NOT EXISTS telemetry_by_vehicle ON telemetry (vehicle_id, t);
CREATE TABLE IF NOT EXISTS statuses (vehicle_id TEXT NOT NULL, t REAL NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes (vehicle_id TEXT NOT NULL, t REAL NOT NULL, edges TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    subject TEXT NOT NULL,           -- 차량 ID. 정체면 도로 ID
    detail TEXT NOT NULL,
    t_raised REAL NOT NULL,
    t_cleared REAL,                  -- 조건이 풀린 시각 (자동)
    state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'acknowledged', 'resolved')),
    t_acknowledged REAL,
    t_resolved REAL
);
CREATE TABLE IF NOT EXISTS commands (
    cmd_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    type TEXT NOT NULL,
    value TEXT,                      -- JSON
    t_sent REAL NOT NULL,
    ok INTEGER,                      -- 결과가 오기 전에는 비어 있다
    reason TEXT,
    t_result REAL
);
"""

_TABLES = ("telemetry", "statuses", "routes", "alerts", "commands")
_ALERT_COLUMNS = "id, type, subject, detail, t_raised, t_cleared, state, t_acknowledged, t_resolved"
_COMMAND_COLUMNS = "cmd_id, vehicle_id, type, value, t_sent, ok, reason, t_result"


@dataclass(frozen=True)
class AlertRow:
    id: int
    type: str
    subject: str
    detail: str
    t_raised: float
    t_cleared: float | None
    state: str  # open, acknowledged, resolved
    t_acknowledged: float | None
    t_resolved: float | None


@dataclass(frozen=True)
class CommandRow:
    cmd_id: str
    vehicle_id: str
    type: str
    value: str | float | None
    t_sent: float
    ok: bool | None
    reason: str | None
    t_result: float | None


def _command_row(row: tuple) -> CommandRow:
    cmd_id, vid, kind, value, t_sent, ok, reason, t_result = row
    return CommandRow(cmd_id, vid, kind, json.loads(value), t_sent, None if ok is None else bool(ok), reason, t_result)


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)

    @_locked
    def commit(self) -> None:
        self._db.commit()

    @_locked
    def close(self) -> None:
        self._db.commit()
        self._db.close()

    @_locked
    def count(self, table: str) -> int:
        if table not in _TABLES:
            raise ValueError(f"없는 테이블: {table}")
        return self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # --- 차량 보고 ---

    @_locked
    def add_telemetry(self, vid: str, tel: Telemetry) -> None:
        self._db.execute(
            "INSERT INTO telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, tel.t, tel.x, tel.y, tel.speed, tel.accel, tel.heading, tel.road, tel.destination),
        )

    @_locked
    def add_status(self, vid: str, status: Status, t: float) -> None:
        self._db.execute("INSERT INTO statuses VALUES (?, ?, ?)", (vid, t, status.state))

    @_locked
    def add_route(self, vid: str, route: Route) -> None:
        self._db.execute("INSERT INTO routes VALUES (?, ?, ?)", (vid, route.t, json.dumps(route.edges)))

    # --- 알림 ---

    @_locked
    def open_alert(self, event: AlertRaised) -> int:
        cur = self._db.execute(
            "INSERT INTO alerts (type, subject, detail, t_raised) VALUES (?, ?, ?, ?)",
            (event.type, event.subject, event.detail, event.t),
        )
        return cur.lastrowid

    @_locked
    def clear_alert(self, kind: str, subject: str, t: float) -> None:
        self._db.execute(
            "UPDATE alerts SET t_cleared = ? WHERE type = ? AND subject = ? AND t_cleared IS NULL", (t, kind, subject)
        )

    @_locked
    def acknowledge(self, alert_id: int, t: float) -> bool:
        """관제사가 알림을 확인함. 새 알림일 때만 바뀐다."""
        cur = self._db.execute(
            "UPDATE alerts SET state = 'acknowledged', t_acknowledged = ? WHERE id = ? AND state = 'open'",
            (t, alert_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    @_locked
    def resolve(self, alert_id: int, t: float) -> bool:
        """관제사가 알림을 처리 완료함."""
        cur = self._db.execute(
            "UPDATE alerts SET state = 'resolved', t_resolved = ? WHERE id = ? AND state != 'resolved'",
            (t, alert_id),
        )
        self._db.commit()
        return cur.rowcount == 1

    @_locked
    def alerts(self) -> list[AlertRow]:
        rows = self._db.execute(f"SELECT {_ALERT_COLUMNS} FROM alerts ORDER BY id")
        return [AlertRow(*row) for row in rows]

    @_locked
    def recent_alerts(self, limit: int = 200) -> list[AlertRow]:
        """처리 완료되지 않은 알림, 최근 것부터."""
        rows = self._db.execute(
            f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE state != 'resolved' ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [AlertRow(*row) for row in rows]

    # --- 원격 명령 ---

    @_locked
    def record_command(self, vid: str, command: Command, t: float) -> None:
        self._db.execute(
            "INSERT INTO commands (cmd_id, vehicle_id, type, value, t_sent) VALUES (?, ?, ?, ?, ?)",
            (command.cmd_id, vid, command.type, json.dumps(command.value), t),
        )

    @_locked
    def record_result(self, vid: str, result: CommandResult) -> None:
        self._db.execute(
            "UPDATE commands SET ok = ?, reason = ?, t_result = ? WHERE cmd_id = ? AND vehicle_id = ?",
            (result.ok, result.reason, result.t, result.cmd_id, vid),
        )

    @_locked
    def commands(self) -> list[CommandRow]:
        rows = self._db.execute(f"SELECT {_COMMAND_COLUMNS} FROM commands ORDER BY t_sent, rowid")
        return [_command_row(row) for row in rows]

    @_locked
    def recent_commands(self, limit: int = 30) -> list[CommandRow]:
        """최근 명령부터."""
        rows = self._db.execute(
            f"SELECT {_COMMAND_COLUMNS} FROM commands ORDER BY t_sent DESC, rowid DESC LIMIT ?", (limit,)
        )
        return [_command_row(row) for row in rows]
