
from pathlib import Path
import shutil
import threading

from datetime import datetime
import time

from lib_include import *

from type_hint import *

'''
첨부파일 백업, 보관 관리
'''

class AttachFileBackupHelper:
    
    def __init__(self):
        
        # self.__strFileBackupDir:str = None
        
        # 첨부파일, 백업 대상 경로
        self.__strAttachFileDestDir:str = ""
        
        pass
    
    def Initialize(self, dictFileBlockDBConfig:dict):
        
        '''
        '''
        
        strFileBackupDir:str = dictFileBlockDBConfig.get(FilePolicyDefine.LOCAL_CONFIG_FILE_BLOCK_BACKUP_DIR)
        
        #TODO: 현재 경로, 스레드 검토.
        
        thread = threading.Thread(name="attach file backup thread", target=self.ThreadHandlerProc, daemon=True, args=(strFileBackupDir,))
        thread.start()
        
        return ERR_OK
    
    # 첨부 파일 백업
    def BackupAttachFile(self, strRealOfficeFilePath:str, strFileName:str) -> str:
        
        '''
        백업, 탐지된 파일은, 압축없이 백업 경로로 바로 이동한다.
        백업시 파티션이 다르다.
        
        대상 파일명을 반환한다. opensearch에 저장 경로를 전달한다.
        '''
        
        #파일의 식별을 위해서, 파일명을 접두어로 추가한다.
        #파일명, 확장자를 떼어서, 이동 경로를 만든다.
        strSrcFileID:str = os.path.basename(strRealOfficeFilePath)
        
        strFileNameOnly:str = ""
        strFileExt:str = ""
        
        strDestFileFullPath:str = ""
        
        #이름, 확장자로 분리
        if 0 < len(strFileName):
            strFileNameOnly, strFileExt = os.path.splitext(strFileName)
            
            #전체 경로, backup/yyyymmdd/hh 형태로 기본 경로를 만든다. => 저장
            strDestFileFullPath = f"{self.__strAttachFileDestDir}/{strFileNameOnly}_{strSrcFileID}{strFileExt}"
            
        else:
            
            strDestFileFullPath = f"{self.__strAttachFileDestDir}/{strSrcFileID}"
        
        
        #TODO: 파일 이동이 실패하면, None을 반환한다.
        bMoveFile:bool = self.__moveFile(strRealOfficeFilePath, strDestFileFullPath)
        
        if False == bMoveFile:
            
            LOG().error(f"fail move attach file {strRealOfficeFilePath}")
            return ""
            
        return strDestFileFullPath
        
    
    # 파일명, 경로 생성
    def ThreadHandlerProc(self, strFileBackupDir:str):
        
        '''
        1분 스레드, 파일명을 만들어서, 생성된 파일로 
        '''
        
        LOG().info(f"start attach file backup thread, backup dir = {strFileBackupDir}")
        
        while True:
            
            #1분단위, 시간 변경
            #매분, 1개 더 생성
            
            now = time.time()
            
            strCurrentDatePath:str = time.strftime("%Y%m%d/%H", time.localtime(now))
            
            strNextDatePath:str = time.strftime("%Y%m%d/%H", time.localtime(now + 3600))
            
            # 매시간 단위, 현재시간, 다음시간 디렉토리 생성, 주기적 검사
            os.makedirs(f"{strFileBackupDir}/{strCurrentDatePath}", exist_ok=True)
            os.makedirs(f"{strFileBackupDir}/{strNextDatePath}", exist_ok=True)
            
            self.__strAttachFileDestDir = f"{strFileBackupDir}/{strCurrentDatePath}"
            
            #디렉토리가 존재하지 않으면, 자동생성
            
            # Path(self.__strAttachFileDestDir).mkdir(parents=True, exist_ok=True)
            
            # 10분 정도 단위 검사
            time.sleep(60*10)
            # pass
        
        # return ERR_OK
    
    ################################# private
    
    #파일의 이동, 다른 파티션을 감안, 테스트 후 모듈 공통화
    def __moveFile(self, strSrcFile:str, strDstFile:str) -> bool:
        
        '''
        예외발생에 대한 처리, 예외가 발생한 파일은 별도의 스케쥴러에서 후처리
        '''
        
        try:
            #TODO: 복사가 실패하면 예외가 발생한다. 예외가 발생하면, 우선 백업 미수행으로, 공백으로 로그에 저장한다.
            shutil.copyfile(strSrcFile, strDstFile)
            
            #사후 처리, 일단 고려하지 않는다. => 성능 이슈 => 그래도 체크, 0바이트에 대한 체크
            nSrcFileSize:int = os.path.getsize(strSrcFile)
            nDestFileSize:int = os.path.getsize(strDstFile)

            # if src_size != dst_size:
                
            #     LOG().error(f"size mismatch {strSrcFile}")
                
            #     logger.error(
            #         "size mismatch: %s(%d) -> %s(%d)",
            #         src,
            #         src_size,
            #         dst,
            #         dst_size
            #     )
            
            if nSrcFileSize != nDestFileSize:
                LOG().error(f"file copy error (mismatch), src file = {strSrcFile}, src size = {nSrcFileSize}, dest size = {nDestFileSize}")
                return False
            
            # TODO: 0바이트에 대한 대응, 둘의 사이즈 보다는 원본이 0 또는 대상이 0인지를 확인한다.
            if 0 == nSrcFileSize or 0 == nDestFileSize:
                LOG().error(f"file size error (zero size), src file = {strSrcFile}, src size = {nSrcFileSize}, dest size = {nDestFileSize}")
                #TODO: 감사로그
                return False
                
            # os.unlink(strSrcFile)
            #TODO: 디버그를 위한 원본 유지, 일단 기본으로는 이동, 문제 발생시 분석용으로 주석으로 하자.
            #백업 성공시에만 제거, 아닐경우에는 2차 스케쥴러에서 디스크 감시만 하고 이동한다. (이러면 UI에서 보이지 않는 문제는 있다.)
            os.remove(strSrcFile)
            return True

        except FileNotFoundError:
            LOG().error(f"file not found {strSrcFile}")
            return False

        except PermissionError:
            LOG().error(f"permission denied {strSrcFile}")
            return False

        except OSError as e:
            LOG().error(f"move fail {strSrcFile} -> {strDstFile}")
            return False
        
        # return ERR_OK
    