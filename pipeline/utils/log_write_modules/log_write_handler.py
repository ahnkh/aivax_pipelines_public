
import threading
import time

from lib_include import *

from utils.log_write_modules.byte_buffer_fast_writer import ByteBufferFastWriter

'''
'''

class LogWriteHandler:

    LAST_FLUSH_TIME = "last_flush_time"

    MAX_WAIT_TIME_OUT = 5
    THREAD_TIME_OUT = 1

    def __init__(self):

        self.__lock = threading.Lock()

        self.__dictBufferWriteQueue:dict = {}

        pass

    ################################################## public

    def AddData(self, strDataType:str, byteLogData:bytes):

        '''
        '''
        
        byteBufferFastWriter:ByteBufferFastWriter = self.__dictBufferWriteQueue.get(strDataType)

        # stringBufferWriter:StringBufferBulkWriterHelper = self.__dictBufferWriteQueue.get(strDataType)

        if None == byteBufferFastWriter:
            
            return ERR_FAIL
        
        byteBufferFastWriter.WriteLog(byteLogData)

        return ERR_OK

    def Initialize(self, dictLogWriteHandlerLocalConfig:dict):

        '''
        '''

        self.__initializeBufferWriter(self.__dictBufferWriteQueue, dictLogWriteHandlerLocalConfig)

        thread = threading.Thread(name="log write thread", target=self.ThreadHandlerProc, daemon=True)
        thread.start()

        return ERR_OK

    def ThreadHandlerProc(self, ):

        '''
        '''
        
        nMaxWaitTimeout:int = LogWriteHandler.MAX_WAIT_TIME_OUT

        while True:

            time.sleep(LogWriteHandler.THREAD_TIME_OUT)

            self.__lock.acquire()
            
            for strDataType in self.__dictBufferWriteQueue.keys():

                byteBufferFastWriter:ByteBufferFastWriter = self.__dictBufferWriteQueue.get(strDataType)

                if None != byteBufferFastWriter:

                    self.__flushBufferWriterAt(byteBufferFastWriter, nMaxWaitTimeout)
                    
            self.__lock.release()

    def __initializeBufferWriter(self, dictBufferWriteQueue:dict, dictLogWriteThreadLocalConfig:dict):

        '''
        '''

        string_buffer_config_list:list = dictLogWriteThreadLocalConfig.get("string_buffer_config_list")

        for dictBulkConfig in string_buffer_config_list:

            strQueueId:str = dictBulkConfig.get("queue_id")
            
            byteBufferFastWriter:ByteBufferFastWriter = ByteBufferFastWriter()
            byteBufferFastWriter.Initialize(dictBulkConfig)
                        
            byteBufferFastWriter.UpdateLastFlushTime(time.time())
            
            dictBufferWriteQueue[strQueueId] = byteBufferFastWriter

        return ERR_OK
    
    def __flushBufferWriterAt(self, byteBufferFastWriter:ByteBufferFastWriter, nMaxWaitTimeout:int):

        '''
        '''

        now:float = time.time()

        fLastFlushTime:float = byteBufferFastWriter.GetLastFlushTIme()

        if (now - fLastFlushTime) >= nMaxWaitTimeout:

            byteBufferFastWriter.FlushBuffer()
            byteBufferFastWriter.UpdateLastFlushTime(now)

        return ERR_OK

