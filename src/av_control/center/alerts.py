"""알림 종류와 감지기가 내는 사건."""

from dataclasses import dataclass
from typing import Literal

AlertType = Literal["hard_brake", "long_stop", "off_route", "congestion", "report_lost"]

LABELS: dict[str, str] = {
    "hard_brake": "급정거",
    "long_stop": "오래 멈춤",
    "off_route": "경로 이탈",
    "congestion": "정체",
    "report_lost": "보고 끊김",
}


@dataclass(frozen=True)
class AlertRaised:
    """알림이 새로 울림. subject는 차량 ID이고, 정체만 도로 ID다."""

    type: AlertType
    subject: str
    t: float
    detail: str


@dataclass(frozen=True)
class AlertCleared:
    """알림 조건이 풀림. 관제사의 확인·처리와는 별개다."""

    type: AlertType
    subject: str
    t: float


AlertEvent = AlertRaised | AlertCleared
