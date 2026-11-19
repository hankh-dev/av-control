"""감지 규칙. 입출력 없이 메시지만 넣어 본다."""

from av_control.center.alerts import AlertCleared, AlertRaised
from av_control.center.monitor import Monitor, Rules
from av_control.messages import Clock, Command, Route, Status, Telemetry


def tel(t: float, *, speed: float = 10.0, accel: float = 0.0, road: str = "A0B0") -> Telemetry:
    return Telemetry(t=t, x=0.0, y=0.0, speed=speed, accel=accel, heading=0.0, road=road, destination="C0C1")


def test_hard_brake_raises_once_and_clears():
    m = Monitor()
    assert m.on_telemetry("av01", tel(1, accel=-3.0)) == []  # 평소 감속
    assert m.on_telemetry("av01", tel(2, accel=-6.0)) == [AlertRaised("hard_brake", "av01", 2, "6.0m/s²로 감속 (A0B0)")]
    assert m.on_telemetry("av01", tel(3, accel=-7.0)) == []  # 이어지는 동안 다시 울리지 않는다
    assert m.on_telemetry("av01", tel(4, accel=0.0)) == [AlertCleared("hard_brake", "av01", 4)]


def test_long_stop_after_threshold_and_clears_on_move():
    m = Monitor(Rules(long_stop_seconds=60))
    events = []
    for t in range(0, 70):
        events += m.on_telemetry("av01", tel(t, speed=0.0))
    assert events == [AlertRaised("long_stop", "av01", 60, "60초째 멈춤 (A0B0)")]
    assert m.on_telemetry("av01", tel(70, speed=3.0)) == [AlertCleared("long_stop", "av01", 70)]


def test_vehicle_stopped_by_operator_is_not_long_stop():
    m = Monitor(Rules(long_stop_seconds=60))
    m.on_telemetry("av01", tel(0))
    m.on_command_sent("av01", Command(cmd_id="c1", type="stop"))
    assert all(m.on_telemetry("av01", tel(t, speed=0.0)) == [] for t in range(1, 200))
    m.on_command_sent("av01", Command(cmd_id="c2", type="resume"))
    # 재출발 명령 뒤에도 계속 서 있으면 그때부터 60초를 잰다
    assert all(m.on_telemetry("av01", tel(t, speed=0.0)) == [] for t in range(200, 260))
    assert m.on_telemetry("av01", tel(260, speed=0.0)) == [AlertRaised("long_stop", "av01", 260, "60초째 멈춤 (A0B0)")]


def test_off_route_needs_consecutive_reports_and_ignores_junctions():
    m = Monitor(Rules(off_route_reports=3))
    m.on_route("av01", Route(t=0, edges=("A0B0", "B0C0")))
    assert m.on_telemetry("av01", tel(1, road="A0B0")) == []
    assert m.on_telemetry("av01", tel(2, road=":B0_1")) == []  # 교차로 안
    assert m.on_telemetry("av01", tel(3, road="B0B1")) == []
    assert m.on_telemetry("av01", tel(4, road="B0B1")) == []
    assert m.on_telemetry("av01", tel(5, road="B0B1")) == [
        AlertRaised("off_route", "av01", 5, "계획 경로에 없는 도로 B0B1")
    ]
    m.on_route("av01", Route(t=6, edges=("B0B1", "B1C1")))  # 새 계획 경로
    assert m.on_telemetry("av01", tel(6, road="B0B1")) == [AlertCleared("off_route", "av01", 6)]


def test_report_lost_when_reports_stop():
    m = Monitor(Rules(report_timeout=5))
    m.on_telemetry("av01", tel(1))
    for t in range(2, 6):
        assert m.on_clock(Clock(t=t)) == []
    assert m.on_clock(Clock(t=6)) == [AlertRaised("report_lost", "av01", 6, "보고가 5초째 없음")]
    assert m.on_clock(Clock(t=7)) == []
    assert m.on_telemetry("av01", tel(8)) == [AlertCleared("report_lost", "av01", 8)]


def test_vehicle_online_without_reports_is_timed_from_first_clock():
    m = Monitor(Rules(report_timeout=5))
    m.on_status("av01", Status(state="online"))
    assert m.on_clock(Clock(t=100)) == []  # 여기서부터 잰다
    assert m.on_clock(Clock(t=104)) == []
    assert m.on_clock(Clock(t=105)) == [AlertRaised("report_lost", "av01", 105, "보고가 5초째 없음")]


def test_last_will_raises_report_lost_and_online_clears():
    m = Monitor()
    m.on_clock(Clock(t=10))
    m.on_telemetry("av01", tel(10))
    assert m.on_status("av01", Status(state="offline")) == [
        AlertRaised("report_lost", "av01", 10, "연결이 끊김 (Last Will)")
    ]
    assert m.on_status("av01", Status(state="online")) == [AlertCleared("report_lost", "av01", 10)]


def test_ended_vehicle_clears_alerts_and_is_no_longer_watched():
    m = Monitor(Rules(report_timeout=5))
    m.on_telemetry("av01", tel(0, accel=-6.0))
    assert m.on_status("av01", Status(state="ended", t=1)) == [AlertCleared("hard_brake", "av01", 1)]
    assert m.on_clock(Clock(t=100)) == []


def test_old_route_does_not_revive_ended_vehicle():
    """관제 센터가 늦게 켜지면 끝난 차량의 retain 메시지(상태, 경로)를 순서 없이 받는다."""
    for messages in (("ended", "route"), ("route", "ended")):
        m = Monitor(Rules(report_timeout=5))
        for kind in messages:
            if kind == "ended":
                m.on_status("av01", Status(state="ended", t=50))
            else:
                m.on_route("av01", Route(t=10, edges=("A0B0",)))
        assert m.on_clock(Clock(t=100)) == [] and m.on_clock(Clock(t=200)) == []


def test_congestion_when_enough_vehicles_are_stuck_on_one_road():
    m = Monitor(Rules(congestion_vehicles=3, congestion_seconds=60, long_stop_seconds=1000))
    events = []
    for t in range(0, 61):
        for vid in ("av01", "av02", "av03"):
            events += m.on_telemetry(vid, tel(t, speed=0.5, road="B1C1"))
        events += m.on_clock(Clock(t=t))
    assert events == [AlertRaised("congestion", "B1C1", 60, "3대가 60초 넘게 막힘")]
    m.on_telemetry("av03", tel(61, speed=8.0, road="B1C1"))
    assert m.on_clock(Clock(t=61)) == [AlertCleared("congestion", "B1C1", 61)]


def test_two_stuck_vehicles_are_not_congestion():
    m = Monitor(Rules(congestion_vehicles=3, congestion_seconds=60, long_stop_seconds=1000))
    events = []
    for t in range(0, 120):
        for vid in ("av01", "av02"):
            events += m.on_telemetry(vid, tel(t, speed=0.5, road="B1C1"))
        events += m.on_clock(Clock(t=t))
    assert events == []
