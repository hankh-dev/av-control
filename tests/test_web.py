"""관제 화면 서버: 지도, 현재 상태, 원격 명령, 알림 처리, 실시간 연결.

실제 bridge와 브로커를 쓰고, 화면 서버와는 HTTP와 WebSocket으로만 주고받는다.
(서버 안의 감지기와 상태는 서버의 이벤트 루프 스레드에서만 다뤄야 하기 때문이다.)
"""

import time

import pytest
from fastapi.testclient import TestClient

from av_control.bridge.app import Bridge
from av_control.bridge.simulation import Simulation
from av_control.center.roadmap import load_map
from av_control.center.service import CenterService
from av_control.center.store import Store
from av_control.center.web import create_app


@pytest.fixture
def setup(grid_net, mqtt_port):
    store = Store()
    service = CenterService(store, mqtt_port=mqtt_port)
    service.start()
    bridge = Bridge(Simulation(grid_net, seed=5), mqtt_port=mqtt_port)
    bridge.start(vehicles=3, prefix="u")
    app = create_app(service, load_map(grid_net), push_interval=0.05)
    with TestClient(app) as client:
        yield bridge, client
    bridge.close()
    service.close()
    store.close()


def drive(bridge: Bridge, steps: int) -> None:
    for _ in range(steps):
        bridge.step()
        time.sleep(0.01)
    time.sleep(0.3)  # 서버가 0.05초마다 메시지를 처리하므로 따라잡을 시간을 준다


def wait_until(client: TestClient, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = client.get("/api/snapshot").json()
        if predicate(snapshot):
            return snapshot
        if time.monotonic() > deadline:
            raise AssertionError(f"조건을 만족하지 않았다: {snapshot}")
        time.sleep(0.05)


def test_page_and_map_are_served(setup):
    _, client = setup
    page = client.get("/")
    assert page.status_code == 200 and "자율주행 관제" in page.text
    for path in ("/static/app.js", "/static/style.css", "/static/vendor/leaflet.js"):
        assert client.get(path).status_code == 200, path
    road_map = client.get("/api/map").json()
    assert len(road_map["roads"]) == 80 and road_map["bounds"] == [0, 0, 800, 800]


def test_snapshot_shows_running_vehicles(setup):
    bridge, client = setup
    drive(bridge, 10)
    snapshot = wait_until(client, lambda s: len(s["vehicles"]) == 3 and s["t"] == 10)
    first = snapshot["vehicles"][0]
    assert (first["id"], first["state"], first["held"]) == ("u00", "online", False)
    assert first["route"] and first["route"][-1] == first["destination"]


def test_command_from_screen_reaches_vehicle(setup):
    bridge, client = setup
    drive(bridge, 5)
    wait_until(client, lambda s: any(v["id"] == "u01" for v in s["vehicles"]))
    res = client.post("/api/vehicles/u01/commands", json={"type": "stop"})
    assert res.status_code == 200
    cmd_id = res.json()["cmd_id"]
    for _ in range(20):
        drive(bridge, 1)
        snapshot = client.get("/api/snapshot").json()
        if snapshot["commands"][0]["ok"] is not None:
            break
    command = snapshot["commands"][0]
    assert (command["cmd_id"], command["vehicle_id"], command["type"], command["ok"]) == (cmd_id, "u01", "stop", True)
    assert next(v for v in snapshot["vehicles"] if v["id"] == "u01")["held"] is True


def test_bad_requests_are_rejected_with_reason(setup):
    bridge, client = setup
    drive(bridge, 3)
    wait_until(client, lambda s: s["vehicles"])
    assert client.post("/api/vehicles/ghost/commands", json={"type": "stop"}).status_code == 404
    bad = client.post("/api/vehicles/u00/commands", json={"type": "speed_limit", "value": -3})
    assert bad.status_code == 400 and "양수" in bad.json()["detail"]
    assert client.post("/api/vehicles/u00/commands", json={"type": "fly"}).status_code == 422
    assert client.post("/api/alerts/999/acknowledge").status_code == 409


def test_alert_can_be_acknowledged_and_resolved(setup):
    bridge, client = setup
    drive(bridge, 5)
    bridge.cut_connection("u02")  # 보고 끊김 알림을 만든다
    drive(bridge, 2)
    snapshot = wait_until(client, lambda s: any(a["subject"] == "u02" for a in s["alerts"]))
    alert = next(a for a in snapshot["alerts"] if a["subject"] == "u02")
    assert (alert["type"], alert["label"], alert["state"]) == ("report_lost", "보고 끊김", "open")

    assert client.post(f"/api/alerts/{alert['id']}/acknowledge").status_code == 200
    assert client.post(f"/api/alerts/{alert['id']}/acknowledge").status_code == 409  # 이미 확인함
    assert next(a for a in client.get("/api/snapshot").json()["alerts"] if a["id"] == alert["id"])["state"] == "acknowledged"
    assert client.post(f"/api/alerts/{alert['id']}/resolve").status_code == 200
    assert all(a["id"] != alert["id"] for a in client.get("/api/snapshot").json()["alerts"])  # 처리하면 목록에서 빠진다


def test_websocket_pushes_snapshots(setup):
    bridge, client = setup
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()  # 연결하자마자 한 번
        assert set(first) == {"t", "vehicles", "alerts", "commands"}
        drive(bridge, 5)
        latest = first
        for _ in range(50):  # 0.05초마다 오므로 금방 따라잡는다
            latest = ws.receive_json()
            if latest["t"] == 5:
                break
        assert latest["t"] == 5 and latest["vehicles"]
