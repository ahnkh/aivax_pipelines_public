
import orjson

from lib_include import *

from type_hint import *

from utils.log_write_modules.log_write_handler import LogWriteHandler

from utils.log_write_modules.merge_fail_output_log_handler import MergeFailOutputLogHandler

from utils.event_alarm_modules.event_alarm_handler import EventAlarmHandler

'''
26.03.12 사양변경, 로그 병합기능, 중간 Queue 처리
TODO: 로그 clear, 성능 개선, timestamp기준, 1일이 넘는 input로그는 초기화
promptlogmap은 주기적으로 교체
'''

class LogMergeQueue:
    
    def __init__(self):
        
        #logwriter, 참조 추가
        self.__logWriteHandlerRef:LogWriteHandler = None
        
        self.__eventAlarmHandlerRef:EventAlarmHandler = None
        
        # 프롬프트 출력 로그를 보관할 dict, key로 검색할 hash
        self.__dictPromptLogMap:dict = None
        
        # 병합이 실패한 로그의 처리
        self.__mergeFailOutputLogHandler:MergeFailOutputLogHandler = None
        pass
    
    def Initialize(self, logWriteHandler:LogWriteHandler, eventAlarmHandler:EventAlarmHandler, dictLogMergeQueueLocalConfig:dict):
        
        '''
        '''
        
        self.__logWriteHandlerRef = logWriteHandler
        
        self.__eventAlarmHandlerRef = eventAlarmHandler
        
        self.__dictPromptLogMap = dict()
        
        self.__mergeFailOutputLogHandler:MergeFailOutputLogHandler = MergeFailOutputLogHandler()
        
        return ERR_OK
    
    def AddToLogQueue(self, nLogType:int, strLogMessageKey:str, dictLogData:dict):
        
        '''
        TODO: 병합기능이 필요하다. dict 타입의 원본의 전달 비용은 감수한다.
        로그 유형에 대해서는 향후 확장이 될수도 있다. 주의
        
        TODO: 기능 분기, Input이면 Queue에 저장, Output이면 로그 기록
        '''
        
        if LOG_INDEX_DEFINE.TYPE_LOG_INPUT == nLogType:
            self.__storeInputPromptToLogMap(strLogMessageKey, dictLogData)
            # pass
        
        elif LOG_INDEX_DEFINE.TYPE_LOG_OUTPUT == nLogType:
            self.__writeMergedLog(strLogMessageKey, dictLogData)
            # pass
        else:
            #있으면 안된다.
            LOG().error(f"invalid log type {nLogType}")
            return ERR_FAIL
        
        return ERR_OK
    
    #################################################### private
    
    # 프롬프트 로그, map에 보관한다.
    def __storeInputPromptToLogMap(self, strLogMessageKey:str, dictLogData:dict):
        
        '''
        '''
        
        #TOOD: 같은 키, 있으면 안되지만, 있을경우, FailQueue로 전달한다.
        
        if None == self.__dictPromptLogMap.get(strLogMessageKey):
        
            self.__dictPromptLogMap[strLogMessageKey] = dictLogData
            
        else:
            #TODO: FailQueue로 원본 전달    
            pass
        
        return ERR_OK
    
    # Output로그, 프롬프트를 찾아서, 병합후 저장한다.
    def __writeMergedLog(self, strLogMessageKey:str, dictLogData:dict):
        
        '''
        TODO: 동시성 처리, 동기화 조심, 스레드는 아니지만, 구조상 동시성 문제가 생길수 있다.    
        '''
        
        dictPromptLog:dict = None
        
        if 0 != len(strLogMessageKey) and strLogMessageKey in self.__dictPromptLogMap:
        
            # dictPromptLog:dict = self.__dictPromptLog.get(strLogMessageKey)
            dictPromptLog = self.__dictPromptLogMap.pop(strLogMessageKey, None)
        
        #TODO: prompt로그의 교체 기능, 향후 반영
        
        #동일ID의 데이터가 존재하면, 병합후 로그를 생성, 저장한다.
        
        if None != dictPromptLog:
            
            # 성능문제 조심
            dictPromptLog.update(dictLogData)
            
            # byteLogData:bytes = orjson.dumps(dictPromptLog)
            # byteLogData += b'\n'
            # self.__logWriteHandlerRef.AddData(LOG_INDEX_DEFINE.KEY_AIVAX_LOG, byteLogData)
            
        else:
            
            # 일단 로깅
            LOG().error(f"fail merge prompt log, call merge fail handler")
            dictPromptLog = self.__mergeFailOutputLogHandler.HandleMergeFailLog(strLogMessageKey, dictLogData, self.__dictPromptLogMap)
            
            #반드시 만든다.
            #성능문제 조심
            dictPromptLog.update(dictLogData)
            # pass
            
        #TODO: 이시점에, Alarm로그로 전달되어야 한다. notify 계열로 전달
        #alarm로그, 다시 만들어서 스레드로 전달. => python 문제 조심, N분 delay가 되어야 한다.
        self.__sendEventAlarmMessage(dictPromptLog)
        
        byteLogData:bytes = orjson.dumps(dictPromptLog)
        byteLogData += b'\n'
        self.__logWriteHandlerRef.AddData(LOG_INDEX_DEFINE.KEY_AIVAX_LOG, byteLogData)
        
        return ERR_OK
    
    def __sendEventAlarmMessage(self, dictPromptLog:dict):
        
        '''
        '''
        
        user:dict = dictPromptLog.get("user", {})
        
        eventAlarmMessage:EventAlarmMessage = EventAlarmMessage(
            time_stamp = dictPromptLog.get("@timestamp"),
            messageid = dictPromptLog.get("message_id"),
            user_id = user.get("id"),
            user_role = user.get("role"),
            email = user.get("email"),
            uuid = user.get("uuid"),
            
            ai_service = dictPromptLog.get("ai_service"),
            prompt = dictPromptLog.get("prompt"),
            mode = dictPromptLog.get("mode"),
            output_text = dictPromptLog.get("output_text"),
        )
        
        self.__eventAlarmHandlerRef.AddEventMessage(eventAlarmMessage)
        
        return ERR_OK