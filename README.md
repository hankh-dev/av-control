# av-control

SUMO로 돌리는 자율주행 차량들을 실시간으로 지켜보고, 이상 상황을 감지해 알리고, 원격 명령을 보내는 관제 시스템입니다.

## 준비

```bash
brew install uv mosquitto
uv sync
```

## 실행

```bash
uv run av-bridge make-grid                   # 처음 한 번: 격자 도로망 만들기
mosquitto -c config/mosquitto.conf           # 터미널 1: MQTT 브로커
uv run av-center run                         # 터미널 2: 관제 센터. 화면은 http://127.0.0.1:8000
uv run av-bridge run                         # 터미널 3: 차량 30대 운행 (Ctrl+C로 종료, --rate 5면 5배속)
mosquitto_sub -t 'vehicles/#' -v             # 차량 보고 엿보기
mosquitto_pub -t 'vehicles/av05/cmd' -m '{"cmd_id":"c1","type":"stop"}'   # 관제 센터를 거치지 않은 정지
```

관제 화면에서 차량을 누르면 정지, 재출발, 속도 제한, 목적지 변경(지도에서 도로 클릭) 명령을 보낼 수 있고, 알림을 확인·처리할 수 있습니다.

관제 화면에서 세운 차는 관제 센터가 알고 있으므로 알림이 울리지 않습니다. 반대로 `mosquitto_pub`으로 몰래 세운 차는 관제 센터가 모르는 정지라서 60초 뒤 '오래 멈춤'으로 잡힙니다.

## 테스트

```bash
uv run pytest
```
