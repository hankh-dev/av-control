"""차량과 관제 센터가 주고받는 메시지와 토픽.

bridge와 center가 함께 import하는 유일한 모듈이다. 내용은 JSON으로 보낸다.

| 토픽                     | 방향              | QoS | retain | 메시지              |
|--------------------------|-------------------|-----|--------|---------------------|
| vehicles/{id}/telemetry  | 차량 → 관제       | 0   | 아니오 | Telemetry (1초마다) |
| vehicles/{id}/status     | 차량 → 관제       | 1   | 예     | Status              |
| vehicles/{id}/route      | 차량 → 관제       | 1   | 예     | Route (출발, 경로 변경) |
| vehicles/{id}/cmd        | 관제 → 차량       | 1   | 아니오 | Command             |
| vehicles/{id}/cmd_result | 차량 → 관제       | 1   | 아니오 | CommandResult       |
| sim/clock                | 시뮬레이터 → 관제 | 0   | 아니오 | Clock (매 스텝)     |

시각 t는 모두 시뮬레이션 시각(초)이다. 관제 센터는 벽시계가 아니라 이 시각으로 판단한다.
그래야 평가할 때 최대 속도로 돌려도, 감지 결과와 감지까지 걸린 시간이 실제 시간으로 돌릴 때와 같다.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

TELEMETRY_QOS = 0
STATUS_QOS = 1
ROUTE_QOS = 1
COMMAND_QOS = 1
RESULT_QOS = 1
CLOCK_QOS = 0

CLOCK_TOPIC = "sim/clock"
ALL_TELEMETRY = "vehicles/+/telemetry"
ALL_STATUS = "vehicles/+/status"
ALL_ROUTES = "vehicles/+/route"
ALL_RESULTS = "vehicles/+/cmd_result"

_KINDS = ("telemetry", "status", "route", "cmd", "cmd_result")


def _vehicle_topic(vehicle_id: str, kind: str) -> str:
    if not vehicle_id or any(c in vehicle_id for c in "/+#"):
        raise ValueError(f"토픽에 쓸 수 없는 차량 ID: {vehicle_id!r}")
    return f"vehicles/{vehicle_id}/{kind}"


def telemetry_topic(vehicle_id: str) -> str:
    return _vehicle_topic(vehicle_id, "telemetry")


def status_topic(vehicle_id: str) -> str:
    return _vehicle_topic(vehicle_id, "status")


def route_topic(vehicle_id: str) -> str:
    return _vehicle_topic(vehicle_id, "route")


def command_topic(vehicle_id: str) -> str:
    return _vehicle_topic(vehicle_id, "cmd")


def command_result_topic(vehicle_id: str) -> str:
    return _vehicle_topic(vehicle_id, "cmd_result")


def parse_vehicle_topic(topic: str) -> tuple[str, str]:
    """'vehicles/av01/telemetry' → ('av01', 'telemetry')"""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "vehicles" or not parts[1] or parts[2] not in _KINDS:
        raise ValueError(f"차량 토픽이 아님: {topic!r}")
    return parts[1], parts[2]


class Message(BaseModel):
    """모든 메시지의 바탕. 정의에 없는 필드가 오면 거부한다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_payload(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_payload(cls, payload: bytes | str) -> Self:
        return cls.model_validate_json(payload)


class Telemetry(Message):
    """차량 상태 보고. 값의 범위는 검사하지 않는다. 이상한 값을 가려내는 건 감지기의 몫이다."""

    t: float  # 시뮬레이션 시각 (초)
    x: float  # 위치 (m, 도로망 좌표)
    y: float
    speed: float  # m/s
    accel: float  # m/s², 감속하면 음수
    heading: float  # 도. 북쪽이 0이고 시계 방향으로 커진다
    road: str  # 지금 달리는 도로 ID. 교차로 안이면 ':'로 시작한다
    destination: str  # 목적지 도로 ID


class Status(Message):
    """차량 연결 상태. 비정상으로 끊기면 브로커가 Last Will로 offline을 대신 보낸다."""

    state: Literal["online", "offline", "ended"]  # ended: 운행을 정상적으로 마침
    t: float | None = None  # online과 offline은 시각 없이 온다


class Route(Message):
    """차량의 계획 경로. 출발할 때와 경로가 바뀔 때마다 보낸다. 관제 센터는 이걸로 경로 이탈을 판단한다."""

    t: float
    edges: tuple[str, ...]  # 지나갈 도로 ID를 순서대로


class Command(Message):
    """관제 센터가 차량에 보내는 원격 명령."""

    cmd_id: str
    type: Literal["stop", "resume", "reroute", "speed_limit"]
    # reroute: 목적지 도로 ID / speed_limit: 최고 속도(m/s), null이면 제한 해제 / stop, resume: 없음
    value: str | float | None = None

    @model_validator(mode="after")
    def _check_value(self) -> Self:
        match self.type:
            case "stop" | "resume" if self.value is not None:
                raise ValueError(f"{self.type} 명령에는 값이 없어야 한다")
            case "reroute" if not isinstance(self.value, str) or not self.value:
                raise ValueError("reroute 명령의 값은 목적지 도로 ID여야 한다")
            case "speed_limit" if self.value is not None and (isinstance(self.value, str) or self.value <= 0):
                raise ValueError("speed_limit 명령의 값은 양수(m/s)이거나 null이어야 한다")
        return self


class CommandResult(Message):
    """차량이 명령을 처리한 결과."""

    cmd_id: str
    ok: bool
    reason: str | None = None  # 실패한 이유
    t: float  # 처리한 시뮬레이션 시각


class Clock(Message):
    """시뮬레이션 시각. 관제 센터가 '지금'을 알 때 쓴다. 실제 관제에서 GPS 시각에 해당한다."""

    t: float
