# CSO worker 4개 부하 테스트

측정 시각: 2026-10-17 18:52 KST.

실제 Pipeline HTTP worker 4개와 worker별 상주 JVM 1개를 사용했다. 별도 loopback 포트의
전용 Valkey와 테스트 파일·로그 디렉터리로 실행했다. 기존 실행 프로세스와 설정은 변경하지 않았다.

## 조건

- Inline 모드, `CSO_ENABLED=true`, `filter_list=["input_filter"]`. 일반 분석 필터는 비활성화했다.
- HTTP 요청 → CSO 선행 가드 → Fasoo 등급 조회 → Valkey INPUT 로그 저장까지 포함한다.
- 측정 당시 매 응답의 debug 로그에서 `grade=3`, 파일별 `status=tagged`,
  상위 `blocked=false`를 검증했다. 현재 스크립트는 변경된 형식인 `cso.status=open`을 검증한다.
- 생성한 O 등급 DOCX 37,192바이트를 반복 사용했다. 파일 캐시가 데워진 조건이다.
- 준비 요청 200건 이후 동시 요청 4·16·64개를 각각 10초간 실행했다. JVM 요청 제한은 5초다.
- 부하 발생기는 aiohttp를 사용했다. worker 분산을 위해 요청마다 TCP 연결을 닫았다.
- 프로세스 CPU affinity는 48개 논리 CPU다. 운영 systemd의 4 CPU 제한을 적용한 결과는 아니다.
- 기존 `CsoGradeHandler.java`·`.class`·Fasoo JAR는 변경하지 않았다.

## 결과

| 동시 요청 | 성공 요청 | 처리량(req/s) | p50(ms) | p95(ms) | p99(ms) | 오류 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | 17,839 | 1,784 | 1.87 | 2.95 | 16.52 | 0 |
| 16 | 26,578 | 2,656 | 5.16 | 18.37 | 21.17 | 0 |
| 64 | 28,742 | 2,866 | 20.17 | 36.76 | 42.34 | 0 |

측정 요청 합계 73,159건, 오류 0건, JVM 재시작 0회다.
4개 JVM의 RSS 합계 최대는 4,052 MiB (3.96 GiB)였다.
JVM 힙 옵션은 따로 지정하지 않았다. 이 측정만으로 메모리 누수 여부나 장시간 안정성을 판단하지 않는다.

각 단계에서 JVM 4개와 Python worker 4개 모두 CPU 사용 증가를 확인했다.
동시 요청 64개에서 부하 발생기 CPU는 한 코어 기준 92.0%였다.
측정 처리량은 이 테스트 조건에서 달성한 값이며, 서버 최대 처리량을 뜻하지 않는다.
실제 대용량 문서·다양한 파일 형식·C/S 차단 후 파일 보관 경로·장시간 부하는 이 결과에 포함하지 않는다.

## 재현

서버에 설치된 `valkey-server`가 PATH에 있어야 한다. 부하 도구는 이 실행 파일로
별도 Valkey 프로세스를 띄우고 임시 데이터·포트를 사용한다.

Pipeline 루트에서 실행한다. 설정 파일과 기존 서비스는 수정하지 않는다.

현재 runner는 일반 DOCX 생성 후 Fasoo `DocProperties.setCTProperty()`로 O 등급을
태깅하고 재조회하여 검증한다. 테스트 데이터 생성용 helper 컴파일에는 JDK 17 이상이
필요하며, 태깅은 부하 측정 전에 수행한다. 위 측정값과 파일 크기는 이전 XML 속성 직접
생성 fixture로 측정한 기록이며, 라이브러리 태깅으로 변경한 성능 수치로 해석하지 않는다.

```bash
/home1/aivax/aivax-venv/bin/python scripts/load_cso.py \
  --workers 4 --concurrency 4 16 64 --seconds 10 --warmup 200
```

실제 O 등급 문서는 `--sample /path/document.docx`로 지정한다. 원본을 테스트 디렉터리로
복사해서 사용한다. `--keepalive`를 추가하면 연결을 재사용하며, 적은 연결 수에서는
일부 worker에 요청이 몰릴 수 있다. 종료 시 테스트 Pipeline과 전용 Valkey를 회수한다.

원본 JSON·프로세스 로그·DOCX는 `.runtime/cso-load-workers4-balanced/`에 있다.
후속 실행은 `.runtime/cso-load/<run-id>/`에 결과를 남긴다.
