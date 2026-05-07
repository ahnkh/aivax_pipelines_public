
import os
import signal
import psutil
import copy
    
from lib_include import *

from type_hint import *

'''
정책 수신,signal 이후의 처리, sslproxy로의 데이터 전달에 관련한 signal을 생성한다.
1차 개발 - settings, ai_service 2개의 테이블을 조회하여 전달한다.
'''

class SSLProxyPolicySignalHandler:
    
    EXPORT_SUMMARY = "summary"
    EXPORT_AI_SERVICE = "ai_service"
    EXPORT_REDIRECT_URL = "redirect_url"
    
    EXPORT_BLOCK = "block_redirect"
    EXPORT_PASS = "pass"
    
    STATUS_BLOCK = 0 #미사용이 차단, 사용이 허용이다.
    STATUS_PASS = 1
    
    
    def __init__(self):
        
        self.__localSignalPolicy:dict = None
        
        # 최근 정책, 다음 패턴으로 분류한다.
        # 신규 정책과 비교하여, 달라질때만 전달하는 구조
        # self.__dictLastPolicy:dict = {
            
        '''
        {
            "gpt": "block_redirect",
            "gemini": "pass",
            "claude": "pass",
            "redirect_url" : ""
        }
        '''
            
        #     # 수집 데이터 원본 => 이건 유지
        #     # SSLProxyPolicySignalHandler.EXPORT_AI_SERVICE: [],
        #     # NAME, 소문자를 키로, status값을 치환하여 저장
            
        #     #구분을 위한 필드, 유지
        #     # SSLProxyPolicySignalHandler.EXPORT_SUMMARY : "",                        
        #     # "desc" : "unknown,gpt,gemini,claude,grok,perplexity"
        # }
        
        #과거 정책, 저장하지는 않고 별도로 관리한다.
        self.__strLastPolicySummary:str = ""
        
        # pass
    
    # 모듈 초기화
    def Initialize(self, dictJsonLocalConfigRoot:dict):
        
        '''
        최초 설정 값.
        '''
        
        # 우선 개발하고, 향후 모듈 분리
        sslproxy_policy_signal_handler:dict = dictJsonLocalConfigRoot.get("sslproxy_policy_signal_handler")
        
        # export_dir:str = sslproxy_policy_signal_handler.get("export_dir")
        # export_file:str = sslproxy_policy_signal_handler.get("export_file")
        
        self.__localSignalPolicy:dict = sslproxy_policy_signal_handler
        
        return ERR_OK
    
    # UI에서 signal을 받은후의 처리
    def NotifyPolicySignal(self, ):
        
        '''
        정책을 가져온다. 원본 그대로 수집, 저장
        다음 형태의 데이터를 만든다.
        "summary": "1,1,1,1,1"
        '''        
        # LOG().info("notify policy signal")
        
        strRedirectUrl:str = self.__localSignalPolicy.get("redirect_url")
        
        # signal을 받으면, 새로운 최근 데이터를 가져온다.
        dictNewPolicy:dict = {
            
            #원본 그대로 추가 => 제거
            # SSLProxyPolicySignalHandler.EXPORT_AI_SERVICE : [],
            
            # SSLProxyPolicySignalHandler.EXPORT_SUMMARY : "",                        
            SSLProxyPolicySignalHandler.EXPORT_REDIRECT_URL : strRedirectUrl #기본값
        }

        #DB 조회, 값 업데이트 
        lstNewStatus:list = []       
        self.__gatherDBPolicy(dictNewPolicy, lstNewStatus)
        
        strNewStatus:str = ",".join(map(str, lstNewStatus))
        
        # 과거 값과 비교 self.__strLastPolicySummary
        # strLastStatus:str = self.__dictLastPolicy.get(SSLProxyPolicySignalHandler.EXPORT_SUMMARY)
        strLastStatus:str = self.__strLastPolicySummary
        
        # strNewStatus:str = dictNewPolicy.get(SSLProxyPolicySignalHandler.EXPORT_SUMMARY)
        
        #값이 없으면, 무시한다.
        if None != strNewStatus and strNewStatus != strLastStatus:
            
            LOG().info(f"policy changed, signal to ssl policy, status = {strNewStatus}")
            
            self.__doSignalToSSLProxy(self.__localSignalPolicy, dictNewPolicy)
            # pass
            
        # 완료되면, 과거 정책 업데이트
        # self.__dictLastPolicy = copy.deepcopy(dictNewPolicy)
        self.__strLastPolicySummary = strNewStatus
        
        return ERR_OK
    
    ############################################# private
    
    def __gatherDBPolicy(self, dictNewPolicy:dict, lstNewStatus:list):
        
        '''
        '''
        
        # 우선 개발, 향후 리펙토링
        '''
        select created_at, id, name, status, type from app.ai_services where type = 1 order by id
        '''
        dictDBResult = {}
        sqlprintf(DBSQLDefine.BASE_CATEGORY_RDB, "rdb_select_ai_service", {}, dictDBResult)
            
        lstAIServiceInfo:list = dictDBResult.get(DBSQLDefine.QUERY_DATA)
        
        # python의 join을 사용한다.
        # lstStatus:list = []
        # lstDesc:list = []
        
        for dictAIServiceInfo in lstAIServiceInfo:
            
            # id:int = dictAIServiceInfo.get("id")
            name:int = str(dictAIServiceInfo.get("name", "")).lower()
            status:int = int(dictAIServiceInfo.get("status", SSLProxyPolicySignalHandler.STATUS_PASS))
            
            # 이름이 공백이어서는 안된다.
            if 0 == len(name):
                LOG().error(f"invalid value, name is empty, skip")
                continue
            
            strExportStatus:str = SSLProxyPolicySignalHandler.EXPORT_PASS
            
            if SSLProxyPolicySignalHandler.STATUS_BLOCK == status:
                strExportStatus = SSLProxyPolicySignalHandler.EXPORT_BLOCK
            
            dictNewPolicy[name] = strExportStatus
            
            lstNewStatus.append(status)
            # lstDesc.append(name)
            # pass
            
        # strStatus:str = ",".join(map(str, lstNewStatus))
        # strDesc:str = ",".join(map(str, lstDesc))
        
        #수집된 원본
        # dictNewPolicy[SSLProxyPolicySignalHandler.EXPORT_AI_SERVICE] = lstAIServiceInfo
        
        # dictNewPolicy[SSLProxyPolicySignalHandler.EXPORT_SUMMARY] = strStatus
        # dictNewPolicy["desc"] = strDesc
      
        return ERR_OK
    
    #ssl proxy로 signal을 전달한다.
    def __doSignalToSSLProxy(self, dictLocalSignalPolicy:dict, dictNewPolicy:dict):
        
        '''
        '''
        
        self.__exportPolicyFile(dictLocalSignalPolicy, dictNewPolicy)
        
        self.__signalToSSLProxy(dictLocalSignalPolicy)
        
        return ERR_OK
    
    #신규 설정을 파일로 내보낸다.
    def __exportPolicyFile(self, dictLocalSignalPolicy:dict, dictNewPolicy:dict):
        
        '''
        '''        
        LOG().info("export to policy file")
        
        # export_dir:str = sslproxy_policy_signal_handler.get("export_dir")
        # export_file:str = sslproxy_policy_signal_handler.get("export_file")
        
        export_dir:str = dictLocalSignalPolicy.get("export_dir")        
        os.makedirs(export_dir, exist_ok=True)
        
        export_file:str = dictLocalSignalPolicy.get("export_file")
        
        strExportFullPath:str = f"{export_dir}/{export_file}"
        
        # 파일로 저장, 향후 예외처리.
        JsonHelper.WriteMapToJsonFile(dictNewPolicy, strExportFullPath)
        
        return ERR_OK
    
    #sslproxy로 signal을 전달한다.
    def __signalToSSLProxy(self, dictLocalSignalPolicy:dict):
        
        '''
        sslproxy의 pid를 찾아서, signal을 전달한다.
        '''
        
        sslproxy_process:str = dictLocalSignalPolicy.get("sslproxy_process")
        
        lstSSLProxyPid = []
        self.__findSSLProxyPid(sslproxy_process, lstSSLProxyPid)
        
        LOG().info(f"signal to sslproxy, pid list {lstSSLProxyPid}")
        
        #try구문, 한번더 감싸자. 죽을수도 있다. 하나라도 실패하면 예외, 중단
        try:
            for nPid in lstSSLProxyPid:
            
                LOG().info(f"kill signal to sslproxy [{nPid}]")        
                os.kill(nPid, signal.SIGUSR2) #12번 => OS마다 다를수 있다.
            
        except ProcessLookupError:
            LOG().error("process lookup error")
            
        except PermissionError:
            LOG().error("permissionerror")
            
        except OSError as e:
            LOG().error("os error")
        
        return ERR_OK
    
    #sslproxy의 pid를 찾는다.
    def __findSSLProxyPid(self, strProxyProcessName: str, lstSSLProxyPid:list):
        
        '''
        디버깅을 위해서 기능 분리
        '''
        
        for proc in psutil.process_iter(['pid', 'exe']):

            try:
                exe = proc.info.get('exe')
                
                # LOG().info(f"exe = {exe}")

                if exe and exe.endswith("/sslproxy"):
                    
                    # LOG().info(f"append pid {proc.pid}")
                    lstSSLProxyPid.append(proc.pid)

            except Exception:
                pass
        
        
        # for proc in psutil.process_iter(['pid', 'name', 'cmdline']):

        #     try:
        #         cmdline = proc.info.get('cmdline')

        #         if not cmdline:
        #             continue

        #         cmd = " ".join(cmdline)

        #         if strProxyProcessName in cmd:
                    
        #             LOG().info(f"find ssl proxy, pid={proc.pid}, cmd={cmd}")
                    
        #             lstSSLProxyPid.append(proc.pid)

        #     except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        #         continue

        #     except Exception as e:
        #         LOG().error(f"process scan error: {e}")
        #         continue
        
        # for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        #     try:
        #         if strProxyProcessName in " ".join(proc.info['cmdline']):
        #             # print(f"PID {proc.pid} → SIGUSR2 전송")
        #             # os.kill(proc.pid, signal.SIGUSR2)
        #             # found = True
        #             lstSSLProxyPid.append(proc.pid)

        #     # 예외처리
        #     except (psutil.NoSuchProcess, psutil.AccessDenied):
        #         LOG().error("sslproxy pid is not found")
        #         continue
        
        return ERR_OK
        
        