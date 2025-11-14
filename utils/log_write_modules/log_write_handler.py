
import threading
import time

from lib_include import *

from utils.log_write_modules.byte_buffer_fast_writer import ByteBufferFastWriter

'''
외부 스레드와 Buffer를 제공, 로그를 전달받으면, 파일을 생성하여 fluentbit로 전달한다.

'''

class LogWriteHandler:

    LAST_FLUSH_TIME = "last_flush_time"

    MAX_WAIT_TIME_OUT = 5
    THREAD_TIME_OUT = 1

    def __init__(self):

        self.__lock = threading.Lock()
        # self.__condition = threading.Condition(self.__lock)

        #string buffer write queue
        self.__dictBufferWriteQueue = {}

        # self.__thread = None
        pass

    ################################################## public

    # 로그 데이터 추가
    # def AddData(self, strDataType:str, strDataLog:str):byteLogData:bytes
    def AddData(self, strDataType:str, byteLogData:bytes):

        '''
        StringBuffer에 추가한다. 이후 저장 조건, Flush 처리는
        StringBuffer에서 담당한다.        
        '''
        
        byteBufferFastWriter:ByteBufferFastWriter = self.__dictBufferWriteQueue.get(strDataType)

        # stringBufferWriter:StringBufferBulkWriterHelper = self.__dictBufferWriteQueue.get(strDataType)

        if None == byteBufferFastWriter:
            
            #TODO: LOG 관리 모듈 이식.
            # LOG().error(f"invalid data type {strDataType}")
            return ERR_FAIL
        
        byteBufferFastWriter.WriteLog(byteLogData)

        return ERR_OK

    #초기화
    def Initialize(self, dictLogWriteHandlerLocalConfig:dict):

        '''
        초기화 시점에 필요 dictionary 전달, 우선은 소스코드.
        '''

        #TODO: 초기화 시점, 예외 발생사 Raize 처리.

        #strinbfuffer, 미리 추가.
        #stringbuffer writer, 이름을 지어서, 미리 추가, Initialize

        self.__initializeBufferWriter(self.__dictBufferWriteQueue, dictLogWriteHandlerLocalConfig)

        #스레드 호출, 가급적 인스턴스를 전역으로 관리
        thread = threading.Thread(name="log write thread", target=self.ThreadHandlerProc, daemon=True)
        thread.start()

        return ERR_OK

    # 스레드 생성.
    def ThreadHandlerProc(self, ):

        '''
        데몬이 종료될때까지는 계속 수행된다. 
        TODO: 스레드가 종료되었으면, 재기동등 예외처리 로직을 추가한다.
        TODO: try/catch 예외처리 필수.
        '''
        
        nMaxWaitTimeout:int = LogWriteHandler.MAX_WAIT_TIME_OUT
        # nThreadSleep:int = 1

        while True:

            time.sleep(LogWriteHandler.THREAD_TIME_OUT) #시작후 대기한다. (바로 저장이 되지는 않을 것으로 예상)

            #각 Buffer별로 다르게 계산한다.
            self.__lock.acquire()
            
            for strDataType in self.__dictBufferWriteQueue.keys():

                # stringBufferBulkWriter:StringBufferBulkWriterHelper = self.__dictBufferWriteQueue.get(strKey)
                byteBufferFastWriter:ByteBufferFastWriter = self.__dictBufferWriteQueue.get(strDataType)

                #TODO: size 체크는 불필요. 

                if None != byteBufferFastWriter:

                    #각 buffer별 flush 체크.                    
                    self.__flushBufferWriterAt(byteBufferFastWriter, nMaxWaitTimeout)
                    
            self.__lock.release()

        #TOOD: 호출될 수 없는 구문.
        return ERR_OK

    ################################################## protected


    ################################################## private

    #buffer writer의 초기화
    def __initializeBufferWriter(self, dictBufferWriteQueue:dict, dictLogWriteThreadLocalConfig:dict):

        '''
        '''

        string_buffer_config_list:list = dictLogWriteThreadLocalConfig.get("string_buffer_config_list")

        for dictBulkConfig in string_buffer_config_list:

            strQueueId:str = dictBulkConfig.get("queue_id")
            
            # LOG().debug(f"initialize buffer writer, queue id {strQueueId}")

            byteBufferFastWriter:ByteBufferFastWriter = ByteBufferFastWriter()
            byteBufferFastWriter.Initialize(dictBulkConfig)
            
            #TODO: 사양 재검토
            # #마지막 수집시간 설정.
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

