"""차량 한 대의 MQTT 연결."""

import json
import socket
from collections.abc import Callable
from dataclasses import dataclass

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from av_control.messages import (
    COMMAND_QOS,
    RESULT_QOS,
    ROUTE_QOS,
    STATUS_QOS,
    TELEMETRY_QOS,
    Command,
    CommandResult,
    Route,
    Status,
    Telemetry,
    command_result_topic,
    command_topic,
    route_topic,
    status_topic,
    telemetry_topic,
)


@dataclass(frozen=True)
class IncomingCommand:
    """관제 센터에서 받은 명령. 형식이 틀렸으면 command가 비어 있고 error에 이유가 담긴다."""

    vehicle_id: str
    cmd_id: str
    command: Command | None
    error: str | None = None


class VehicleLink:
    """차량 한 대의 MQTT 연결.

    차량마다 연결을 따로 두어서, 비정상으로 끊기면 브로커가 Last Will로 offline을 알린다.
    받은 명령은 MQTT 스레드에서 on_command로 넘기기만 한다. 적용은 메인 스레드가 한다.
    """

    def __init__(self, vehicle_id: str, host: str, port: int, on_command: Callable[[IncomingCommand], None]) -> None:
        self.vehicle_id = vehicle_id
        self._host = host
        self._port = port
        self._on_command = on_command
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=vehicle_id)
        self._client.will_set(
            status_topic(vehicle_id), Status(state="offline").to_payload(), qos=STATUS_QOS, retain=True
        )
        self._client.on_connect = self._handle_connect
        self._client.on_message = self._handle_message

    def connect(self) -> None:
        self._client.connect(self._host, self._port)
        self._client.loop_start()

    def report(self, telemetry: Telemetry) -> None:
        self._client.publish(telemetry_topic(self.vehicle_id), telemetry.to_payload(), qos=TELEMETRY_QOS)

    def send_result(self, result: CommandResult) -> None:
        self._client.publish(command_result_topic(self.vehicle_id), result.to_payload(), qos=RESULT_QOS)

    def send_route(self, route: Route) -> None:
        self._client.publish(route_topic(self.vehicle_id), route.to_payload(), qos=ROUTE_QOS, retain=True)

    def cut(self) -> None:
        """정상 종료 없이 연결을 끊는다. 브로커가 Last Will로 offline을 알리고, 잠시 뒤 저절로 다시 연결된다."""
        sock = self._client.socket()
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)

    def end(self, t: float) -> None:
        """운행을 정상 종료한다. ended를 알린 뒤 끊으므로 Last Will은 나가지 않는다."""
        info = self._client.publish(
            status_topic(self.vehicle_id), Status(state="ended", t=t).to_payload(), qos=STATUS_QOS, retain=True
        )
        try:
            info.wait_for_publish(timeout=5)
        except (RuntimeError, ValueError):
            pass  # 이미 연결이 끊겨 알릴 수 없다
        self._client.disconnect()
        self._client.loop_stop()

    def _handle_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            return  # loop_start의 네트워크 스레드가 다시 연결을 시도한다
        client.subscribe(command_topic(self.vehicle_id), qos=COMMAND_QOS)
        client.publish(status_topic(self.vehicle_id), Status(state="online").to_payload(), qos=STATUS_QOS, retain=True)

    def _handle_message(self, client, userdata, message) -> None:
        try:
            command = Command.from_payload(message.payload)
        except ValidationError as e:
            error = f"명령 형식 오류: {e.errors()[0]['msg']}"
            self._on_command(IncomingCommand(self.vehicle_id, _cmd_id_of(message.payload), None, error))
            return
        self._on_command(IncomingCommand(self.vehicle_id, command.cmd_id, command))


def _cmd_id_of(payload: bytes) -> str:
    """형식이 틀린 명령에서도 cmd_id는 최대한 꺼내서, 관제 센터가 결과를 짝지을 수 있게 한다."""
    try:
        cmd_id = json.loads(payload).get("cmd_id")
    except (ValueError, AttributeError):
        return ""
    return cmd_id if isinstance(cmd_id, str) else ""
