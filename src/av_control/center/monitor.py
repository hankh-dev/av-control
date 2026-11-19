"""규칙 기반 이상 감지.

메시지를 받아 알림 사건(AlertRaised, AlertCleared)만 내고, 저장이나 통신 같은 입출력은 하지 않는다.
시각은 모두 메시지에 담긴 시뮬레이션 시각이라, 같은 메시지를 같은 순서로 넣으면 몇 배속이든 결과가 같다.
"""

from collections import Counter
from dataclasses import dataclass, field

from av_control.center.alerts import AlertCleared, AlertEvent, AlertRaised, AlertType
from av_control.messages import Clock, Command, Route, Status, Telemetry


@dataclass(frozen=True)
class Rules:
    """감지 기준. 평가 결과를 보며 계속 조정한다.

    처음 값은 정상 운행 1시간(차량 30대, 2026-11-19 측정) 기록으로 정했다. 그때 감속은 가장 세도 3.0m/s²였고,
    가장 오래 멈춘 시간은 신호 대기 46초였으며, 60초 넘게 막힌 차가 모인 도로는 없었다. 이 기준으로 오탐은 0건이었다.
    """

    hard_brake_accel: float = -4.0  # 이 값 이하로 감속하면 급정거 (평소 감속 상한은 3.0m/s²)
    stop_speed: float = 0.1  # 이보다 느리면 멈춘 것으로 본다 (m/s)
    long_stop_seconds: float = 60.0  # 이만큼 멈춰 있으면 오래 멈춤 (가장 긴 신호 대기 46초보다 길게)
    congestion_speed: float = 2.0  # 이보다 느린 차는 막힌 것으로 본다 (m/s)
    congestion_seconds: float = 60.0  # 이만큼 넘게 막힌 차가
    congestion_vehicles: int = 3  # 한 도로에 이만큼 있으면 정체
    off_route_reports: int = 3  # 계획 경로에 없는 도로 보고가 이만큼 이어지면 경로 이탈
    report_timeout: float = 5.0  # 보고가 이만큼(초) 없으면 보고 끊김


@dataclass
class _Vehicle:
    last_t: float | None = None  # 마지막 보고 시각
    road: str = ""
    stopped_since: float | None = None
    slow_since: float | None = None
    route: frozenset[str] = frozenset()
    off_route_count: int = 0
    held: bool = False  # 관제사가 정지 명령을 내린 상태
    ended: bool = False
    active: set[AlertType] = field(default_factory=set)  # 울리고 있는 알림


