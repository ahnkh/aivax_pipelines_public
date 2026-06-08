
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
        #yyyymmdd/hh => 1분 스레드, 꼭 바로 맞지 않아도 된다.
        
        thread = threading.Thread(name="ipc socket thread", target=self.ThreadHandlerProc, daemon=True, args=(strFileBackupDir,))
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
        
    
    # 파일명 생성
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
            
            os.makedirs(f"{strFileBackupDir}/{strCurrentDatePath}", exist_ok=True)
            os.makedirs(f"{strFileBackupDir}/{strNextDatePath}", exist_ok=True)
            
            self.__strAttachFileDestDir = f"{strFileBackupDir}/{strCurrentDatePath}"
            
            #디렉토리가 존재하지 않으면, 자동생성
            
            # Path(self.__strAttachFileDestDir).mkdir(parents=True, exist_ok=True)
            
            time.sleep(60)
            # pass
            
        
        # return ERR_OK
    
    ################################# private
    
    #파일의 이동, 다른 파티션을 감안, 테스트 후 모듈 공통화
    def __moveFile(self, strSrcFile:str, strDstFile:str) -> bool:
        
        '''
        예외발생에 대한 처리, 예외가 발생한 파일은 별도의 스케쥴러에서 후처리
        '''
        
        try:
            shutil.copyfile(strSrcFile, strDstFile)
            
            #사후 처리, 일단 고려하지 않는다.
            # src_size = os.path.getsize(strSrcFile)
            # dst_size = os.path.getsize(strDstFile)

            # if src_size != dst_size:
                
            #     LOG().error(f"size mismatch {strSrcFile}")
                
            #     logger.error(
            #         "size mismatch: %s(%d) -> %s(%d)",
            #         src,
            #         src_size,
            #         dst,
            #         dst_size
            #     )
                
            # os.unlink(strSrcFile)
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
    