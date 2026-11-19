import pytest

from av_control.center.alerts import AlertRaised
from av_control.center.store import Store
from av_control.messages import Command, CommandResult, Telemetry


def test_alert_lifecycle():
    s = Store()
    alert_id = s.open_alert(AlertRaised("long_stop", "av01", 60, "60초째 멈춤 (A0B0)"))
    s.clear_alert("long_stop", "av01", 120)
    assert s.acknowledge(alert_id, 125)
    assert not s.acknowledge(alert_id, 126)  # 이미 확인함
    assert s.resolve(alert_id, 130)
    (row,) = s.alerts()
    assert (row.type, row.subject, row.t_raised, row.t_cleared) == ("long_stop", "av01", 60, 120)
    assert (row.state, row.t_acknowledged, row.t_resolved) == ("resolved", 125, 130)


def test_clearing_touches_only_the_open_alert_of_that_kind():
    s = Store()
    s.open_alert(AlertRaised("long_stop", "av01", 60, "첫 번째"))
    s.clear_alert("long_stop", "av01", 70)
    s.open_alert(AlertRaised("long_stop", "av01", 200, "두 번째"))
    s.open_alert(AlertRaised("hard_brake", "av01", 210, "다른 종류"))
    s.clear_alert("long_stop", "av01", 220)
    assert [(a.detail, a.t_cleared) for a in s.alerts()] == [("첫 번째", 70), ("두 번째", 220), ("다른 종류", None)]


def test_command_and_result():
    s = Store()
    s.record_command("av01", Command(cmd_id="c1", type="speed_limit", value=5.0), 10)
    assert s.commands()[0].ok is None  # 결과가 오기 전
    s.record_result("av01", CommandResult(cmd_id="c1", ok=True, t=11))
    (row,) = s.commands()
    assert (row.cmd_id, row.vehicle_id, row.type, row.value, row.ok, row.t_result) == (
        "c1", "av01", "speed_limit", 5.0, True, 11,
    )


def test_file_database_survives_reopen(tmp_path):
    path = tmp_path / "sub" / "center.db"
    s = Store(path)
    s.add_telemetry("av01", Telemetry(t=1, x=0, y=0, speed=1, accel=0, heading=0, road="A0B0", destination="B0C0"))
    s.close()
    assert Store(path).count("telemetry") == 1


def test_count_rejects_unknown_table():
    with pytest.raises(ValueError):
        Store().count("telemetry; DROP TABLE alerts")
