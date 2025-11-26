import requests
import time
from datetime import datetime

url = "http://127.0.0.1:7000/v1/filter/multiple_filter"

dictRequest = {    
    "filter_list": [
        "input_filter",
        "secret_filter"
    ],
    "prompt": "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요",
    
    "user_role": {
        "id": "",
        "email": "",
        "client_host": "",
        "session_id": ""
    }
}

for i in range(2):
    start_dt = datetime.now()
    start_perf = time.perf_counter()
    
    print(f"request - {dictRequest}")

    response = requests.post(url, json=dictRequest)
    json_result = response.json()
    
    print(f"received - {json_result}")

    end_dt = datetime.now()
    end_perf = time.perf_counter()
    elapsed = end_perf - start_perf

    print(f"시작 시간: {start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")}, 종료시간 : {end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")}. 소요 시간: {elapsed:.6f}초")
    
    time.sleep(1)  # 테스트용 딜레이