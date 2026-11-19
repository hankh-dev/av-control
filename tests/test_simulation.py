"""SUMO 시뮬레이션 래퍼: 차량 운행과 원격 명령 적용 (MQTT 없이)."""

import pytest

from av_control.bridge.simulation import Simulation
from av_control.messages import Command, Telemetry


@pytest.fixture
def sim(grid_net):
    s = Simulation(grid_net, seed=1)
    s.start()
    yield s
    s.close()


def _drive_until(sim: Simulation, vid: str, predicate, max_steps: int) -> Telemetry:
    for _ in range(max_steps):
        tel = sim.step().telemetry[vid]
        if predicate(tel):
            return tel
    raise AssertionError(f"{max_steps}스텝 안에 조건을 만족하지 않았다")


def test_fleet_departs_and_reports(sim):
    ids = sim.add_fleet(5)
    results = [sim.step() for _ in range(15)]
    assert results[0].departed == ["av00"]
    assert sorted(v for r in results for v in r.departed) == ids
    last = results[-1]
    assert set(last.telemetry) == set(ids)
    assert all(tel.t == last.t == 15.0 for tel in last.telemetry.values())


def test_fleet_keeps_driving_to_new_destinations(sim):
    sim.add_fleet(5)
    results = [sim.step() for _ in range(900)]  # 15분
    assert not any(r.arrived for r in results), "목적지에 도착해 사라진 차량이 있다"
    assert len(results[-1].telemetry) == 5
    destinations = {tel.destination for r in results for tel in r.telemetry.values() if tel is not None}
    assert len(destinations) > 5, "새 목적지를 받지 못했다"


def test_routes_are_reported_on_departure_and_change(sim):
    sim.add_fleet(1)
    first = sim.step()
    assert list(first.routes) == ["av00"]
    assert first.routes["av00"][-1] == first.telemetry["av00"].destination
    assert sim.step().routes == {}  # 바뀌지 않으면 다시 알리지 않는다
    target = next(e for e in sim.edges if e not in first.routes["av00"])
    assert sim.apply("av00", Command(cmd_id="c1", type="reroute", value=target)) == (True, None)
    assert sim.step().routes["av00"][-1] == target


def test_normal_braking_stays_under_the_comfort_limit(sim):
    sim.add_fleet(10)
    accels = [tel.accel for _ in range(600) for tel in sim.step().telemetry.values()]
    assert min(accels) >= -3.0 - 1e-6


def test_stop_holds_until_resume(sim):
    sim.add_fleet(1)
    _drive_until(sim, "av00", lambda t: t.speed > 5, 60)
    assert sim.apply("av00", Command(cmd_id="c1", type="stop")) == (True, None)
    _drive_until(sim, "av00", lambda t: t.speed == 0, 10)
    for _ in range(30):
        assert sim.step().telemetry["av00"].speed == 0, "정지 명령이 풀렸다"
    assert sim.apply("av00", Command(cmd_id="c2", type="resume")) == (True, None)
    _drive_until(sim, "av00", lambda t: t.speed > 1, 90)


def test_reroute_changes_destination(sim):
    sim.add_fleet(1)
    tel = _drive_until(sim, "av00", lambda t: not t.road.startswith(":"), 30)
    target = next(e for e in sim.edges if e not in (tel.road, tel.destination))
    assert sim.apply("av00", Command(cmd_id="c1", type="reroute", value=target)) == (True, None)
    assert sim.step().telemetry["av00"].destination == target


def test_reroute_to_unknown_road_fails(sim):
    sim.add_fleet(1)
    sim.step()
    ok, reason = sim.apply("av00", Command(cmd_id="c1", type="reroute", value="no-such-road"))
    assert not ok and reason


def test_speed_limit_and_release(sim):
    sim.add_fleet(1)
    _drive_until(sim, "av00", lambda t: t.speed > 10, 60)
    assert sim.apply("av00", Command(cmd_id="c1", type="speed_limit", value=3.0)) == (True, None)
    for _ in range(10):
        sim.step()
    assert max(sim.step().telemetry["av00"].speed for _ in range(30)) <= 3.0 + 1e-6
    assert sim.apply("av00", Command(cmd_id="c2", type="speed_limit", value=None)) == (True, None)
    _drive_until(sim, "av00", lambda t: t.speed > 5, 90)


def test_command_to_vehicle_not_on_road_fails(sim):
    assert sim.apply("ghost", Command(cmd_id="c1", type="stop")) == (False, "도로 위에 없는 차량")
