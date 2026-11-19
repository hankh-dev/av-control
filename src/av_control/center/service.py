"""관제 센터 본체: MQTT로 받은 보고를 감지기에 넣고, 기록과 알림을 저장한다. 원격 명령도 여기서 보낸다."""

import logging
import queue
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict

import paho.mqtt.client as mqtt

from av_control.center.alerts import LABELS, AlertEvent, AlertRaised
from av_control.center.monitor import Monitor
from av_control.center.store import Store
from av_control.messages import (
    ALL_RESULTS,
    ALL_ROUTES,
    ALL_STATUS,
    ALL_TELEMETRY,
    CLOCK_QOS,
    CLOCK_TOPIC,
    COMMAND_QOS,
    RESULT_QOS,
    ROUTE_QOS,
    STATUS_QOS,
    TELEMETRY_QOS,
    Clock,
    Command,
    CommandResult,
    Route,
    Status,
    Telemetry,
    command_topic,
    parse_vehicle_topic,
)

log = logging.getLogger(__name__)

_SUBSCRIPTIONS = [
    (ALL_TELEMETRY, TELEMETRY_QOS),
    (ALL_STATUS, STATUS_QOS),
    (ALL_ROUTES, ROUTE_QOS),
    (ALL_RESULTS, RESULT_QOS),
    (CLOCK_TOPIC, CLOCK_QOS),
]


class CenterService:
    """받은 메시지는 MQTT 스레드에서 큐에 넣기만 하고, process_pending()을 부르는 스레드가 처리한다.

    감지기와 차량별 최신 상태는 그 한 스레드에서만 다룬다. send_command, acknowledge, resolve, snapshot도
    같은 스레드에서 부른다. 관제 화면에서는 이벤트 루프 스레드가 그 역할을 한다.
    """

    def __init__(
        self,
        store: Store,
        *,
        monitor: Monitor | None = None,
        mqtt_host: str = "127.0.0.1",
        mqtt_port: int = 1883,
        on_event: Callable[[AlertEvent], None] | None = None,
    ) -> None:
        self.store = store
        self.monitor = monitor or Monitor()
        # 화면에 보여 줄 차량별 최신 상태. 운행을 마친(ended) 차량은 latest에서 뺀다.
        self.latest: dict[str, Telemetry] = {}
        self.states: dict[str, str] = {}
        self.routes: dict[str, tuple[str, ...]] = {}
        self._host = mqtt_host
        self._port = mqtt_port
        self._on_event = on_event  # 알림이 울리거나 풀릴 때 부른다 (로그 등)
        self._inbox: queue.Queue[tuple[str, bytes]] = queue.Queue()
        self._subscribed = threading.Event()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"center-{uuid.uuid4().hex[:8]}")
        self._client.on_connect = self._handle_connect
        self._client.on_subscribe = lambda *args: self._subscribed.set()
        self._client.on_message = lambda client, userdata, msg: self._inbox.put((msg.topic, msg.payload))

    def start(self, timeout: float = 5.0) -> None:
        """브로커에 연결해 구독한다. 브로커가 없으면 ConnectionRefusedError."""
        self._client.connect(self._host, self._port)
        self._client.loop_start()
        if not self._subscribed.wait(timeout):
            raise TimeoutError("브로커가 구독을 확인해 주지 않았습니다")

    def close(self) -> None:
        self._client.disconnect()
        self._client.loop_stop()

    def process_pending(self, timeout: float = 0.0, limit: int | None = None) -> int:
        """받아 둔 메시지를 처리하고 처리한 개수를 돌려준다.

        timeout이 있으면 첫 메시지를 그만큼 기다린다. limit이 있으면 그만큼만 처리하고 나머지는 다음에 한다.
        """
        try:
            item = self._inbox.get(timeout=timeout) if timeout else self._inbox.get_nowait()
        except queue.Empty:
            return 0
        count = 0
        while True:
            self._dispatch(*item)
            count += 1
            if limit is not None and count >= limit:
                break
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                break
        self.store.commit()
        return count

    def send_command(self, vehicle_id: str, command_type: str, value: str | float | None = None) -> str:
        """원격 명령을 보내고 기록한 뒤 명령 번호를 돌려준다. 형식이 틀리면 ValueError."""
        command = Command(cmd_id=uuid.uuid4().hex[:8], type=command_type, value=value)
        self._client.publish(command_topic(vehicle_id), command.to_payload(), qos=COMMAND_QOS)
        self.store.record_command(vehicle_id, command, self.monitor.now)
        self.store.commit()
        self.monitor.on_command_sent(vehicle_id, command)
        return command.cmd_id

    def acknowledge(self, alert_id: int) -> bool:
        return self.store.acknowledge(alert_id, self.monitor.now)

    def resolve(self, alert_id: int) -> bool:
        return self.store.resolve(alert_id, self.monitor.now)

    def snapshot(self) -> dict:
        """관제 화면에 보낼 현재 상태: 운행 중인 차량, 처리 안 된 알림, 최근 명령."""
        vehicles = [
            {
                "id": vid,
                "state": self.states.get(vid, "online"),
                "held": self.monitor.held(vid),
                **tel.model_dump(),
                "route": list(self.routes.get(vid, ())),
            }
            for vid, tel in sorted(self.latest.items())
        ]
        alerts = [{**asdict(a), "label": LABELS.get(a.type, a.type)} for a in self.store.recent_alerts()]
        commands = [asdict(c) for c in self.store.recent_commands()]
        return {"t": self.monitor.now, "vehicles": vehicles, "alerts": alerts, "commands": commands}

    def _handle_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if not reason_code.is_failure:
            client.subscribe(_SUBSCRIPTIONS)

    def _dispatch(self, topic: str, payload: bytes) -> None:
        if not payload:
            return  # 빈 retain 메시지는 지운다는 뜻이다
        try:
            events = self._handle(topic, payload)
        except ValueError as e:  # pydantic의 ValidationError도 ValueError다
            log.warning("처리할 수 없는 메시지 %s: %s", topic, e)
            return
        for event in events:
            if isinstance(event, AlertRaised):
                self.store.open_alert(event)
            else:
                self.store.clear_alert(event.type, event.subject, event.t)
            if self._on_event is not None:
                self._on_event(event)

    def _handle(self, topic: str, payload: bytes) -> list[AlertEvent]:
        if topic == CLOCK_TOPIC:
            return self.monitor.on_clock(Clock.from_payload(payload))
        vid, kind = parse_vehicle_topic(topic)
        match kind:
            case "telemetry":
                telemetry = Telemetry.from_payload(payload)
                self.store.add_telemetry(vid, telemetry)
                self.latest[vid] = telemetry
                return self.monitor.on_telemetry(vid, telemetry)
            case "status":
                status = Status.from_payload(payload)
                self.store.add_status(vid, status, self.monitor.now)
                self.states[vid] = status.state
                if status.state == "ended":
                    self.latest.pop(vid, None)
                return self.monitor.on_status(vid, status)
            case "route":
                route = Route.from_payload(payload)
                self.store.add_route(vid, route)
                self.routes[vid] = route.edges
                return self.monitor.on_route(vid, route)
            case "cmd_result":
                self.store.record_result(vid, CommandResult.from_payload(payload))
        return []
