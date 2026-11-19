"""bridge 본체: 시뮬레이션을 한 스텝씩 돌리며 명령을 적용하고, 차량별 보고를 MQTT로 보낸다."""

import logging
import queue
import time

import paho.mqtt.client as mqtt

from av_control.bridge.link import IncomingCommand, VehicleLink
from av_control.bridge.simulation import Simulation, StepResult
from av_control.messages import CLOCK_QOS, CLOCK_TOPIC, Clock, CommandResult, Route

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, sim: Simulation, *, mqtt_host: str = "127.0.0.1", mqtt_port: int = 1883) -> None:
        self.sim = sim
        self._host = mqtt_host
        self._port = mqtt_port
        self._links: dict[str, VehicleLink] = {}
        self._commands: queue.Queue[IncomingCommand] = queue.Queue()  # MQTT 스레드 → 메인 스레드
        self._clock = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bridge-clock")
        self._sim_started = False

    def start(self, vehicles: int, prefix: str = "av") -> None:
        """브로커에 연결하고 SUMO를 띄운 뒤 차량을 출발시킨다. 브로커가 없으면 ConnectionRefusedError."""
        self._clock.connect(self._host, self._port)
        self._clock.loop_start()
        self.sim.start()
        self._sim_started = True
        self.sim.add_fleet(vehicles, prefix)

    def step(self) -> StepResult:
        self._apply_commands()
        result = self.sim.step()
        for vid in result.departed:
            link = VehicleLink(vid, self._host, self._port, self._commands.put)
            link.connect()
            self._links[vid] = link
        for vid, edges in result.routes.items():  # 보고보다 계획 경로를 먼저 보낸다
            self._links[vid].send_route(Route(t=result.t, edges=edges))
        self._clock.publish(CLOCK_TOPIC, Clock(t=result.t).to_payload(), qos=CLOCK_QOS)
        for vid, telemetry in result.telemetry.items():
            self._links[vid].report(telemetry)
        for vid in result.arrived:
            self._links.pop(vid).end(result.t)
        return result

    def cut_connection(self, vehicle_id: str) -> None:
        """차량 한 대의 연결을 정상 종료 없이 끊는다 (통신 두절 흉내)."""
        self._links[vehicle_id].cut()

    def close(self) -> None:
        if self._sim_started:
            t = self.sim.time
            for link in self._links.values():
                link.end(t)
            self._links.clear()
            self.sim.close()
            self._sim_started = False
        self._clock.disconnect()
        self._clock.loop_stop()

    def _apply_commands(self) -> None:
        """MQTT 스레드가 받아 둔 명령을 메인 스레드에서 적용한다. TraCI는 한 스레드에서만 불러야 한다."""
        while True:
            try:
                incoming = self._commands.get_nowait()
            except queue.Empty:
                return
            link = self._links.get(incoming.vehicle_id)
            if link is None:
                continue  # 그사이 운행을 마친 차량
            if incoming.command is None:
                ok, reason = False, incoming.error
            else:
                ok, reason = self.sim.apply(incoming.vehicle_id, incoming.command)
            link.send_result(CommandResult(cmd_id=incoming.cmd_id, ok=ok, reason=reason, t=self.sim.time))
            log.info("%s 명령 %s: %s", incoming.vehicle_id, incoming.cmd_id, "성공" if ok else f"실패 ({reason})")


def run(bridge: Bridge, *, rate: float = 1.0, duration: float | None = None) -> None:
    """duration(시뮬레이션 초)까지 돌린다. rate는 배속이고, 0이면 기다리지 않고 최대 속도로 돌린다."""
    tick = bridge.sim.step_length / rate if rate > 0 else 0.0
    next_at = time.monotonic()
    while duration is None or bridge.sim.time < duration:
        result = bridge.step()
        if result.t % 60 == 0:
            log.info("t=%.0fs 운행 중 %d대", result.t, len(result.telemetry))
        if tick:
            next_at += tick
            delay = next_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_at = time.monotonic()  # 밀렸으면 몰아서 따라잡지 않는다
