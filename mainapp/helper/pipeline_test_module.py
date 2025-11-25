
import socket
import select

#외부 라이브러리
from lib_include import *

from type_hint import *

'''
테스트 모듈 추가
'''

class PipelineTestModule:
    
    def __init__(self):
        
        pass
    
    def test(self):
        
        # self.testSqlprintf()
        
        self.testIPCClient()
        
        pass
    
    
    #sqlprint, 테스트.
    def testSqlprintf(self):
        
        '''
        '''
        
        LOG().debug("test sqlprint")
        
        # ai 서비스, 계정 추가, insert or replace

        dictDBInfo = {
            "user_id" : "ghahn",
            "reg_date" : datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "email" : "ghahn@wins21.co.kr",
            "ai_service" : 0, #순차적으로 GPT, claude, gemini, copilot, ..
            "etc_comment" : "", #comment
            "use_flag" : 1, #1:활성, 0:비활성
        }
        
        dictDBResult:dict = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_insert_update_ai_user_account", dictDBInfo, dictDBResult)
        
        LOG().debug(f"insert user account, result = {dictDBResult}")
        
        # ai 서비스, 계정 조회
        dictDBResult = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_user_account", {"where" : "", "limit":1}, dictDBResult)
        
        LOG().debug(f"select user account = {dictDBResult}")
        
        pass
    
    #ipc 통신, 클라이언트 테스트
    def testIPCClient(self):
        
        '''
        단순 테스트, claude로 구현
        '''
        
        LOG().debug("test ipc client")
        
        SOCKET_PATH = "/tmp/pipeline.sock"
        BUFFER_SIZE = 4096
        TIMEOUT = 1  # 초 단위
        
        #AF_UNIX, linux만 제공.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(SOCKET_PATH)
        sock.setblocking(False)  # 논블로킹 모드
        
        buffer = bytearray()

        # 반복 전송 테스트용
        for i in range(2):
            
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
            print(f"Sent: {dictRequest}")

            buffer = bytearray()
            while True:
                
                ready_to_read, _, _ = select.select([sock], [], [], TIMEOUT)
                
                if not ready_to_read:
                    print("No data received (timeout)")
                    break  # 데이터가 없으면 루프 탈출
                                
                chunk = sock.recv(BUFFER_SIZE)
                    
                if not chunk:
                    print("Server closed connection")
                    break
                
                buffer.extend(chunk)
                jsonResult = json.loads(buffer.decode("utf-8"))
                    
                LOG().debug(f"received buffer = {jsonResult}")
                
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
                #         print(f"Invalid JSON received: {raw_msg}")

            time.sleep(1)  # 테스트용 딜레이
        
        # ipcTestClient.start()
        
        # # 2. 데이터 처리 요청
        # response = ipcTestClient.send_request_sync(
        #     action="process_data",
        #     data={"data": [1, 2, 3, 4, 5]},
        #     timeout=5.0
        # )
        
        # if response:
        #     LOG().info(f"Processed result: {response.get('result')}")
            
            
        # # 3. 비동기 요청
        # LOG().info("\n=== Test 3: Async Requests ===")
        
        # def async_callback(resp):
        #     LOG().info(f"Async response: {resp}")
        
        # for i in range(3):
        #     ipcTestClient.send_request(
        #         action="process_data",
        #         data={"data": [i, i*2, i*3]},
        #         callback=async_callback
        #     )
        
        # time.sleep(3)
        
        # # 통계 출력 => 불필요, 제거
        # logger.info(f"\n=== Statistics ===")
        # stats = client.get_stats()
        # for key, value in stats.items():
        #     logger.info(f"{key}: {value}")
        
        # ipcTestClient.stop()