
import orjson

from lib_include import *

from type_hint import *

from utils.log_write_modules.log_write_handler import LogWriteHandler

from utils.log_write_modules.merge_fail_output_log_handler import MergeFailOutputLogHandler

from utils.event_alarm_modules.event_alarm_handler import EventAlarmHandler

'''
'''

class LogMergeQueue:
    
    def __init__(self):
        
        self.__logWriteHandlerRef:LogWriteHandler = None
        
        self.__eventAlarmHandlerRef:EventAlarmHandler = None
        
        self.__dictPromptLogMap:dict = None
        
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
        
        
        if LOG_INDEX_DEFINE.TYPE_LOG_INPUT == nLogType:
            self.__storeInputPromptToLogMap(strLogMessageKey, dictLogData)
        
        elif LOG_INDEX_DEFINE.TYPE_LOG_OUTPUT == nLogType:
            self.__writeMergedLog(strLogMessageKey, dictLogData)

        else:
            LOG().error(f"invalid log type {nLogType}")
            return ERR_FAIL
        
        return ERR_OK
    
    def __storeInputPromptToLogMap(self, strLogMessageKey:str, dictLogData:dict):
        
        '''
        '''
                
        if None == self.__dictPromptLogMap.get(strLogMessageKey):
        
            self.__dictPromptLogMap[strLogMessageKey] = dictLogData
                    
        return ERR_OK
    
    def __writeMergedLog(self, strLogMessageKey:str, dictLogData:dict):
        
        
        dictPromptLog:dict = None
        
        if 0 != len(strLogMessageKey) and strLogMessageKey in self.__dictPromptLogMap:
        
            dictPromptLog = self.__dictPromptLogMap.pop(strLogMessageKey, None)
        
        
        if None != dictPromptLog:
            
            dictPromptLog.update(dictLogData)
            
        else:
            
            LOG().error(f"fail merge prompt log, call merge fail handler")
            dictPromptLog = self.__mergeFailOutputLogHandler.HandleMergeFailLog(strLogMessageKey, dictLogData, self.__dictPromptLogMap)
                        
            if None == dictPromptLog:
                LOG().error(f"fail update prompt, messageid = {strLogMessageKey}")
                return ERR_FAIL
            
            dictPromptLog.update(dictLogData)
            
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
        
        #TODO: 예외처리, prompt, mode값이 None이면 전송하지 않고 버린다.
        if None == eventAlarmMessage.prompt or None == eventAlarmMessage.mode:
            LOG().error(f"invalid alarm message, skip add message")
            return ERR_FAIL
        
        self.__eventAlarmHandlerRef.AddEventMessage(eventAlarmMessage)
        
        return ERR_OK