

import socket
import select
# import selectors
import threading
import struct
from pathlib import Path

# import asyncio
# from collections import deque
# from typing import Dict, Deque, Any

# from dataclasses import dataclass

from lib_include import *

from ipc_modules.sub_modules.ipc_request_router import IPCRequestRouter

'''
api 외 ipc 통신 모듈의 추가
'''

# 프레임: 4바이트 big-endian 길이 + JSON payload
# HEADER_SIZE = 4

# @dataclass
class ClientConnection:
    '''
    '''
    
    # self.sock = sock
    # fd: int
    # addr: str
    # recv_buffer: bytearray
    # send_queue: bytearray
    
    def __init__(self, sock: socket.socket):
        
        self.sock = sock
        self.fd = sock.fileno()
        self.recv_buffer = bytearray()
        self.send_buffer = bytearray()    
        pass

class IPCPipelineServer:
    
    #buffer size, 4096
    BUFFER_SIZE = 4096
    
    def __init__(self):
        
        # #ipc socket
        # self.__socket:socket.socket = None
        
        # self.__selector:selectors.DefaultSelector = None
        
        # self.__dictIPCPipelineServerLocalConfig:dict = None
        
        # #접속 세션 관리
        # self.__dictConnectionMap: Dict[int, IPCConnectionInfo] = {}
        
        #스레드 lock
        self.__lock:Any = threading.Lock()
        
        #select.epoll은 linux만 지원.
        self._epoll: Optional[select.epoll] = None
        
        self._connections: Dict[int, ClientConnection] = None #TODO: 우선 실행후, 리펙토링
        
        pass
    
    def Initialize(self, mainApp:Any, dictJsonLocalConfigRoot:dict):
        
        '''
        자기 자신의 전달, 주의 => 통신을 수신후, mainApp를 통해서 pipeline filter등을 호출하는 로직이 필요하다.
        이 부분에 대한 고민.
        TODO: ipc통신은 bind 수행시 경로 정보가 필요하다.
        
        설정 정보를 수집후, socket 시작 모듈은 별도 수행이 되는 구조로 구성한다.
        '''
        
        self._connections: Dict[int, ClientConnection] = {}
        
        #ipc pipeline server 정보, 이건 가지고 있자.
        #수정 불필요, 참조로 복사한다.
        ipc_pipeline_server:dict = dictJsonLocalConfigRoot.get("ipc_pipeline_server")
        
        # self.__dictIPCPipelineServerLocalConfig = ipc_pipeline_server
                        
        thread = threading.Thread(name="ipc socket thread", target=self.ThreadHandlerProc, daemon=True, args=(mainApp, ipc_pipeline_server,))
        thread.start()
        
        return ERR_OK
    
    def ThreadHandlerProc(self, mainApp:Any, dictIPCPipelineServerLocalConfig:dict):
        
        '''
        '''
        
        strIPCSocketPath:str = dictIPCPipelineServerLocalConfig.get("socket_path")
        
        ipcRequestRouter:IPCRequestRouter = IPCRequestRouter()
        ipcRequestRouter.Initialize(mainApp, dictIPCPipelineServerLocalConfig)
    
        try:
            
            if os.path.exists(strIPCSocketPath):
                os.unlink(strIPCSocketPath)
            
            serverSocket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            serverSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            serverSocket.setblocking(False)
            
            serverSocket.bind(strIPCSocketPath)
            serverSocket.listen(128)  # backlog 큐 크기
            
            # os.chmod(strIPCSocketPath, 0o660)
            
            LOG().info(f"ipc server listening on {strIPCSocketPath}")
            
            # 이시점에 초기화.
            self._epoll = select.epoll()
            
            # 서버 소켓을 epoll에 등록
            # self._epoll.register(serverSocket.fileno(), select.EPOLLIN | select.EPOLLET) # Edge-triggered
            self._epoll.register(serverSocket.fileno(), select.EPOLLIN) # Edge-triggered
            
            LOG().info(f"Epoll registered, server fd={serverSocket.fileno()}")
            
            # while self._running:
            while True:
                
                try:
                    events = self._epoll.poll(timeout=1.0)
                    
                    #TODO: debug 필요.
                    for fd, event in events:
                        
                        try:
                            
                            # 최초 IPC 연결
                            if fd == serverSocket.fileno():
                                self.__acceptConnection(serverSocket, strIPCSocketPath)
                                
                            elif event & select.EPOLLIN:
                                self.__readData(fd, ipcRequestRouter)
                                
                            elif event & select.EPOLLOUT:
                                self.__writeDataQueue(fd)
                                
                            elif event & (select.EPOLLERR | select.EPOLLHUP):
                                # logger.warning(f"Connection error on fd={fd}")
                                self.__closeConnection(fd)
                                
                        except Exception as e:                            
                            LOG().error(traceback.format_exc())
                            self.__closeConnection(fd)
                
                except KeyboardInterrupt:
                    break
                
                except Exception as e:
                    LOG().error(traceback.format_exc())
        
        finally:
            self.__cleanup(serverSocket, strIPCSocketPath)
            
            
    ######################################################################## private
    
    
    def __cleanup(self, serverSocket:socket.socket, strIPCSocketPath):
        
        '''
        '''
        
        with self.__lock:
            # 모든 클라이언트 연결 종료
            for fd in list(self._connections.keys()):
                self.__closeConnection(fd)
            
            # epoll 종료
            if self._epoll:
                self._epoll.close()
                # self._epoll = None #제일 마지막 부분, 무의미.
            
            # 서버 소켓 종료
            if serverSocket:
                serverSocket.close()
                # serverSocket = None #TODO: 이 부분 조심. 이게 호출되면, serversocket은 이미 종료된 시점.
            
            # 소켓 파일 삭제
            try:
                Path(strIPCSocketPath).unlink(missing_ok=True)
            except Exception as e:
                # logger.error(f"Failed to unlink socket: {e}")
                LOG().error(traceback.format_exc())
                
        # pass
    
    # 새연결, accept
    def __acceptConnection(self, serverSocket:socket.socket, strIPCSocketPath:str):
        '''
        '''
        
        try:
            while True:
                
                try:
                    client_sock, _ = serverSocket.accept()
                    client_sock.setblocking(False)
                    
                    conn = ClientConnection(client_sock)
                    
                    # fd = client_sock.fileno()
                    
                    with self.__lock:                        
                        self._connections[conn.fd] = conn
                    
                    # self._epoll.register(conn.fd, select.EPOLLIN | select.EPOLLET)
                    self._epoll.register(conn.fd, select.EPOLLIN)
                    
                    LOG().info(f"Client connected: fd={conn.fd}")
                
                #TODO: 괜찮은 구문인지 확인 필요.
                except BlockingIOError:
                    break  # 더 이상 accept 할 클라이언트 없음, TODO: 추가적인 예외처리.
        
        except Exception as e:
            LOG().error(traceback.format_exc())
            
        # pass
    
    # 데이터 수신    
    def __readData(self, fd: int, ipcRequestRouter:IPCRequestRouter):
        '''
        '''
        
        with self.__lock:            
            conn = self._connections.get(fd)
            
            if not conn:
                LOG().error(f"invalid fd, skip read data, fd = {fd}")
                return #TODO: 반환값.
        
        try:
            # sock = socket.socket(fileno=fd)
            
            bcloseConnection:bool = False
            
            while True:
                try:
                    chunk = conn.sock.recv(IPCPipelineServer.BUFFER_SIZE)
                    
                    # if not chunk:
                        
                    #     LOG().info(f"Client disconnected: fd={fd}")
                    #     self.__closeConnection(fd)
                    #     return
                    
                    # 소켓이 끊어짐. nc등으로 1회성 테스트를 할때.
                    if chunk == b'':
                        LOG().info(f"Client disconnected: fd={fd}")     
                        
                        #TODO: 데이터를 처리후에 끊어보자.    
                        bcloseConnection = True                                       
                        # self.__closeConnection(fd)                                                
                        break
                    
                    conn.recv_buffer.extend(chunk)
                    
                except BlockingIOError:
                    break  # 더 이상 읽을 데이터 없음
            
            # 수집된 데이터, json 포맷으로 수집한다.
            self.__processMessage(conn, ipcRequestRouter)
            
            if True == bcloseConnection:
                self.__closeConnection(fd)   
        
        except Exception as e:
            # logger.error(f"Read error on fd={fd}: {e}")
            LOG().error(traceback.format_exc())            
            self.__closeConnection(fd)
            
        # pass
    
    # 수신 버퍼에서 완전한 메시지 추출 및 처리
    def __processMessage(self, conn: ClientConnection, ipcRequestRouter:IPCRequestRouter):
        
        '''
        '''
        
        buf = conn.recv_buffer
        
        # while len(conn.recv_buffer) >= 4:
        while True:
            
            if len(buf) <= 0:
                return ERR_OK
            
            # TODO: 협의에 따라, ipc의 앞단에 사이즈를 던지고, 데이터를 받는 구조도 고려한다.
            # # 메시지 길이 읽기 (4 bytes, big-endian)
            # msg_len = struct.unpack('>I', buf[:HEADER_SIZE])[0]
            
            # # 메시지 길이 검증, 일단 제한을 두지 않는다.
            # # if msg_len > 10 * 1024 * 1024:  # 10MB 제한
            # #     LOG().error(f"Message too large: {msg_len} bytes")
            # #     self.__closeConnection(fd)
            # #     return
            
            # if msg_len <= 0:
            #     LOG.error(f"Invalid message length {msg_len} from fd={conn.fd}")
            #     self.__closeConnection(conn.fd)
            #     return
            
            # # 완전한 메시지가 도착했는지 확인
            # if len(buf) < HEADER_SIZE + msg_len:
            #     break  # 아직 불완전한 메시지
            
            # 메시지 추출 => TODO: 이건 어떤 기능인지? 테스트 하면서 확인
            # byteMessageData = buf[HEADER_SIZE:HEADER_SIZE + msg_len]
            # conn.recv_buffer = buf[HEADER_SIZE + msg_len:] #왜 필요한지?
            
            # JSON 파싱 및 처리
            try:
                
                # 데이터 처리, 여기 정의 필요. => 일단 echo 로 대응.
                '''
                # mainApp 와 helper를 통해서 처리한다. ipcRequestRouter
                request = json.loads(msg_data.decode('utf-8'))
                response = self.message_handler(fd, request)
                                
                if response:
                    self._send_message(fd, conn, response)
                '''
            
                #TODO: json의 종료 문자열, 일단 }\n으로 체크    
                # newline_pos = buf.find(b'\n')

                # if newline_pos == -1:
                #     # 메시지가 아직 완성되지 않음
                #     return
                
                # raw_msg = buf[:newline_pos]
                
                # del buf[:newline_pos + 1]  # 버퍼에서 제거
                
                
                #TODO: try 포함, 데이너 처리는 router에서 수행. 향후 수정.
                
                # 빈 데이터 제거
                # if not raw_msg.strip():
                #     LOG().error(f"invalid request, empty, skip")
                #     continue
                
                
                #TODO: json 문자열 생성까지는 여기서 보장하자.
                
                #json 변환, 매우 심플하게, 향후 좀더 개선
                strMessageData:str = buf.decode('utf-8')
                dictRequest = json.loads(strMessageData, strict=False)
                
                dictResponse = ipcRequestRouter.RouteRequest(dictRequest)
                
                self.__sendMessage(conn, dictResponse)
                
                #처리가 다 되었으면,buf 제거
                del buf[:]
                
            #TODO: 오류 상관없이, 잘못되었으면, 에러를 발생하고, 종료
            # 호출측에서 재전송이 되도록 구조화.
            
            except Exception as e:
                # logger.error(f"Message processing error: {e}")
                
                LOG().error(f"ipc process error, buf = {buf}")
                LOG().error(traceback.format_exc())
                
                #TODO: 잘못된 에러에 대한 고민, 이것도 ipcRequestRouter에서 처리.
                error_response = {"error": f"{e}"}
                self.__sendMessage(conn, error_response)
                
                #버퍼는 비운다.
                del buf[:]
                
                #오류가 발생했으면 종료하는 방향으로 고려
                return ERR_FAIL #TODO: 예외처리
            
        #pass  
        return ERR_OK              
                
            
    
    # 메시지 전송 큐에 추가
    def __sendMessage(self, conn: ClientConnection, dictResponse: dict):
        '''
        '''
        
        try:
            
            # payload = json.dumps(obj).encode()
            # header = struct.pack('>I', len(payload))
            # conn.send_buffer.extend(header + payload)
            # self.ep.modify(conn.fd, select.EPOLLIN | select.EPOLLOUT)

            #구분자, 개행으로.
            payload = json.dumps(dictResponse).encode('utf-8') + b'\n'
            # header = struct.pack('>I', len(payload))
            
            # conn.send_buffer.extend(header + payload)
            conn.send_buffer.extend(payload)
            
            # EPOLLOUT 이벤트 등록
            # self._epoll.modify(fd, select.EPOLLIN | select.EPOLLOUT | select.EPOLLET)
            self._epoll.modify(conn.fd, select.EPOLLIN | select.EPOLLOUT)
            
        except Exception as e:
            # logger.error(f"Failed to queue message: {e}")
            LOG().error(traceback.format_exc())
    
    # 전송 큐 처리
    # def _handle_write(self, fd: int):
    def __writeDataQueue(self, fd: int):
        '''
        claude 코드, 우선 테스트 하면서 좀더 개선.
        '''
        
        with self.__lock:
            conn = self._connections.get(fd)
            # if not conn or not conn.send_queue:
            if not conn:
                return
        
        try:
            # sock = socket.socket(fileno=fd)
            
            while conn.send_buffer:
                try:
                    sent = conn.sock.send(conn.send_buffer)
                    # conn.send_queue = conn.send_queue[sent:]
                    del conn.send_buffer[:sent]
                    
                except BlockingIOError:
                    break  # 소켓 버퍼가 가득 참
            
            # 전송 완료 시 EPOLLOUT 제거
            if not conn.send_buffer:
                # self._epoll.modify(fd, select.EPOLLIN | select.EPOLLET)
                self._epoll.modify(fd, select.EPOLLIN)
        
        except Exception as e:
            # logger.error(f"Write error on fd={fd}: {e}")
            LOG().error(traceback.format_exc())
            self.__closeConnection(fd)
    
    #연결 종료
    # def _close_connection(self, fd: int):
    def __closeConnection(self, fd: int):
        '''
        '''
        
        with self.__lock:
            # if fd not in self._connections:
            #     return
            conn = self._connections.pop(fd, None)
            
            if conn:
                LOG().info(f"[Server] Closing fd={fd}")
                
                try: self._epoll.unregister(fd)
                except: pass
                
                try: conn.sock.close()
                except: pass
            
            # try:
            #     self._epoll.unregister(fd)
            # except:
            #     pass
            
            # try:
            #     sock = socket.socket(fileno=fd)
            #     sock.close()
            # except:
            #     pass
            
            # del self._connections[fd]
            # LOG().info(f"Connection closed: fd={fd}")
    
    
    # # 모든 연결된 클라이언트에게 메시지 전송 => 제거, 예시에서는 fastapi로 테스트.
    # def broadcast(self, message: dict):
    #     '''
    #     TODO: claude에 의한 코드, 일단 기능은 유지 (사용 여부는 검증후, 불안한 기능)
    #     '''
        
    #     with self.__lock:
            
    #         for fd, conn in list(self._connections.items()):
    #             try:
    #                 self._send_message(fd, conn, message)
    #             except Exception as e:
    #                 # logger.error(f"Broadcast error to fd={fd}: {e}")
    #                 LOG().error(traceback.format_exc())
    
    
    # #서버 소켓 생성 => 제거, thread 본문에 추가.
    # def _create_server_socket(self, strIPCSocketPath:str) -> socket.socket:
        
    #     '''
    #     '''
        
    #     sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    #     sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    #     sock.setblocking(False)
        
    #     sock.bind(strIPCSocketPath)
    #     sock.listen(128)  # backlog 큐 크기
        
    #     LOG().info(f"Server listening on {self.socket_path}")
        
    #     return sock
    
    ########################################################################
    
    
    # def HandleClientData(self, conn, addr, selector):
        
    #     '''
    #     '''
        
    #     try:
            
    #         data = conn.recv(IPCPipelineServer.BUFFER_SIZE)
            
    #         if not data:
    #             return ERR_FAIL  # 연결 종료

    #         # JSON 파싱
    #         try:
                
    #             request = json.loads(data.decode('utf-8'))
    #             # print(f"Received JSON: {request}")
                
    #         except json.JSONDecodeError:
    #             print("Invalid JSON received")
    #             return

    #         # 비즈니스 로직: 받은 데이터 처리 (예: 'data' 필드를 대문자로 변환)
    #         # processed_data = request.get('data', '').upper()  # 실제 로직으로 대체 (e.g., DB 쿼리, FastAPI 연동)
    #         # response = {
    #         #     "status": "ok",
    #         #     "result": f"Processed: {processed_data}",
    #         #     "original_action": request.get('action', 'unknown')
    #         # }

    #         # # JSON 응답 전송
    #         # conn.sendall(json.dumps(response).encode('utf-8'))
    #         # print(f"Sent response: {response}")
            
    #     except socket.error as e:
    #         print(f"Socket error: {e}")
    #     finally:
    #         sel.unregister(conn)
    #         conn.close()
    #         print("Client disconnected")
        
    #     return ERR_OK    
    
    # GPT 코드, 처음 시작 시 참고, 선택이 불필요하면 제거
    # #ipc socket의 시작
    # def StartIPCSocket(self, ):
        
    #     '''        
    #     '''
        
    #     strIPCSocketPath:str = self.__dictIPCPipelineServerLocalConfig.get("socket_path")
        
    #     #TODO: 생성 오류가 발생하면, 에외처리후 exception을 발생한다.
    #     try:
            
        
    #         #TODO: 시작시점에 ipc 연결 파일을 끊는다. (강제로 끊는다.)
    #         # self.__unlinkIPCSocketPath(strIPCSocketPath)
            
    #         #TODO: 시작 시점의 오류는 바로 종료후 재기동을 유도한다.
    #         os.unlink(strIPCSocketPath)
            
    #         self.__socket:socket.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    #         self.__socket.setblocking(False)
    #         self.__socket.bind(strIPCSocketPath)
            
    #         self.__socket.listen(100)
    #         self.__selector.register(self.__socket, selectors.EVENT_READ, data=None)
            
    #         LOG().info("start ipc socket listening {strIPCSocketPath}")
            
    #     except Exception as ex:
            
    #         LOG().error(traceback.format_exc())
            
    #         raise Exception(f"fail unlink ipc socket {strIPCSocketPath}, error = {ex}")
        
    #     return ERR_OK
    
    # def Accept(self, socket:socket.socket):
        
    #     try:
    #         conn, addr = socket.accept()
    #         conn.setblocking(False)
            
    #         #connection 관리 => 여기 사양, 파악 필요.
    #         ipcConnectionInfo = IPCConnectionInfo(conn, addr)
            
    #         nFileNo:int = conn.fileno()
            
    #         #accept가 성공하면, map에 담는다.
    #         self.__dictConnectionMap[conn.fileno()] = ipcConnectionInfo
            
    #         self.__selector.register(conn, selectors.EVENT_READ | selectors.EVENT_WRITE, data=ipcConnectionInfo)
            
    #         print("Accepted connection fileno=", conn.fileno())
            
    #     except Exception as e:
    #         LOG().error(traceback.format_exc())
        
    #     return ERR_OK
    
    # def CloseConnection(self, ipcConnectionInfo:IPCConnectionInfo):
        
    #     try:
    #         fileno = ipcConnectionInfo.sock.fileno()
    #         LOG().info(f"Closing connection {fileno}, client_id= {ipcConnectionInfo.client_id}")
            
    #         try:
    #             self.__selector.unregister(ipcConnectionInfo.sock)
                
    #         except Exception:
    #             pass
            
    #         try:
    #             ipcConnectionInfo.sock.close()
    #         except Exception:
    #             pass
            
    #         with self.__lock:
                
    #             self.__ipcConnectionInfo.pop(fileno, None)
                
    #             if ipcConnectionInfo.client_id:
    #                 self.clients_by_id.pop(ipcConnectionInfo.client_id, None)
                    
    #     except Exception as e:
    #         LOG().error(traceback.format_exc())
        
    #     return ERR_OK
    
    # def RecvData(self, ipcConnectionInfo : IPCConnectionInfo):
        
    #     '''
    #     '''
        
    #     try:
            
    #         data = ipcConnectionInfo.sock.recv(4096)
            
    #         if None == data:
                
    #             self.CloseConnection(ipcConnectionInfo)
    #             return ERR_FAIL
            
    #         while b"\n" in ipcConnectionInfo.recv_buf:
                
    #             line, ipcConnectionInfo.recv_buf = ipcConnectionInfo.recv_buf.split(b"\n", 1)
                
    #             if not line:
    #                 continue
                
    #             obj = json.loads(line.decode('utf-8'))

    #             # first message from client expected to contain client_id
    #             if ipcConnectionInfo.client_id is None:
                    
    #                 client_id = obj.get("client_id")
                    
    #                 if client_id:
    #                     connection.client_id = client_id
                        
    #                     with self.lock:
    #                         self.clients_by_id[client_id] = connection
                            
    #                     # print("Registered client_id =", client_id, "fileno=", c.sock.fileno())
    #                     # optionally send ack
    #                     # self.queue_send(c, {"type":"registered"})
    #                     continue
    #                 else:
    #                     print("Client did not identify itself; closing.")
    #                     self.CloseConnection(connection)
    #                     return

    #             # handle response messages
    #             # If message contains request_id and matches pending future -> set result
    #             req_id = obj.get("request_id")
    #             is_stream = obj.get("stream", False)
    #             if req_id:
    #                 # stream message: push to stream subscribers and possibly set final future
    #                 if is_stream:
    #                     # push to stream queues if any
    #                     queues = []
    #                     with self.lock:
    #                         queues = list(self.stream_queues.get(req_id, []))
                            
    #                     for q in queues:
    #                         # put_nowait in asyncio queue via threadsafe call
    #                         asyncio_loop.call_soon_threadsafe(q.put_nowait, obj)
    #                     # If message indicates final chunk:
    #                     if obj.get("final", False):
    #                         # set single future as well if exists
    #                         fut = None
    #                         with self.lock:
    #                             fut = self.pending.pop(req_id, None)
    #                         if fut:
    #                             asyncio_loop.call_soon_threadsafe(fut.set_result, obj)
    #                 else:
    #                     # non-stream single response: set future
    #                     fut = None
    #                     with self.lock:
    #                         fut = self.pending.pop(req_id, None)
    #                     if fut:
    #                         asyncio_loop.call_soon_threadsafe(fut.set_result, obj)
    #                     else:
    #                         print("No pending future for request_id", req_id, "message:", obj)
    #             else:
    #                 # unsolicited message: could broadcast or log
    #                 print("Unsolicited message from", c.client_id, ":", obj)
            
    #     except Exception as e:
    #         LOG().error(traceback.format_exc())
            
    #         self.CloseConnection(ipcConnectionInfo)
            
    
    
    ########################################################### private
    
    # #socket의 연결 경로를 끊는다.
    # def __unlinkIPCSocketPath(self, strIPCSocketPath:str):
        
    #     '''
    #     TODO: 오류는 외부에서 잡는다.
    #     '''
        
    #     try:
    #         os.unlink(strIPCSocketPath)
            
    #     except OSError as ex:
    #         LOG().error(traceback.format_exc())
            
    #         #TODO: 발생확률이 적지만, 이경우에는 차라리 프로그램을 죽이고 다시 시작하는 쪽으로 선택한다.
    #         raise Exception(f"fail unlink ipc socket {strIPCSocketPath}, error = {ex}")
    #         # return ERR_FAIL
        
    #     return ERR_OK
    
    
    
    