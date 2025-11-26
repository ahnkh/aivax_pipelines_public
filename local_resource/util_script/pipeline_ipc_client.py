

import socket
import select
import time

import json

from datetime import datetime

'''
pipeline, ipc 통신 테스트
'''

SOCKET_PATH = "/tmp/pipeline.sock"
BUFFER_SIZE = 4096
TIMEOUT = 0.001  # 초 단위

def test_ipc_client():
    
    #AF_UNIX, linux만 제공.
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(SOCKET_PATH)
    sock.setblocking(False)  # 논블로킹 모드
    
    buffer = bytearray()
    
    for i in range(2):
        
            #시간 측정            
            start_dt = datetime.now()
            
            start = time.perf_counter()
            
            dictRequest = {
                "router.point" : "multiple_filter",
                "filter_list": [
                    "input_filter",
                    "secret_filter"
                ],
                "prompt": "내 API key는 API_key=sk-1234567-0000-abdcdef 인데 이걸로 어떻게 OpenAI 로 KEY를 전달하는지 예제를 알려주세요",
                
                "user_id" : "",
                "email" : "",
                "client_host" : "",
                "session_id" : ""
            }

            data = json.dumps(dictRequest).encode("utf-8") + b"\n"
            sock.sendall(data)
            print(f"request - {dictRequest}")
            
            buffer = bytearray()
            
            # while True:
            #     try:
            #         chunk = sock.recv(BUFFER_SIZE)
            #         if not chunk:
            #             print("Server closed connection")
            #             break
            #         buffer.extend(chunk)
            #     except BlockingIOError:
            #         # 읽을 데이터가 없으면 루프 탈출
            #         break
            
            # # 수신된 데이터 처리
            # if buffer:
            #     jsonResult = json.loads(buffer.decode("utf-8"))
            #     print(f"received - {jsonResult}")
                
            
            while True:
                
                ready_to_read, _, _ = select.select([sock], [], [], TIMEOUT)
                
                if not ready_to_read:
                    # print("No data received (timeout)")
                    break  # 데이터가 없으면 루프 탈출
                                
                chunk = sock.recv(BUFFER_SIZE)
                    
                if not chunk:
                    print("Server closed connection")
                    break
                
                buffer.extend(chunk)
                jsonResult = json.loads(buffer.decode("utf-8"))
                    
                # LOG().debug(f"received buffer = {jsonResult}")
                print(f"received - {jsonResult}")
                
                del buffer[:]
                
                # chunk = sock.recv(BUFFER_SIZE)
                # if not chunk:
                #     # 서버가 연결 종료
                #     break
                # buffer.extend(chunk)

                # 개행 기준으로 메시지 파싱
                # while b"\n" in buffer:
                #     idx = buffer.index(b"\n")
                #     raw_msg = buffer[:idx]
                #     del buffer[:idx + 1]  # 버퍼에서 해당 메시지 제거

                #     try:
                #         msg = json.loads(raw_msg.decode("utf-8"))
                #         print(f"Received: {msg}")
                #     except json.JSONDecodeError:
                #         print(f"Invalid JSON received: {raw_msg}").
            
            #시간 측정
            
            end_dt = datetime.now()
            end = time.perf_counter()
            elapsed = end - start
            print(f"시작 시간: {start_dt.strftime("%Y-%m-%d %H:%M:%S.%f")}, 종료시간 : {end_dt.strftime("%Y-%m-%d %H:%M:%S.%f")}. 소요 시간: {elapsed:.6f}초")

            time.sleep(1)  # 테스트용 딜레이
    
    pass


test_ipc_client()