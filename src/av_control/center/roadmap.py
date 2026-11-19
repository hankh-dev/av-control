"""SUMO 도로망 파일(.net.xml)에서 화면에 그릴 도로와 교차로 모양을 읽는다.

정적인 지도 파일만 읽고 시뮬레이션 상태는 보지 않으므로, '관제 센터는 보고한 정보만 안다'는 원칙과 맞다.
sumolib 없이 XML을 직접 읽는다.
"""

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

Point = tuple[float, float]


@dataclass(frozen=True)
class Road:
    id: str
    lanes: tuple[tuple[Point, ...], ...]  # 차로마다 중심선 좌표 (m)


@dataclass(frozen=True)
class Junction:
    id: str
    shape: tuple[Point, ...]  # 교차로 영역 다각형
    traffic_light: bool


@dataclass(frozen=True)
class RoadMap:
    bounds: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax
    roads: tuple[Road, ...]
    junctions: tuple[Junction, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _points(shape: str) -> tuple[Point, ...]:
    return tuple((float(p.split(",")[0]), float(p.split(",")[1])) for p in shape.split())


def load_map(path: str | Path) -> RoadMap:
    root = ET.parse(path).getroot()
    xmin, ymin, xmax, ymax = (float(v) for v in root.find("location").get("convBoundary").split(","))
    roads = tuple(
        Road(edge.get("id"), tuple(_points(lane.get("shape")) for lane in edge.findall("lane")))
        for edge in root.findall("edge")
        if edge.get("function") != "internal"  # 교차로 안의 연결 차로는 그리지 않는다
    )
    junctions = tuple(
        Junction(j.get("id"), _points(j.get("shape")), j.get("type") == "traffic_light")
        for j in root.findall("junction")
        if j.get("type") != "internal" and j.get("shape")
    )
    return RoadMap((xmin, ymin, xmax, ymax), roads, junctions)
