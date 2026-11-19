"""av-center 명령."""

import argparse
import logging
import signal
import socket
from collections import Counter
from datetime import datetime
from pathlib import Path

import uvicorn

from av_control.center.alerts import LABELS, AlertEvent, AlertRaised
from av_control.center.roadmap import load_map
from av_control.center.service import CenterService
from av_control.center.store import Store
from av_control.center.web import create_app

log = logging.getLogger("av_control.center")


def _log_event(event: AlertEvent) -> None:
    label = LABELS[event.type]
    if isinstance(event, AlertRaised):
        log.info("t=%.0fs [%s] %s: %s", event.t, label, event.subject, event.detail)
    else:
        log.info("t=%.0fs [%s 해제] %s", event.t, label, event.subject)


def _raise_keyboard_interrupt(signum, frame) -> None:
    """kill(SIGTERM)로 끝내도 Ctrl+C처럼 정리하고 끝나게 한다."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="av-center", description="차량 보고를 받아 이상을 감지하고 기록하는 관제 센터.")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="관제를 시작한다 (Ctrl+C로 종료)")
    r.add_argument("--db", help="기록할 SQLite 파일 (기본: data/center-날짜-시각.db)")
    r.add_argument("--net", default="sim/grid.net.xml", help="관제 화면에 그릴 도로망 (기본 sim/grid.net.xml)")
    r.add_argument("--port", type=int, default=8000, help="관제 화면 포트 (기본 8000)")
    r.add_argument("--no-web", action="store_true", help="관제 화면 없이 감지와 기록만 한다")
    r.add_argument("--mqtt-host", default="127.0.0.1")
    r.add_argument("--mqtt-port", type=int, default=1883)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if not args.no_web and not Path(args.net).exists():
        parser.error(f"{args.net} 파일이 없습니다. 먼저 'uv run av-bridge make-grid'를 실행하세요.")
    try:  # 기록 파일을 만들기 전에 브로커가 떠 있는지 본다
        socket.create_connection((args.mqtt_host, args.mqtt_port), timeout=2).close()
    except OSError:
        parser.exit(
            1,
            f"MQTT 브로커({args.mqtt_host}:{args.mqtt_port})에 연결할 수 없습니다. "
            "먼저 'mosquitto -c config/mosquitto.conf'를 실행하세요.\n",
        )

    db = args.db or datetime.now().strftime("data/center-%Y%m%d-%H%M%S.db")
    store = Store(db)
    service = CenterService(store, mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port, on_event=_log_event)
    service.start()
    log.info("관제 시작 (브로커 %s:%d, 기록 %s)", args.mqtt_host, args.mqtt_port, db)

    # uvicorn은 끝날 때 받은 신호를 다시 일으키므로, 여기서 SIGTERM도 KeyboardInterrupt로 받아 정리한다
    previous_handler = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        if args.no_web:
            while True:
                service.process_pending(timeout=1.0)
        else:
            app = create_app(service, load_map(args.net))
            log.info("관제 화면: http://127.0.0.1:%d", args.port)
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    finally:
        # 정리하는 동안 겹쳐 온 종료 신호에 끊기지 않게 한다. uv run으로 띄우면 uv가 신호를 한 번 더 전달하기도 한다.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        service.close()
        counts = Counter(LABELS[a.type] for a in store.alerts())
        store.close()
        signal.signal(signal.SIGTERM, previous_handler)
        summary = ", ".join(f"{label} {n}건" for label, n in counts.most_common()) or "없음"
        log.info("관제 종료. 알림: %s", summary)
