
import requests
import threading
from collections import deque
# from typing import List, Any

# from apscheduler.schedulers.background import BackgroundScheduler

from lib_include import *

from type_hint import *

'''
event에 대한 alarm 처리
1차 pipeline filter 이벤트 알람
'''

class EventAlarmHandler:
    
    def __init__(self):
        
        # self.__buffer:deque = None
        self.__buffer:deque = deque()
        
        self.__lock = threading.Lock()
        pass
    
    def Initialize(self, dictEventAlarmHandlerLocalConfig:dict):
        
        '''
        '''
        
        self.__buffer:deque = deque()
        
        thread = threading.Thread(name="event alarm thread", target=self.ThreadHandlerProc, daemon=True, args=(dictEventAlarmHandlerLocalConfig,))
        thread.start()
        
        return ERR_OK
    
    
    def AddEventMessage(self, eventAlarmMessage:EventAlarmMessage):
        
        #TODO: 일단 추가후, MaxQueue 처리는 스레드 안에서.. 주기를 짧게 가져가야 할 필요가 있다.
        #deque은 lock을 사용하지 않는다.
        # with self.__lock:
            # self.__buffer.append(eventAlarmMessage)
        self.__buffer.append(eventAlarmMessage)
            
        return ERR_OK
    
    def ThreadHandlerProc(self, dictEventAlarmHandlerLocalConfig:dict):
        
        '''
        '''
        
        event_forward_cycle:int = dictEventAlarmHandlerLocalConfig.get("event_forward_cycle")
        
        pipe_filter_event:dict = dictEventAlarmHandlerLocalConfig.get("pipe_filter_event")
        
        # event 최대 개수, 제한값
        event_max_limit:int = pipe_filter_event.get("event_max_limit")
        
        #전송 옵션, 우선 미 고려
        alarm_level_mask:int = pipe_filter_event.get("alarm_level_mask")
        
        #전송 url
        ui_alarm_event:dict = dictEventAlarmHandlerLocalConfig.get("ui_alarm_event")
        url:str = ui_alarm_event.get("url")
        timeout:str = ui_alarm_event.get("timeout")
        
        # fNextRun:float = time.time()
        
        while True:
                        
            try:
                
                # now = time.time()
                start = time.monotonic()
                                
                with self.__lock:
                    
                    if len(self.__buffer) > 0:
                        self.__sendAlarmEvent(event_max_limit, url, timeout)
                    
                    elapsed = time.monotonic() - start
                    sleepTime = event_forward_cycle - elapsed
                    
                    if sleepTime > 0:
                        time.sleep(sleepTime)
                    
                    # if now >= fNextRun:
                        
                    #     fNextRun += event_forward_cycle
                        
                    #     if now > fNextRun:
                    #         fNextRun = now + event_forward_cycle
                    
                    # fSleepTime:float = fNextRun - time.time()
                    
                    # if fSleepTime > 0:                
                    #     #TODO: 전송이 밀리면, 같이 밀린다.
                    #     # time.sleep(event_forward_cycle) 
                    #     time.sleep(fSleepTime) 
                
            except Exception as err:         
                LOG().error(traceback.format_exc())
                
                time.sleep(event_forward_cycle)
            
            #pass
        
        # return ERR_OK
    
    
    ################################# private
    
    def __sendAlarmEvent(self, nEventMaxLimit:int, strBackendURL:str, nTimeout:int):
        '''
        '''
        
        #buffer swap
        old_buffer = self.__buffer
        self.__buffer = deque()
        
        lstEvent:List[EventAlarmMessage] = list(old_buffer)
        
        nSendCount:int = 0
        
        # for i in range(0, len(lstEvent), nEventMaxLimit):
        #     chunk = events[i:i + CHUNK_SIZE]
        
        lstHttpEvent:list = []

        for eventAlarmMessage in lstEvent:
            
            nSendCount += 1
            
            #전송 못한 나머지는 버린다.
            if nSendCount > nEventMaxLimit:
                nQueueCount = len(lstEvent)
                LOG().info(f"send buffer overflow, maxlimit = {nEventMaxLimit}, count = {nQueueCount}")
                break
            
            #여기는 어쩔수 없다. 이벤트를 만들어서 버린다.
            lstHttpEvent.append({
                "timestamp" : eventAlarmMessage.time_stamp,
                "messageId" : eventAlarmMessage.messageid,
                "user": {
                    "id" : eventAlarmMessage.user_id,
                    "role" : eventAlarmMessage.user_role,
                    "email" : eventAlarmMessage.email,
                    "uuid" : eventAlarmMessage.uuid,
                },
                
                "aiService" : eventAlarmMessage.ai_service,
                "prompt" : eventAlarmMessage.prompt,
                "mode" : eventAlarmMessage.mode,
                "outputText" : eventAlarmMessage.output_text
            })
            
            # pass
            
        # strURL:str = ""
        
        #TODO: 재시도, 일단 고려하지 않는다.
        #응답값 처리, 개발하면서 고려
        response = requests.post(
            strBackendURL,
            json=lstHttpEvent,  #자동으로 Content-Type: application/json 붙음       
            timeout=nTimeout             
        )
        
        return ERR_OK
    
    # def __readLocalConfig(self, dictEventAlarmHandlerLocalConfig:dict):
        
    #     '''
    #     '''
        
    #     # event 전송 주가
    #     event_forward_cycle:int = dictEventAlarmHandlerLocalConfig.get("event_forward_cycle")
        
    #     pipe_filter_event:dict = dictEventAlarmHandlerLocalConfig.get("pipe_filter_event")
        
    #     # event 최대 개수, 제한값
    #     event_max_limit:int = pipe_filter_event.get("event_max_limit")
    #     alarm_level_mask:int = pipe_filter_event.get("alarm_level_mask")
        
    #     return ERR_OK
    
    