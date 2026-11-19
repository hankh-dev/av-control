"""관제 센터 전체: 실제 bridge가 보낸 보고를 MQTT로 받아 감지하고 기록하는지."""

import time

import paho.mqtt.client as mqtt
import pytest

from av_control.bridge.app import Bridge
from av_control.bridge.simulation import Simulation
from av_control.center.cli import main
from av_control.center.service import CenterService
from av_control.center.store import Store
from av_control.messages import status_topic


def advance(bridge: Bridge, center: CenterService, steps: int) -> None:
    """bridge를 한 스텝씩 돌리며 관제 센터가 받은 메시지를 처리한다."""
    for _ in range(steps):
        bridge.step()
        time.sleep(0.005)  # 연결이 다른 메시지들의 도착 순서가 흔들리지 않을 만큼만 쉰다
        center.process_pending()
    time.sleep(0.2)
    center.process_pending()


@pytest.fixture
def world(grid_net, mqtt_port):
    """차량 4대를 돌리는 bridge와 관제 센터. 차량 ID 앞글자는 테스트마다 달리해 retain 메시지가 섞이지 않게 한다."""
    created = []

    def make(prefix: str) -> tuple[Bridge, CenterService, Store]:
        store = Store()
        center = CenterService(store, mqtt_port=mqtt_port)
        center.start()
        bridge = Bridge(Simulation(grid_net, seed=4), mqtt_port=mqtt_port)
        bridge.start(vehicles=4, prefix=prefix)
        created.append((bridge, center, store))
        return bridge, center, store

    yield make
    for bridge, center, store in created:
        bridge.close()
        center.close()
        store.close()


def test_normal_driving_is_recorded_without_alerts(world):
    bridge, center, store = world("n")
    advance(bridge, center, 120)
    assert center.monitor.now == 120
    assert store.count("telemetry") >= 4 * 100
    assert store.count("routes") >= 4
    assert store.alerts() == []


def test_operator_stop_is_recorded_and_not_reported_as_long_stop(world):
    bridge, center, store = world("c")
    advance(bridge, center, 10)
    cmd_id = center.send_command("c00", "stop")
    for _ in range(20):
        advance(bridge, center, 1)
        if store.commands()[0].ok is not None:
            break
    (command,) = store.commands()
    assert (command.cmd_id, command.vehicle_id, command.type, command.ok) == (cmd_id, "c00", "stop", True)

    advance(bridge, center, 100)  # 오래 멈춤 기준(60초)보다 오래 세워 둔다
    assert not [a for a in store.alerts() if a.type == "long_stop" and a.subject == "c00"]


def test_lost_connection_raises_report_lost(world, mqtt_port):
    bridge, center, store = world("w")
    advance(bridge, center, 10)
    bridge.cut_connection("w01")
    advance(bridge, center, 3)
    lost = [a for a in store.alerts() if a.type == "report_lost" and a.subject == "w01"]
    assert lost and lost[0].detail == "연결이 끊김 (Last Will)"

    # 다른 테스트가 이 차량의 offline retain 메시지를 받지 않게 지운다
    cleaner = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="cleaner")
    cleaner.connect("127.0.0.1", mqtt_port)
    cleaner.loop_start()
    cleaner.publish(status_topic("w01"), b"", qos=1, retain=True).wait_for_publish(5)
    cleaner.loop_stop()
    cleaner.disconnect()


def test_cli_without_broker_exits_without_creating_db(tmp_path, capsys):
    db = tmp_path / "center.db"
    with pytest.raises(SystemExit) as exc:
        main(["run", "--db", str(db), "--no-web", "--mqtt-port", "1"])
    assert exc.value.code == 1
    assert "mosquitto" in capsys.readouterr().err
    assert not db.exists()
