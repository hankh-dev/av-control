"""관제 화면 서버 (FastAPI).

지도, 현재 상태, 원격 명령, 알림 확인·처리를 HTTP로 내주고, 현재 상태를 WebSocket으로 밀어 준다.
받은 MQTT 메시지 처리도 이벤트 루프 안의 작업(pump)이 맡는다. 그래서 감지기와 차량 상태는 루프 스레드에서만 다뤄진다.
엔드포인트는 모두 async def여야 한다. 그냥 def로 쓰면 다른 스레드에서 돌아 이 전제가 깨진다.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from av_control.center.roadmap import RoadMap
from av_control.center.service import CenterService

STATIC_DIR = Path(__file__).parent / "static"

log = logging.getLogger(__name__)


class CommandRequest(BaseModel):
    type: Literal["stop", "resume", "reroute", "speed_limit"]
    value: str | float | None = None


def create_app(service: CenterService, road_map: RoadMap, *, push_interval: float = 0.3) -> FastAPI:
    clients: set[WebSocket] = set()

    async def broadcast(data: dict) -> None:
        for ws in list(clients):
            try:
                await ws.send_json(data)
            except Exception:  # 닫힌 화면은 뺀다
                clients.discard(ws)

    async def pump() -> None:
        """0.05초마다 받은 메시지를 처리하고, push_interval마다 화면들에 현재 상태를 보낸다."""
        last_push = 0.0
        while True:
            try:
                service.process_pending(limit=5000)
                if clients and time.monotonic() - last_push >= push_interval:
                    last_push = time.monotonic()
                    await broadcast(service.snapshot())
            except Exception:
                log.exception("메시지 처리 중 오류")
            await asyncio.sleep(0.05)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(pump())
        yield
        task.cancel()

    app = FastAPI(title="자율주행 관제", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/map")
    async def get_map() -> dict:
        return road_map.to_dict()

    @app.get("/api/snapshot")
    async def get_snapshot() -> dict:
        return service.snapshot()

    @app.post("/api/vehicles/{vehicle_id}/commands")
    async def post_command(vehicle_id: str, body: CommandRequest) -> dict:
        if vehicle_id not in service.latest:
            raise HTTPException(404, "운행 중인 차량이 아닙니다")
        try:
            cmd_id = service.send_command(vehicle_id, body.type, body.value)
        except ValidationError as e:
            raise HTTPException(400, e.errors()[0]["msg"].removeprefix("Value error, ")) from e
        return {"cmd_id": cmd_id}

    @app.post("/api/alerts/{alert_id}/acknowledge")
    async def acknowledge(alert_id: int) -> dict:
        if not service.acknowledge(alert_id):
            raise HTTPException(409, "확인할 수 없는 알림입니다 (없거나 이미 확인·처리됨)")
        return {"ok": True}

    @app.post("/api/alerts/{alert_id}/resolve")
    async def resolve(alert_id: int) -> dict:
        if not service.resolve(alert_id):
            raise HTTPException(409, "처리할 수 없는 알림입니다 (없거나 이미 처리됨)")
        return {"ok": True}

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        await websocket.accept()
        clients.add(websocket)
        try:
            await websocket.send_json(service.snapshot())  # 연결하자마자 한 번 보낸다
            while True:
                await websocket.receive_text()  # 받을 내용은 없지만, 연결이 끊긴 걸 알아챈다
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)

    return app
