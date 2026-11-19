import pytest
from pydantic import ValidationError

from av_control.messages import (
    Command,
    Route,
    Status,
    Telemetry,
    command_topic,
    parse_vehicle_topic,
    route_topic,
    telemetry_topic,
)


def test_telemetry_round_trip():
    tel = Telemetry(t=12.0, x=1.5, y=-2.0, speed=11.5, accel=-0.3, heading=90.0, road="A0B0", destination="C2C3")
    assert Telemetry.from_payload(tel.to_payload()) == tel


def test_route_round_trip_and_topic():
    route = Route(t=3.0, edges=("A0B0", "B0C0"))
    assert Route.from_payload(route.to_payload()) == route
    assert parse_vehicle_topic(route_topic("av01")) == ("av01", "route")


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        Status.from_payload(b'{"state": "online", "battery": 80}')


@pytest.mark.parametrize(
    "payload",
    [
        '{"cmd_id": "c1", "type": "stop", "value": 3}',  # stop에는 값이 없어야 한다
        '{"cmd_id": "c1", "type": "reroute"}',  # reroute에는 목적지가 있어야 한다
        '{"cmd_id": "c1", "type": "speed_limit", "value": -5}',  # 음수 속도
        '{"cmd_id": "c1", "type": "speed_limit", "value": "10"}',  # 문자열 속도
        '{"cmd_id": "c1", "type": "fly"}',  # 없는 명령
    ],
)
def test_invalid_commands_are_rejected(payload):
    with pytest.raises(ValidationError):
        Command.from_payload(payload)


def test_speed_limit_accepts_integer_and_null():
    assert Command.from_payload('{"cmd_id": "c1", "type": "speed_limit", "value": 10}').value == 10.0
    assert Command.from_payload('{"cmd_id": "c2", "type": "speed_limit", "value": null}').value is None


def test_topics():
    assert telemetry_topic("av01") == "vehicles/av01/telemetry"
    assert parse_vehicle_topic(command_topic("av01")) == ("av01", "cmd")
    with pytest.raises(ValueError):
        telemetry_topic("a/b")
    with pytest.raises(ValueError):
        parse_vehicle_topic("sim/clock")
