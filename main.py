
import getopt
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from lib_include import *

from type_hint import *

from mainapp.pipeline_main_app import PipeLineMainApp

# TODO: 리펙토링 필요
from mainapp.pipeline_global_load_functions import *

#api 관련 모듈들
# from api_modules.helper.fast_api_help_functions import *
from api_modules.router.router_daemon_api import app as daemon_api_router
from api_modules.router.router_pipeline import app as pipeline_router


# #config.py
# from config import *

@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()

#이것만 전역에 추가.
app = FastAPI(docs_url="/docs", redoc_url=None, lifespan=lifespan)

#TODO: 리펙토링 대상, 우선 유지
#Loggin, TODO: 리펙토링. => 기존에 사용하던 logger는 우선 유지.
# Add GLOBAL_LOG_LEVEL for Pipeplines

# Define log levels dictionary
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}

#TODO: 불필요 코드, 리펙토링시 제거
log_level = os.getenv("GLOBAL_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVELS[log_level])

#TODO: 사용할수 없는 변수, 우선 소스 유지
app.state.PIPELINES = PIPELINES

@app.middleware("http")
async def check_url(request: Request, call_next):
    start_time = int(time.time())
    app.state.PIPELINES = get_all_pipelines()
    response = await call_next(request)
    process_time = int(time.time()) - start_time
    response.headers["X-Process-Time"] = str(process_time)

    return response


#fast api 실행 관련, TODO: main 에서 수행할지, mainapp에서 수행할지 향후 결정
def setup_fast_api(fastApi:FastAPI, pipeLineMainApp:PipeLineMainApp, daemonRouter:ApiRouterEx, pipelineRouter:ApiRouterEx):
    
    '''
    '''

    fastApi.add_middleware(
        CORSMiddleware,
        # allow_origins=origins,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    #TODO: fastapi를 상속받을지, 별도의 main app를 개발할지 고려
    #logging, config 등 기반 작업의 설정도 필요
    #일단 기존 형상은 유지, 관리 App를 만들어서 FastAPI 와 App 별도로 호출 가능한 방향으로 검토.
    
    #TODO: global로 참조하는 문제, 개선은 필요
    
    #TODO: fastapi와 공유, state로 관리한다. => 상태에 추가후, 상태가 변경되는지도 확인.
    #TODO: pipelines와 pipeline_modules가 같은 모듈인지 확인은 필요하다.
    dictPipelineModules:dict = PIPELINE_MODULES
    pipeLineMainApp.AttachPipelineModules(dictPipelineModules)
    
    #일단 개발후 리펙토링 => TODO: mainApp로 이동해야 할 필요성.
    #향후를 위해서 dictionary 구조는 유지
    pipelineRouter.AddState(ApiRouterEx.STATE_KEY_MAINAPP, pipeLineMainApp)
    
    daemonRouter.AddState(ApiRouterEx.STATE_KEY_MAINAPP, pipeLineMainApp)
    
    # 각각의 라우터를 등록, daemonRouter를 가장 먼저 등록
    fastApi.include_router(daemonRouter) 
    fastApi.include_router(pipelineRouter)    
    return ERR_OK

def run_uvicorn(fastApi:FastAPI, strFastApiHost:str, nFastApiPort:int):
    
    #TODO: SSL, 우선 무시
    strSSLKeyFilePath = ""
    strSSLCertFilePath = ""
    logLevel = logging.INFO
    
    uvicorn.run(    
            # strApiPath                    
            fastApi,
            host=strFastApiHost,
            port=nFastApiPort,            
            # reload=bReload,
            ssl_keyfile=strSSLKeyFilePath,
            ssl_certfile=strSSLCertFilePath,
            log_level=logLevel,                        
        )
    
    return ERR_OK

def main():
    
    try:

        #Logger, 별도로 사용한다.
        InitLogger("log.txt", TRACE_LOG_PATH, TRACE_PREFIX)
        
        dictOpt = {
            # WEB_HOST : "127.0.0.1",
            APP_PARMETER_DEFINE.WEB_HOST : "0.0.0.0",
            APP_PARMETER_DEFINE.WEB_PORT : 9099,
            APP_PARMETER_DEFINE.CONFIG : CONFIG_FILE_PATH            
        }
        
        opts, args = getopt.getopt(sys.argv[1:], "dhm:pw:f:s:",
            [
                "debug", "printlog",
                
                "host=",                
                "port=",                
            ])
        
        for o, args in opts:

            if o in ("-d", "--debug"):
                LOG().setLevel(logging.DEBUG)
            
            elif o in ("-p", "--printlog"): 
                AddStreamLogger()

            else:
                                
                strOptKey = o[2:]
                
                if None != args and 0 < len(args) :
                    dictOpt[strOptKey] = args
                else:
                    dictOpt[strOptKey] = CONFIG_OPT_ENABLE
        
        LOG().info(f"start process pid = {os.getpid()}, argc = {len(sys.argv)}, argv = {str(sys.argv)}")
        
        pipeLineMainApp = PipeLineMainApp()
        pipeLineMainApp.Initialize(dictOpt)
        
        #TODO: api 부분은 비동기로 호출되어야 하는 문제가 있다.
        
        setup_fast_api(app, pipeLineMainApp, daemon_api_router, pipeline_router)

        #TODO: 이후 데몬 처리는 uvicorn 또는 FastApi에 일임한다.
        #venv기반으로 실행, 프로세스 관리가 되도록 기동 사양을 변경한다. (상태 관리등 필요)
        
        #TODO: port 설정 => config 필요. 테스트, mainapp로 이동
        #TODO: 우선 ssl 인증서는 무시하고, localhost로 기동한다. 포트를 인자로 받도록 향후 개선, guard에서 실행 관리.
        #테스트 시점에서는 외부 접속 테스트를 위해서 0.0.0.0 으로 변경
        # strFastApiHost = "127.0.0.1"
        strFastApiHost = dictOpt.get(APP_PARMETER_DEFINE.WEB_HOST)
        nFastApiPort = int(dictOpt.get(APP_PARMETER_DEFINE.WEB_PORT))
        
        #실행 파라미터 추가.        
        run_uvicorn(app, strFastApiHost, nFastApiPort)
        
    except Exception as err:         
        LOG().error(traceback.format_exc())
        
    finally:        
        LOG().info(f"end process pid = {os.getpid()}, argc = {len(sys.argv)}, argv = {str(sys.argv)}")
        pass
    
    pass


if __name__ == "__main__":
    main()  
    pass