class Monitor:
    def __init__(self, rules: Rules | None = None) -> None:
        self.rules = rules or Rules()
        self.now = 0.0
        self._vehicles: dict[str, _Vehicle] = {}
        self._congested: set[str] = set()  # 정체 알림이 울리고 있는 도로

    # --- 메시지 입력 ---

    def on_clock(self, clock: Clock) -> list[AlertEvent]:
        self.now = max(self.now, clock.t)
        return self._check_report_timeouts() + self._check_congestion()

    def on_telemetry(self, vid: str, tel: Telemetry) -> list[AlertEvent]:
        r = self.rules
        v = self._live(vid)
        self.now = max(self.now, tel.t)
        v.last_t = tel.t
        v.road = tel.road
        events = self._clear(vid, v, "report_lost", tel.t)

        if tel.accel <= r.hard_brake_accel:
            events += self._raise(vid, v, "hard_brake", tel.t, f"{-tel.accel:.1f}m/s²로 감속 ({tel.road})")
        else:
            events += self._clear(vid, v, "hard_brake", tel.t)

        if tel.speed < r.stop_speed:
            if v.stopped_since is None:
                v.stopped_since = tel.t
            stopped_for = tel.t - v.stopped_since
            if stopped_for >= r.long_stop_seconds and not v.held:
                events += self._raise(vid, v, "long_stop", tel.t, f"{stopped_for:.0f}초째 멈춤 ({tel.road})")
        else:
            v.stopped_since = None
            events += self._clear(vid, v, "long_stop", tel.t)

        if tel.speed < r.congestion_speed:
            if v.slow_since is None:
                v.slow_since = tel.t
        else:
            v.slow_since = None

        if v.route and not tel.road.startswith(":"):  # 교차로 안은 경로 판단에서 뺀다
            if tel.road in v.route:
                v.off_route_count = 0
                events += self._clear(vid, v, "off_route", tel.t)
            else:
                v.off_route_count += 1
                if v.off_route_count >= r.off_route_reports:
                    events += self._raise(vid, v, "off_route", tel.t, f"계획 경로에 없는 도로 {tel.road}")
        return events

    def on_status(self, vid: str, status: Status) -> list[AlertEvent]:
        t = status.t if status.t is not None else self.now
        match status.state:
            case "online":
                return self._clear(vid, self._live(vid), "report_lost", t)
            case "offline":
                return self._raise(vid, self._live(vid), "report_lost", t, "연결이 끊김 (Last Will)")
            case "ended":
                v = self._vehicles.setdefault(vid, _Vehicle())
                events: list[AlertEvent] = [AlertCleared(kind, vid, t) for kind in sorted(v.active)]
                v.active.clear()
                v.ended = True
                return events
        return []

    def on_route(self, vid: str, route: Route) -> list[AlertEvent]:
        v = self._vehicles.setdefault(vid, _Vehicle())
        v.route = frozenset(route.edges)
        v.off_route_count = 0
        return []

    def on_command_sent(self, vid: str, command: Command) -> None:
        """관제 센터가 보낸 명령을 알려 준다. 관제사가 세운 차는 오래 멈춤으로 보지 않는다."""
        v = self._vehicles.get(vid)
        if v is None:
            return
        if command.type == "stop":
            v.held = True
        elif command.type == "resume":
            v.held = False
            v.stopped_since = None  # 다시 출발할 시간을 준다

    def held(self, vid: str) -> bool:
        """관제사가 정지 명령을 내려 세워 둔 차량인지."""
        v = self._vehicles.get(vid)
        return v is not None and v.held

    # --- 시계로 판단하는 규칙 ---

    def _check_report_timeouts(self) -> list[AlertEvent]:
        events: list[AlertEvent] = []
        for vid, v in self._vehicles.items():
            if v.ended:
                continue
            if v.last_t is None:  # 알고는 있지만 아직 보고가 없는 차량: 지금부터 잰다
                v.last_t = self.now
                continue
            silent_for = self.now - v.last_t
            if silent_for >= self.rules.report_timeout:
                events += self._raise(vid, v, "report_lost", self.now, f"보고가 {silent_for:.0f}초째 없음")
        return events

    def _check_congestion(self) -> list[AlertEvent]:
        r = self.rules
        stuck = Counter(
            v.road
            for v in self._vehicles.values()
            if not v.ended
            and "report_lost" not in v.active  # 소식이 끊긴 차의 마지막 위치는 믿지 않는다
            and v.slow_since is not None
            and v.road
            and not v.road.startswith(":")
            and self.now - v.slow_since >= r.congestion_seconds
        )
        congested = {road for road, n in stuck.items() if n >= r.congestion_vehicles}
        events: list[AlertEvent] = [
            AlertRaised("congestion", road, self.now, f"{stuck[road]}대가 {r.congestion_seconds:.0f}초 넘게 막힘")
            for road in sorted(congested - self._congested)
        ]
        events += [AlertCleared("congestion", road, self.now) for road in sorted(self._congested - congested)]
        self._congested = congested
        return events

    # --- 도우미 ---

    def _live(self, vid: str) -> _Vehicle:
        """운행 중인 차량의 상태. 처음 보거나 운행을 마쳤던 차량이면 새로 시작한다 (계획 경로는 유지)."""
        v = self._vehicles.get(vid)
        if v is None or v.ended:
            v = self._vehicles[vid] = _Vehicle(route=v.route if v else frozenset())
        return v

    def _raise(self, vid: str, v: _Vehicle, kind: AlertType, t: float, detail: str) -> list[AlertEvent]:
        if kind in v.active:
            return []
        v.active.add(kind)
        return [AlertRaised(kind, vid, t, detail)]

    def _clear(self, vid: str, v: _Vehicle, kind: AlertType, t: float) -> list[AlertEvent]:
        if kind not in v.active:
            return []
        v.active.discard(kind)
        return [AlertCleared(kind, vid, t)]
