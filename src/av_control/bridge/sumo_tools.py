"""SUMO 실행 파일 위치와 도로망 만들기."""

import os
import subprocess
from pathlib import Path

import sumo

SUMO_HOME = Path(sumo.SUMO_HOME)


def sumo_binary(name: str) -> Path:
    """eclipse-sumo 패키지에 들어 있는 SUMO 실행 파일 경로 (sumo, netgenerate 등)."""
    return SUMO_HOME / "bin" / name


def make_grid(path: str | Path, *, junctions: int = 5, block_length: float = 200.0) -> Path:
    """격자 도로망을 만든다. 신호등은 사거리에만 둔다.

    junctions=5면 교차로가 5×5개, 블록이 4×4개다. 도로는 방향마다 1차로이고 제한 속도는 50km/h(13.89m/s)다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sumo_binary("netgenerate"),
            "--grid",
            f"--grid.number={junctions}",
            f"--grid.length={block_length}",
            # 들어오는 차로들의 제한 속도 합이 기준을 넘는 교차로에 신호등을 둔다.
            # 1차로·13.89m/s 기준으로 사거리는 55.6, 삼거리는 41.7이라 50이면 사거리만 해당한다.
            "--tls.guess", "true",
            "--tls.guess.threshold", "50",
            "-o", path,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "SUMO_HOME": str(SUMO_HOME)},
    )
    return path
