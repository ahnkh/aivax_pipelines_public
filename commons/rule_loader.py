import sys
import json
import requests

# ==============================
# 🔧 설정 부분 (필요시 수정)
# ==============================
IP = "10.0.17.101"  # ← 여기에 실제 서버 IP 또는 호스트 입력
PORT = "3000"        # 포트가 없으면 "" 로 두세요
TIMEOUT = 10.0
OUTPUT_FILE = "policy_rules.json"  # 응답 저장 파일명 (저장 원치 않으면 None 으로)
# ==============================


def build_url(ip: str, port: str = "") -> str:
    """IP와 포트를 기반으로 최종 요청 URL 구성"""
    if not ip.startswith("http://") and not ip.startswith("https://"):
        ip = "http://" + ip
    if port:
        ip = f"{ip}:{port}"
    return ip.rstrip("/") + "/api/internal/policy/rules"


def main():
    url = build_url(IP, PORT)
    headers = {
        "accept": "application/json",
    }

    print(f"요청: GET {url}")
    print(f"헤더: {headers}")

    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        print("요청 중 오류 발생:", e, file=sys.stderr)
        sys.exit(1)

    print(f"\nHTTP {resp.status_code} {resp.reason}")
    for h in ("Date", "Content-Type", "Content-Length", "Last-Modified"):
        if h in resp.headers:
            print(f"{h}: {resp.headers[h]}")

    if resp.status_code == 304:
        print("\n서버가 'Not Modified(304)'를 응답했습니다. 변경된 내용이 없습니다.")
        sys.exit(0)

    content_type = resp.headers.get("Content-Type", "")
    body_text = resp.text or ""

    if "application/json" in content_type:
        try:
            parsed = resp.json()
            pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            print("\n응답(JSON):\n")
            print(pretty)
            body_to_save = pretty
        except ValueError:
            print("\n응답(원문, JSON 파싱 실패):\n")
            print(body_text)
            body_to_save = body_text
    else:
        print("\n응답(원문):\n")
        print(body_text)
        body_to_save = body_text

    if OUTPUT_FILE:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(body_to_save)
        print(f"\n응답 본문을 파일로 저장했습니다: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
