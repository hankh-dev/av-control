"""개발 환경 점검: SUMO가 돌고, MQTT로 보고와 끊김 알림이 오가는지 확인한다."""

import queue
import subprocess
import threading

import paho.mqtt.client as mqtt
import traci

from av_control.bridge.sumo_tools import sumo_binary


def _some_route(min_edges: int = 3) -> tuple[str, ...]:
    edges = [e for e in traci.edge.getIDList() if not e.startswith(":")]
    for a in edges:
        for b in edges:
            if a != b:
                route = traci.simulation.findRoute(a, b).edges
                if len(route) >= min_edges:
                    return route
    raise AssertionError("경로를 찾지 못했다")


def test_sumo_vehicle_moves_and_obeys_remote_stop(tmp_path):
    net = tmp_path / "grid.net.xml"
    subprocess.run(
        [sumo_binary("netgenerate"), "--grid", "--grid.number=3", "--grid.length=200", "-o", net],
        check=True,
        capture_output=True,
    )
    traci.start([str(sumo_binary("sumo")), "-n", str(net), "--no-step-log", "true"])
    try:
        traci.route.add("r0", _some_route())
        traci.vehicle.add("v0", "r0")
        for _ in range(15):
            traci.simulationStep()
        assert traci.vehicle.getSpeed("v0") > 0, "차량이 출발하지 않았다"

        traci.vehicle.setSpeed("v0", 0.0)  # 원격 정지 명령
        for _ in range(10):
            traci.simulationStep()
        assert traci.vehicle.getSpeed("v0") == 0.0, "정지 명령 후에도 움직인다"
    finally:
        traci.close()


def _subscriber(client_id: str, port: int, topic: str) -> tuple[mqtt.Client, queue.Queue]:
    """topic을 구독한 관제 센터 쪽 클라이언트를 만들고, 받은 메시지가 쌓이는 큐를 돌려준다."""
    received: queue.Queue = queue.Queue()
    subscribed = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.on_connect = lambda c, *args: c.subscribe(topic, qos=1)
    client.on_subscribe = lambda *args: subscribed.set()
    client.on_message = lambda c, userdata, msg: received.put(msg)
    client.connect("127.0.0.1", port)
    client.loop_start()
    assert subscribed.wait(5), "구독이 확인되지 않았다"
    return client, received


def test_vehicle_report_reaches_center(mqtt_port):
    center, received = _subscriber("center-report", mqtt_port, "vehicles/+/telemetry")
    try:
        vehicle = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="v0")
        vehicle.connect("127.0.0.1", mqtt_port)
        vehicle.loop_start()
        try:
            vehicle.publish("vehicles/v0/telemetry", '{"speed": 11.5}', qos=1).wait_for_publish(5)
        finally:
            vehicle.loop_stop()
            vehicle.disconnect()

        msg = received.get(timeout=5)
        assert (msg.topic, msg.payload) == ("vehicles/v0/telemetry", b'{"speed": 11.5}')
    finally:
        center.loop_stop()
        center.disconnect()


def test_abrupt_disconnect_sends_last_will(mqtt_port):
    # 브로커를 테스트 전체가 같이 쓰므로, 다른 테스트가 남긴 retain 상태를 받지 않게 이 차량만 구독한다
    center, received = _subscriber("center-will", mqtt_port, "vehicles/v1/status")
    try:
        vehicle = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="v1")
        vehicle.will_set("vehicles/v1/status", "offline", qos=1)
        vehicle.connect("127.0.0.1", mqtt_port)
        vehicle.loop(timeout=1)  # 브로커의 접속 승인 처리
        vehicle.socket().close()  # 정상 종료 없이 끊김: 차량 프로그램이 죽은 상황

        msg = received.get(timeout=5)
        assert (msg.topic, msg.payload) == ("vehicles/v1/status", b"offline")
    finally:
        center.loop_stop()
        center.disconnect()
