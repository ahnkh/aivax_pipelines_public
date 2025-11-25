
import socket
import struct
import json
import threading
import time
import logging
from typing import Optional, Callable, Any, Dict
from queue import Queue, Empty
from dataclasses import dataclass
from enum import Enum

from lib_include import *

from type_hint import *

'''
ipc 통신, 테스트 클라이언트
c언어 sslproxy에 추가전, python 테스트 클라이언트 추가
'''

SOCKET_PATH = "/tmp/pipeline.sock"
BUFFER_SIZE = 4096

class ConnectionState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2

@dataclass
class PendingRequest:
    """대기 중인 요청"""
    request_id: str
    data: dict
    callback: Optional[Callable[[dict], None]]
    timestamp: float
    retry_count: int = 0
    max_retries: int = 3

class IPCTestClient:
    
    def __init__(self, socket_path=SOCKET_PATH, timeout=5.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self.sock = None
        self.recv_buffer = bytearray()
    
    def connect(self):
        """서버 연결"""
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(self.socket_path)
            print(f"[Connected] {self.socket_path}")
            return True
        
        except FileNotFoundError:
            print(f"[Error] Socket file not found: {self.socket_path}")
            return False
        
        except ConnectionRefusedError:
            print("[Error] Connection refused - is server running?")
            return False
        
        except Exception as e:
            print(f"[Error] Connection failed: {e}")
            return False
    
    def disconnect(self):
        """연결 종료"""
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None
            print("[Disconnected]")
    
    def send_request(self, action, data=None, request_id=None):
        """
        요청 전송 및 응답 수신
        
        Args:
            action: 액션 타입
            data: 추가 데이터 (dict)
            request_id: 요청 ID (없으면 자동 생성)
        
        Returns:
            응답 dict 또는 None (실패 시)
        """
        if not self.sock:
            print("[Error] Not connected")
            return None
        
        # 요청 메시지 구성
        if request_id is None:
            request_id = f"req_{int(time.time() * 1000000)}"
        
        message = {
            'request_id': request_id,
            'action': action,
            'timestamp': time.time()
        }
        
        if data:
            message.update(data)
        
        try:
            # JSON 직렬화
            json_data = json.dumps(message).encode('utf-8')
            
            # 메시지 길이 헤더 (4 bytes, big-endian)
            msg_len = struct.pack('>I', len(json_data))
            
            # 전송
            self.sock.sendall(msg_len + json_data)
            print(f"[Sent] {message}")
            
            # 응답 수신
            response = self._receive_response()
            
            if response:
                print(f"[Received] {response}")
            
            return response
        
        except BrokenPipeError:
            print("[Error] Connection broken")
            self.disconnect()
            return None
        except socket.timeout:
            print("[Error] Response timeout")
            return None
        except Exception as e:
            print(f"[Error] Send/Receive failed: {e}")
            return None
    
    def _receive_response(self):
        """응답 수신 (블로킹)"""
        # 최소 4바이트(길이 헤더) 수신
        while len(self.recv_buffer) < 4:
            chunk = self.sock.recv(BUFFER_SIZE)
            if not chunk:
                print("[Error] Server closed connection")
                return None
            self.recv_buffer.extend(chunk)
        
        # 메시지 길이 파싱
        msg_len = struct.unpack('>I', self.recv_buffer[:4])[0]
        
        # 전체 메시지 수신
        while len(self.recv_buffer) < 4 + msg_len:
            chunk = self.sock.recv(BUFFER_SIZE)
            if not chunk:
                print("[Error] Incomplete message")
                return None
            self.recv_buffer.extend(chunk)
        
        # 메시지 추출
        msg_data = self.recv_buffer[4:4 + msg_len]
        self.recv_buffer = self.recv_buffer[4 + msg_len:]
        
        # JSON 파싱
        try:
            response = json.loads(msg_data.decode('utf-8'))
            return response
        except json.JSONDecodeError as e:
            print(f"[Error] JSON decode failed: {e}")
            return None
    
    # def __init__(self,
    #     socket_path: str = SOCKET_PATH,
    #     auto_reconnect: bool = True,
    #     reconnect_interval: float = 2.0,
    #     request_timeout: float = 10.0):
        
    #     self.socket_path = socket_path
    #     self.auto_reconnect = auto_reconnect
    #     self.reconnect_interval = reconnect_interval
    #     self.request_timeout = request_timeout
        
    #     self._sock: Optional[socket.socket] = None
    #     self._state = ConnectionState.DISCONNECTED
    #     self._running = False
    #     self._lock = threading.Lock()
        
    #     # 스레드
    #     self._recv_thread: Optional[threading.Thread] = None
    #     self._reconnect_thread: Optional[threading.Thread] = None
        
    #     # 수신 버퍼
    #     self._recv_buffer = bytearray()
        
    #     # 전송 큐
    #     self._send_queue: Queue[PendingRequest] = Queue()
        
    #     # 응답 대기 중인 요청 (request_id -> PendingRequest)
    #     self._pending_requests: Dict[str, PendingRequest] = {}
        
    #     # 통계
    #     self._stats = {
    #         'sent': 0,
    #         'received': 0,
    #         'reconnects': 0,
    #         'errors': 0
    #     }
        
    #     pass
    
    # def start(self) -> bool:
    #     """클라이언트 시작"""
    #     if self._running:
    #         LOG().warning("Client already running")
    #         return False
        
    #     self._running = True
        
    #     # 초기 연결 시도
    #     if not self._connect():
    #         if not self.auto_reconnect:
    #             self._running = False
    #             return False
    #         LOG().info("Initial connection failed, will retry in background")
        
    #     # 수신 스레드 시작
    #     self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
    #     self._recv_thread.start()
        
    #     # 재연결 스레드 시작 (auto_reconnect 모드)
    #     if self.auto_reconnect:
    #         self._reconnect_thread = threading.Thread(
    #             target=self._reconnect_loop,
    #             daemon=True
    #         )
    #         self._reconnect_thread.start()
        
    #     LOG().info("IPC Client started")
    #     return True
    
    # def stop(self):
    #     """클라이언트 종료"""
    #     LOG().info("Stopping IPC client...")
    #     self._running = False
        
    #     if self._recv_thread:
    #         self._recv_thread.join(timeout=3.0)
        
    #     if self._reconnect_thread:
    #         self._reconnect_thread.join(timeout=3.0)
        
    #     self._disconnect()
    #     logger.info("IPC Client stopped")
    
    # def is_connected(self) -> bool:
    #     """연결 상태 확인"""
    #     with self._lock:
    #         return self._state == ConnectionState.CONNECTED
    
    # def send_request(
    #     self,
    #     action: str,
    #     data: Optional[dict] = None,
    #     callback: Optional[Callable[[dict], None]] = None,
    #     request_id: Optional[str] = None
    # ) -> str:
    #     """
    #     요청 전송
        
    #     Args:
    #         action: 액션 타입
    #         data: 추가 데이터
    #         callback: 응답 콜백 (None이면 동기 방식)
    #         request_id: 커스텀 요청 ID (None이면 자동 생성)
        
    #     Returns:
    #         request_id
    #     """
    #     if not request_id:
    #         request_id = f"req_{int(time.time() * 1000000)}"
        
    #     payload = {
    #         'request_id': request_id,
    #         'action': action,
    #         'timestamp': time.time()
    #     }
        
    #     if data:
    #         payload.update(data)
        
    #     req = PendingRequest(
    #         request_id=request_id,
    #         data=payload,
    #         callback=callback,
    #         timestamp=time.time()
    #     )
        
    #     # 전송 큐에 추가
    #     self._send_queue.put(req)
        
    #     # 콜백이 있으면 응답 대기 맵에 추가
    #     if callback:
    #         with self._lock:
    #             self._pending_requests[request_id] = req
        
    #     # 즉시 전송 시도
    #     self._try_send_queued()
        
    #     return request_id
    
    # def send_request_sync(
    #     self,
    #     action: str,
    #     data: Optional[dict] = None,
    #     timeout: float = None
    # ) -> Optional[dict]:
    #     """
    #     동기 요청 전송 (응답 대기)
        
    #     Returns:
    #         응답 딕셔너리 또는 None (타임아웃/실패)
    #     """
    #     if timeout is None:
    #         timeout = self.request_timeout
        
    #     response_event = threading.Event()
    #     response_data = {'result': None}
        
    #     def sync_callback(resp: dict):
    #         response_data['result'] = resp
    #         response_event.set()
        
    #     request_id = self.send_request(action, data, sync_callback)
        
    #     # 응답 대기
    #     if response_event.wait(timeout=timeout):
    #         return response_data['result']
        
    #     # 타임아웃
    #     LOG().warning(f"Request timeout: {request_id}")
    #     with self._lock:
    #         self._pending_requests.pop(request_id, None)
    #     return None
    
    # def get_stats(self) -> dict:
    #     """통계 정보 반환"""
    #     with self._lock:
    #         return self._stats.copy()
    
    # # ============ 내부 메서드 ============
    
    # def _connect(self) -> bool:
    #     """서버 연결"""
    #     with self._lock:
    #         if self._state == ConnectionState.CONNECTED:
    #             return True
            
    #         if self._state == ConnectionState.CONNECTING:
    #             return False
            
    #         self._state = ConnectionState.CONNECTING
        
    #     try:
    #         sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    #         sock.settimeout(5.0)
    #         sock.connect(self.socket_path)
    #         sock.setblocking(False)
            
    #         with self._lock:
    #             self._sock = sock
    #             self._state = ConnectionState.CONNECTED
    #             self._recv_buffer.clear()
            
    #         LOG().info(f"Connected to {self.socket_path}")
            
    #         # 큐에 있는 메시지 재전송
    #         self._try_send_queued()
            
    #         return True
        
    #     except FileNotFoundError:
    #         LOG().debug(f"Socket file not found: {self.socket_path}")
    #     except ConnectionRefusedError:
    #         LOG().debug("Connection refused")
    #     except socket.timeout:
    #         LOG().debug("Connection timeout")
    #     except Exception as e:
    #         LOG().error(f"Connection error: {e}")
    #         self._stats['errors'] += 1
        
    #     with self._lock:
    #         self._state = ConnectionState.DISCONNECTED
        
    #     return False
    
    # def _disconnect(self):
    #     """연결 종료"""
    #     with self._lock:
    #         if self._sock:
    #             try:
    #                 self._sock.close()
    #             except:
    #                 pass
    #             self._sock = None
    #         self._state = ConnectionState.DISCONNECTED
    
    # def _reconnect_loop(self):
    #     """재연결 루프"""
    #     while self._running:
    #         if self._state != ConnectionState.CONNECTED:
    #             LOG().info(f"Attempting reconnection...")
    #             if self._connect():
    #                 self._stats['reconnects'] += 1
    #                 LOG().info("Reconnected successfully")
    #             else:
    #                 time.sleep(self.reconnect_interval)
    #         else:
    #             time.sleep(1.0)
    
    # def _recv_loop(self):
    #     """수신 루프"""
    #     while self._running:
    #         if self._state != ConnectionState.CONNECTED:
    #             time.sleep(0.1)
    #             continue
            
    #         try:
    #             # select로 읽기 가능 대기
    #             import select
    #             readable, _, _ = select.select([self._sock], [], [], 1.0)
                
    #             if not readable:
    #                 continue
                
    #             # 데이터 수신
    #             data = self._sock.recv(BUFFER_SIZE)
                
    #             if not data:
    #                 LOG().warning("Server closed connection")
    #                 self._disconnect()
    #                 continue
                
    #             # 수신 버퍼에 추가
    #             self._recv_buffer.extend(data)
                
    #             # 메시지 파싱
    #             self._process_recv_buffer()
            
    #         except BlockingIOError:
    #             continue
    #         except (ConnectionResetError, BrokenPipeError):
    #             LOG().warning("Connection lost")
    #             self._disconnect()
    #         except Exception as e:
    #             LOG().error(f"Recv error: {e}")
    #             self._stats['errors'] += 1
    #             self._disconnect()
    
    # def _process_recv_buffer(self):
    #     """수신 버퍼에서 완전한 메시지 추출"""
    #     while len(self._recv_buffer) >= 4:
    #         # 메시지 길이 읽기
    #         msg_len = struct.unpack('>I', self._recv_buffer[:4])[0]
            
    #         # 길이 검증
    #         if msg_len > 10 * 1024 * 1024:  # 10MB
    #             LOG().error(f"Message too large: {msg_len} bytes")
    #             self._disconnect()
    #             return
            
    #         # 완전한 메시지 확인
    #         if len(self._recv_buffer) < 4 + msg_len:
    #             break
            
    #         # 메시지 추출
    #         msg_data = self._recv_buffer[4:4 + msg_len]
    #         self._recv_buffer = self._recv_buffer[4 + msg_len:]
            
    #         # JSON 파싱 및 처리
    #         try:
    #             response = json.loads(msg_data.decode('utf-8'))
    #             self._handle_response(response)
    #         except json.JSONDecodeError as e:
    #             LOG().error(f"JSON decode error: {e}")
    #             self._stats['errors'] += 1
    
    # def _handle_response(self, response: dict):
    #     """응답 처리"""
    #     self._stats['received'] += 1
        
    #     request_id = response.get('request_id')
        
    #     if not request_id:
    #         LOG().warning(f"Response without request_id: {response}")
    #         return
        
    #     # 대기 중인 요청 찾기
    #     with self._lock:
    #         pending = self._pending_requests.pop(request_id, None)
        
    #     if pending and pending.callback:
    #         try:
    #             pending.callback(response)
    #         except Exception as e:
    #             logger.error(f"Callback error: {e}")
    #             self._stats['errors'] += 1
    #     else:
    #         LOG().debug(f"No handler for response: {request_id}")
    
    # def _try_send_queued(self):
    #     """큐에 있는 메시지 전송 시도"""
    #     if self._state != ConnectionState.CONNECTED:
    #         return
        
    #     while not self._send_queue.empty():
    #         try:
    #             req = self._send_queue.get_nowait()
    #         except Empty:
    #             break
            
    #         if not self._send_message(req):
    #             # 전송 실패 - 재시도
    #             if req.retry_count < req.max_retries:
    #                 req.retry_count += 1
    #                 self._send_queue.put(req)
    #                 LOG().warning(
    #                     f"Send failed, will retry ({req.retry_count}/{req.max_retries})"
    #                 )
    #             else:
    #                 LOG().error(f"Message dropped after {req.max_retries} retries")
    #                 self._stats['errors'] += 1
    #             break
    
    # def _send_message(self, req: PendingRequest) -> bool:
    #     """메시지 전송"""
    #     if self._state != ConnectionState.CONNECTED or not self._sock:
    #         return False
        
    #     try:
    #         # JSON 직렬화
    #         json_data = json.dumps(req.data).encode('utf-8')
    #         msg_len = struct.pack('>I', len(json_data))
            
    #         # 전송
    #         self._sock.sendall(msg_len + json_data)
    #         self._stats['sent'] += 1
            
    #         LOG().debug(f"Sent request: {req.request_id}")
    #         return True
        
    #     except (BrokenPipeError, ConnectionResetError):
    #         LOG().warning("Connection lost during send")
    #         self._disconnect()
    #     except Exception as e:
    #         logger.error(f"Send error: {e}")
    #         LOG()._stats['errors'] += 1
        
    #     return False
    
    # def _cleanup_timeout_requests(self):
    #     """타임아웃된 요청 정리"""
    #     now = time.time()
    #     with self._lock:
    #         timeout_ids = [
    #             req_id for req_id, req in self._pending_requests.items()
    #             if now - req.timestamp > self.request_timeout
    #         ]
            
    #         for req_id in timeout_ids:
    #             LOG().warning(f"Request timeout: {req_id}")
    #             self._pending_requests.pop(req_id, None)