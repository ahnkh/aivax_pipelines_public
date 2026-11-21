# ai_engine — logrotate

`aivax-engine-inline` / `aivax-engine-mirror` 는 systemd `StandardOutput=append:` 로
`/home1/aivax/ai_engine/ai_engine.log`(및 SHM 로그)에 계속 씁니다.
unit 에 `--log-payload` 가 켜져 있으면 Deep 트래픽마다 페이로드 미리보기가 붙어서
일일 OS logrotate 만으로는 파일이 한도 없이 커질 수 있습니다.

## 대상

| 파일 | 생성 주체 |
|------|-----------|
| `ai_engine.log` | `aivax-engine-inline` stdout/stderr |
| `ai_engine_shm.log` | Mirror SHM consumer stdout/stderr (`missingok`) |

## 설치

인스톨러가 같이 깔거나, 엔진을 재시작하지 않고 이것만:

```bash
sudo bash /home1/aivax/ai_engine/logrotate/install.sh
```

경로를 바꾸면 `AIVAX_ENGINE_LOG` / `AIVAX_ENGINE_SHM_LOG` 를 주고 같은 스크립트를 돌립니다.

## 설정 요약

| 옵션 | 값 | 설명 |
|------|-----|------|
| `daily` | — | 하루 한 번은 회전 |
| `maxsize 1G` | — | 1GB 를 넘으면 같은 날에도 회전 (`size` 와 달리 일일과 같이 씀) |
| `rotate 14` | — | `.1` … `.14.gz` 보관 (**하루 1회면 약 2주**, 파일당 최대 약 14G) |
| `copytruncate` | — | 열린 fd 를 유지한 채 내용만 자름 (엔진 재시작 없음) |
| timer | 매시 00분 | `run-hourly.sh` 가 `aivax-engine` / `aivax-mirror` / `aivax-apps` 를 같이 검사. 1GB 미만이면 파일은 그대로 |

### copytruncate 를 쓰는 이유

이름만 바꾸면 systemd/python 은 **옛 inode** 에 계속 쓰고, 새 `ai_engine.log` 는 비어 보입니다.

## 검증

```bash
sudo logrotate -d /etc/logrotate.d/aivax-engine
systemctl list-timers aivax-engine-logrotate.timer
ls -lh /home1/aivax/ai_engine/ai_engine.log*
```

강제 1회 (엔진 PID·inode 가 그대로여야 함):

```bash
pid=$(systemctl show aivax-engine-inline -p MainPID --value)
ino=$(stat -c %i /home1/aivax/ai_engine/ai_engine.log)
sudo logrotate -f /etc/logrotate.d/aivax-engine
test "$pid" = "$(systemctl show aivax-engine-inline -p MainPID --value)"
test "$ino" = "$(stat -c %i /home1/aivax/ai_engine/ai_engine.log)"
ls -lh /home1/aivax/ai_engine/ai_engine.log*
```

같은 timer 가 같이 검사하는 다른 설정:

| 파일 | 대상 | 한도 |
|------|------|------|
| `/etc/logrotate.d/aivax-mirror` | `mirror_engine/log/*.log` (http-logger 포함) | 1G × 14 |
| `/etc/logrotate.d/aivax-apps` | pipeline/toolkit **stderr**, vhost-deep-bridge | stderr 200M × 30, vhost 200M × 14 |

pipeline/toolkit 은 stdout → journal, stderr → 파일 (하이브리드).
Envoy/agents 는 journal 유지 + `journald.conf.d/aivax.conf` (`SystemMaxUse=512M`).

## 제거

```bash
sudo systemctl disable --now aivax-engine-logrotate.timer
sudo rm -f /etc/logrotate.d/aivax-engine \
  /etc/systemd/system/aivax-engine-logrotate.service \
  /etc/systemd/system/aivax-engine-logrotate.timer
sudo systemctl daemon-reload
```
