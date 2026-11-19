"""테스트 공용 픽스처."""

import os
import shutil
import socket
import subprocess
import time

import pytest

from av_control.bridge.sumo_tools import make_grid


def _find_mosquitto() -> str | None:
    found = shutil.which("mosquitto")
    if found:
        return found
    for path in ("/opt/homebrew/sbin/mosquitto", "/usr/local/sbin/mosquitto"):
        if os.path.exists(path):
            return path
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mqtt_port(tmp_path_factory):
    """테스트 동안만 쓰는 Mosquitto 브로커를 빈 포트에 띄우고 그 포트를 돌려준다."""
    exe = _find_mosquitto()
    if exe is None:
        pytest.fail("mosquitto가 필요합니다: brew install mosquitto")

    port = _free_port()
    workdir = tmp_path_factory.mktemp("mosquitto")
    conf = workdir / "mosquitto.conf"
    conf.write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n")
    log_path = workdir / "mosquitto.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen([exe, "-c", str(conf)], stdout=log, stderr=subprocess.STDOUT)

    deadline = time.monotonic() + 5
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail(f"mosquitto 실행 실패:\n{log_path.read_text()}")
            if time.monotonic() > deadline:
                proc.kill()
                pytest.fail("mosquitto가 5초 안에 뜨지 않았습니다")
            time.sleep(0.05)

    yield port

    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="session")
def grid_net(tmp_path_factory):
    """테스트용 격자 도로망 (교차로 5×5, 사거리에 신호등)."""
    return make_grid(tmp_path_factory.mktemp("net") / "grid.net.xml")
