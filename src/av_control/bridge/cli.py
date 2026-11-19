"""av-bridge 명령."""

import argparse
import logging
import signal
from pathlib import Path

from av_control.bridge.app import Bridge, run
from av_control.bridge.simulation import Simulation
from av_control.bridge.sumo_tools import make_grid

DEFAULT_NET = "sim/grid.net.xml"


def _raise_keyboard_interrupt(signum, frame) -> None:
    """kill(SIGTERM)로 끝내도 Ctrl+C처럼 차량들의 운행 종료를 알리고 끝나게 한다."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="av-bridge", description="SUMO로 자율주행 차량들을 돌리며 관제 센터에 MQTT로 보고한다."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    grid = sub.add_parser("make-grid", help="격자 도로망을 만든다")
    grid.add_argument("-o", "--output", default=DEFAULT_NET, help=f"저장할 파일 (기본 {DEFAULT_NET})")
    grid.add_argument("--junctions", type=int, default=5, help="한 방향 교차로 수 (기본 5)")
    grid.add_argument("--length", type=float, default=200.0, help="블록 길이, m (기본 200)")

    r = sub.add_parser("run", help="차량들을 운행하며 보고한다")
    r.add_argument("--net", default=DEFAULT_NET, help=f"도로망 파일 (기본 {DEFAULT_NET})")
    r.add_argument("--vehicles", type=int, default=30, help="차량 수 (기본 30)")
    r.add_argument("--seed", type=int, default=0, help="출발지·목적지를 고르는 난수 시드 (기본 0)")
    r.add_argument("--rate", type=float, default=1.0, help="배속. 1이면 실제 시간, 0이면 최대 속도 (기본 1)")
    r.add_argument("--duration", type=float, help="이 시뮬레이션 시각(초)까지 돌린다. 없으면 Ctrl+C까지")
    r.add_argument("--mqtt-host", default="127.0.0.1")
    r.add_argument("--mqtt-port", type=int, default=1883)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.command == "make-grid":
        path = make_grid(args.output, junctions=args.junctions, block_length=args.length)
        print(f"도로망을 만들었습니다: {path}")
        return

    if not Path(args.net).exists():
        parser.error(f"{args.net} 파일이 없습니다. 먼저 'av-bridge make-grid'를 실행하세요.")

    bridge = Bridge(Simulation(args.net, seed=args.seed), mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port)
    try:
        bridge.start(args.vehicles)
    except ConnectionRefusedError:
        bridge.close()
        parser.exit(
            1,
            f"MQTT 브로커({args.mqtt_host}:{args.mqtt_port})에 연결할 수 없습니다. "
            "먼저 'mosquitto -c config/mosquitto.conf'를 실행하세요.\n",
        )
    logging.info("차량 %d대 운행 시작 (브로커 %s:%d, 배속 %s)", args.vehicles, args.mqtt_host, args.mqtt_port, args.rate)
    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        run(bridge, rate=args.rate, duration=args.duration)
    except KeyboardInterrupt:
        pass
    finally:
        # 정리하는 동안 겹쳐 온 종료 신호에 끊기지 않게 한다. uv run으로 띄우면 uv가 신호를 한 번 더 전달하기도 한다.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        bridge.close()
        signal.signal(signal.SIGTERM, previous_handler)
        logging.info("운행 종료")
