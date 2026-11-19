"""bridge 전체: 차량 보고와 원격 명령이 MQTT로 오가는지, 그리고 av-bridge 명령."""

import subprocess
import threading
import time

import paho.mqtt.client as mqtt
import pytest

from av_control.bridge.app import Bridge
from av_control.bridge.cli import main
from av_control.bridge.simulation import Simulation
from av_control.messages import (
    CLOCK_TOPIC,
    Command,
    CommandResult,
    Route,
    Status,
    Telemetry,
    command_topic,
)


class Center:
    """테스트용 관제 센터: 차량 토픽과 시계를 모두 받아 쌓는다."""

    def __init__(self, port: int) -> None:
        self._messages: list[tuple[str, bytes]] = []
        self._lock = threading.Lock()
        subscribed = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-center")
        self.client.on_connect = lambda c, *args: c.subscribe([("vehicles/#", 1), (CLOCK_TOPIC, 0)])
        self.client.on_subscribe = lambda *args: subscribed.set()
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        assert subscribed.wait(5), "구독이 확인되지 않았다"

    def _on_message(self, client, userdata, msg) -> None:
        with self._lock:
            self._messages.append((msg.topic, msg.payload))

    def payloads(self, topic: str) -> list[bytes]:
        with self._lock:
            return [p for t, p in self._messages if t == topic]

    def last_status(self, vid: str) -> str | None:
        found = self.payloads(f"vehicles/{vid}/status")
        return Status.from_payload(found[-1]).state if found else None

    def results(self, vid: str) -> list[CommandResult]:
        return [CommandResult.from_payload(p) for p in self.payloads(f"vehicles/{vid}/cmd_result")]

    def telemetry(self, vid: str) -> list[Telemetry]:
        return [Telemetry.from_payload(p) for p in self.payloads(f"vehicles/{vid}/telemetry")]

    def send(self, vid: str, payload: bytes) -> None:
        self.client.publish(command_topic(vid), payload, qos=1).wait_for_publish(5)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("기다리던 메시지가 오지 않았다")
        time.sleep(0.02)


def step_until(bridge: Bridge, predicate, max_steps: int = 20) -> None:
    """명령이 MQTT로 도착할 틈을 주며 한 스텝씩 돌린다."""
    for _ in range(max_steps):
        bridge.step()
        time.sleep(0.05)
        if predicate():
            return
    raise AssertionError(f"{max_steps}스텝 안에 조건을 만족하지 않았다")


@pytest.fixture
def center(mqtt_port):
    c = Center(mqtt_port)
    yield c
    c.close()


@pytest.fixture
def bridge(grid_net, mqtt_port):
    b = Bridge(Simulation(grid_net, seed=2), mqtt_host="127.0.0.1", mqtt_port=mqtt_port)
    b.start(vehicles=3)
    yield b
    b.close()


def test_vehicles_report_over_mqtt(center, bridge):
    for _ in range(10):
        bridge.step()
    for vid in ("av00", "av01", "av02"):
        wait_for(lambda: center.last_status(vid) == "online")
        wait_for(lambda: len(center.telemetry(vid)) >= 5)
        wait_for(lambda: center.payloads(f"vehicles/{vid}/route"))
        route = Route.from_payload(center.payloads(f"vehicles/{vid}/route")[-1])
        assert route.edges[-1] == center.telemetry(vid)[-1].destination
    wait_for(lambda: len(center.payloads(CLOCK_TOPIC)) == 10)
    # 시계와 차량 보고는 연결이 달라 도착 순서가 보장되지 않으므로 따로 기다린다
    wait_for(lambda: center.telemetry("av00")[-1].t == 10.0)


def test_stop_command_round_trip(center, bridge):
    for _ in range(5):
        bridge.step()
    wait_for(lambda: center.last_status("av00") == "online")
    center.send("av00", Command(cmd_id="c1", type="stop").to_payload())
    step_until(bridge, lambda: center.results("av00"))
    result = center.results("av00")[0]
    assert (result.cmd_id, result.ok, result.reason) == ("c1", True, None)

    for _ in range(10):
        bridge.step()
    wait_for(lambda: center.telemetry("av00")[-1].t == bridge.sim.time)
    assert center.telemetry("av00")[-1].speed == 0


def test_malformed_command_gets_error_result(center, bridge):
    for _ in range(3):
        bridge.step()
    wait_for(lambda: center.last_status("av00") == "online")
    center.send("av00", b'{"cmd_id": "bad", "type": "fly"}')
    step_until(bridge, lambda: center.results("av00"))
    result = center.results("av00")[0]
    assert (result.cmd_id, result.ok) == ("bad", False)
    assert result.reason.startswith("명령 형식 오류")


def test_close_announces_end(center, grid_net, mqtt_port):
    b = Bridge(Simulation(grid_net, seed=3), mqtt_host="127.0.0.1", mqtt_port=mqtt_port)
    b.start(vehicles=2)
    for _ in range(5):
        b.step()
    wait_for(lambda: center.last_status("av01") == "online")
    b.close()
    wait_for(lambda: center.last_status("av00") == "ended" and center.last_status("av01") == "ended")


def test_cli_run_reports(center, grid_net, mqtt_port):
    main(["run", "--net", str(grid_net), "--vehicles", "2", "--duration", "5", "--rate", "0",
          "--mqtt-port", str(mqtt_port)])
    wait_for(lambda: len(center.payloads(CLOCK_TOPIC)) == 5)
    wait_for(lambda: center.last_status("av01") == "ended")


def test_pkill_on_uv_launched_bridge_still_ends_vehicles(center, grid_net, mqtt_port):
    """uv run으로 띄운 bridge를 pkill로 끄면 uv와 파이썬이 둘 다 신호를 받고 uv가 한 번 더 전달한다.

    정리하는 도중에 겹쳐 온 신호에 프로세스가 죽으면 차량들이 ended가 아니라 offline(Last Will)으로 끝난다.
    """
    vehicles = [f"av{i:02d}" for i in range(10)]
    command = ["av-bridge", "run", "--net", str(grid_net), "--vehicles", "10", "--rate", "5", "--mqtt-port", str(mqtt_port)]
    proc = subprocess.Popen(["uv", "run", *command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: all(center.last_status(v) == "online" for v in vehicles), timeout=20)
        subprocess.run(["pkill", "-TERM", "-f", " ".join(command)], check=True)  # uv와 파이썬 프로세스를 둘 다 맞힌다
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    wait_for(lambda: all(center.last_status(v) == "ended" for v in vehicles))


def test_cli_without_broker_exits_with_hint(grid_net, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--net", str(grid_net), "--mqtt-port", "1"])
    assert exc.value.code == 1
    assert "mosquitto" in capsys.readouterr().err
