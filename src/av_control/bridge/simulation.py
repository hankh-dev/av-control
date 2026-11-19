"""SUMO 시뮬레이션. TraCI를 부르는 코드는 이 모듈에만 둔다."""

import os
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

import traci
import traci.constants as tc
from traci.connection import Connection

from av_control.bridge.sumo_tools import SUMO_HOME, sumo_binary
from av_control.messages import Command, Telemetry

# 구독해 두면 SUMO가 매 스텝 결과에 실어 보내므로, 차량 수가 늘어도 스텝마다 따로 묻지 않는다.
_VEHICLE_VARS = (tc.VAR_POSITION, tc.VAR_SPEED, tc.VAR_ACCELERATION, tc.VAR_ANGLE, tc.VAR_ROAD_ID, tc.VAR_EDGES)
_SIMULATION_VARS = (tc.VAR_TIME, tc.VAR_DEPARTED_VEHICLES_IDS, tc.VAR_ARRIVED_VEHICLES_IDS)
_VEHICLE_TYPE = "av"


@dataclass(frozen=True)
class StepResult:
    t: float
    telemetry: dict[str, Telemetry]  # 지금 도로 위에 있는 차량별 상태
    departed: list[str]  # 이번 스텝에 출발한 차량
    arrived: list[str]  # 이번 스텝에 도착해서 사라진 차량
    routes: dict[str, tuple[str, ...]]  # 출발했거나 경로가 바뀐 차량의 계획 경로


class Simulation:
    """SUMO를 한 스텝씩 돌린다. 차량은 목적지에 닿기 전에 새 목적지를 받아 계속 돈다."""

    def __init__(self, net_file: str | Path, *, seed: int = 0, step_length: float = 1.0) -> None:
        self.net_file = Path(net_file)
        self.seed = seed
        self.step_length = step_length
        self._rng = random.Random(seed)
        self._conn: Connection | None = None
        self._edges: list[str] = []
        self._routes: dict[str, tuple[str, ...]] = {}  # 마지막으로 알린 계획 경로

    def start(self) -> None:
        os.environ.setdefault("SUMO_HOME", str(SUMO_HOME))
        label = f"sim-{id(self)}"
        traci.start(
            [
                str(sumo_binary("sumo")),
                "--net-file", str(self.net_file),
                "--step-length", str(self.step_length),
                "--seed", str(self.seed),
                "--time-to-teleport", "-1",  # 막힌 차량을 순간 이동시키지 않는다
                "--no-step-log", "true",
                "--no-warnings", "true",
            ],
            label=label,
            stdout=subprocess.DEVNULL,
        )
        self._conn = traci.getConnection(label)
        self._conn.simulation.subscribe(_SIMULATION_VARS)
        self._edges = [e for e in self._conn.edge.getIDList() if not e.startswith(":")]
        # 자율주행 차량: 기본 승용차에서 운전 실수(속도를 들쭉날쭉하게 내는 버릇)를 없앤다
        self._conn.vehicletype.copy("DEFAULT_VEHTYPE", _VEHICLE_TYPE)
        self._conn.vehicletype.setImperfection(_VEHICLE_TYPE, 0.0)
        # 평소 감속 상한(m/s²). SUMO 기본값 4.5를 쓰면 회전 전 감속도 급정거만큼 세다.
        # 피할 수 없을 때는 이보다 세게(비상 감속) 선다.
        self._conn.vehicletype.setDecel(_VEHICLE_TYPE, 3.0)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def edges(self) -> list[str]:
        """교차로 내부를 뺀 도로 ID 목록."""
        return list(self._edges)

    @property
    def time(self) -> float:
        return self._sumo.simulation.getTime()

    @property
    def _sumo(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("start()를 먼저 불러야 한다")
        return self._conn

    def add_fleet(self, count: int, prefix: str = "av") -> list[str]:
        """차량 count대를 무작위 출발지·목적지로 1초 간격으로 출발시킨다."""
        ids = []
        for i in range(count):
            vid = f"{prefix}{i:02d}"
            route = self._random_route(self._rng.choice(self._edges))
            self._sumo.route.add(f"route-{vid}", route)
            self._sumo.vehicle.add(vid, f"route-{vid}", typeID=_VEHICLE_TYPE, depart=str(self.time + i))
            ids.append(vid)
        return ids

    def step(self) -> StepResult:
        conn = self._sumo
        conn.simulationStep()
        sim_values = conn.simulation.getSubscriptionResults()
        departed = list(sim_values[tc.VAR_DEPARTED_VEHICLES_IDS])
        arrived = list(sim_values[tc.VAR_ARRIVED_VEHICLES_IDS])
        for vid in departed:
            conn.vehicle.subscribe(vid, _VEHICLE_VARS)

        t = sim_values[tc.VAR_TIME]
        telemetry = {}
        routes = {}
        for vid, values in conn.vehicle.getAllSubscriptionResults().items():
            road, route = values[tc.VAR_ROAD_ID], values[tc.VAR_EDGES]
            if road == route[-1]:  # 목적지 도로에 들어섰으면 도착하기 전에 새 목적지를 준다
                conn.vehicle.changeTarget(vid, self._random_route(road)[-1])
                route = conn.vehicle.getRoute(vid)
            if self._routes.get(vid) != route:  # 출발했거나 경로가 바뀌었다
                self._routes[vid] = routes[vid] = route
            x, y = values[tc.VAR_POSITION]
            telemetry[vid] = Telemetry(
                t=t,
                x=x,
                y=y,
                speed=values[tc.VAR_SPEED],
                accel=values[tc.VAR_ACCELERATION],
                heading=values[tc.VAR_ANGLE],
                road=road,
                destination=route[-1],
            )
        for vid in arrived:
            self._routes.pop(vid, None)
        return StepResult(t=t, telemetry=telemetry, departed=departed, arrived=arrived, routes=routes)

    def apply(self, vehicle_id: str, command: Command) -> tuple[bool, str | None]:
        """원격 명령을 차량에 적용하고 (성공 여부, 실패 이유)를 돌려준다."""
        conn = self._sumo
        if vehicle_id not in conn.vehicle.getIDList():
            return False, "도로 위에 없는 차량"
        try:
            match command.type:
                case "stop":
                    conn.vehicle.setSpeed(vehicle_id, 0.0)
                case "resume":
                    conn.vehicle.setSpeed(vehicle_id, -1)  # -1: 정지 명령을 풀고 원래 주행으로 돌아간다
                case "reroute":
                    conn.vehicle.changeTarget(vehicle_id, str(command.value))
                case "speed_limit":
                    limit = command.value
                    if limit is None:
                        # 제한 해제. 차량 한 대의 속도를 바꾸면 SUMO가 그 차량 전용 차종(av@av00)을 만들어
                        # 제한값이 거기 남으므로, 원래 최고 속도는 기본 차종에서 읽는다.
                        limit = conn.vehicletype.getMaxSpeed(_VEHICLE_TYPE)
                    conn.vehicle.setMaxSpeed(vehicle_id, float(limit))
        except traci.TraCIException as e:
            return False, str(e)
        return True, None

    def _random_route(self, start: str) -> tuple[str, ...]:
        """start에서 무작위 목적지까지 가는 경로 (도로 2개 이상)."""
        for _ in range(50):
            target = self._rng.choice(self._edges)
            if target != start:
                route = self._sumo.simulation.findRoute(start, target).edges
                if len(route) >= 2:
                    return route
        raise RuntimeError(f"{start}에서 갈 수 있는 목적지를 찾지 못했다")
