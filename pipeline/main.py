
import getopt
import uvicorn
import uvloop
import secrets

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from lib_include import *

from type_hint import *

from mainapp.pipeline_main_app import PipeLineMainApp

from mainapp.pipeline_global_load_functions import *

from api_modules.router.router_daemon_api import app as daemon_api_router
from api_modules.router.router_pipeline import app as pipeline_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()

app = FastAPI(docs_url="/docs", redoc_url=None, root_path="", openapi_url="/openapi.json", lifespan=lifespan, default_response_class=ORJSONResponse)

uvloop.install()

app.state.PIPELINES = PIPELINES

# @app.middleware("http")
# async def check_url(request: Request, call_next):
#     start_time = int(time.time())
#     app.state.PIPELINES = get_all_pipelines()
#     response = await call_next(request)
#     process_time = int(time.time()) - start_time
#     response.headers["X-Process-Time"] = str(process_time)

#     return response

# 세션 생성 middleware 추가
# SESSION_COOKIE = "session_id"
# @app.middleware("http")
# async def session_middleware(request: Request, call_next: Any):

#     session_id = request.cookies.get(APIServerDefine.SESSION_COOKIE)

#     # 세션 없으면 생성
#     if not session_id:
#         # session_id = str(uuid.uuid4())
#         session_id = secrets.token_hex(16) #TODO: 이게 더 성능이 빠르다.

#     # request id 생성
#     # request_id = str(uuid.uuid4())

#     request.state.session_id = session_id
#     # request.state.request_id = request_id

#     response: Response = await call_next(request)

#     # 최초 요청이면 cookie 생성
#     # if APIServerDefine.SESSION_COOKIE not in request.cookies:
#     if not request.cookies.get(APIServerDefine.SESSION_COOKIE):
#         response.set_cookie(
#             key=APIServerDefine.SESSION_COOKIE,
#             value=session_id,
#             httponly=True
#         )

#     # 응답 헤더에 request id 추가
#     # response.headers["X-Request-ID"] = request_id

#     return response

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
    
    dictPipelineModules:dict = PIPELINE_MODULES
    pipeLineMainApp.AttachPipelineModules(dictPipelineModules)
    
    pipelineRouter.AddState(ApiRouterEx.STATE_KEY_MAINAPP, pipeLineMainApp)
    
    daemonRouter.AddState(ApiRouterEx.STATE_KEY_MAINAPP, pipeLineMainApp)
    
    fastApi.include_router(daemonRouter) 
    fastApi.include_router(pipelineRouter)    
    return ERR_OK

def run_uvicorn(fastApi:FastAPI, strFastApiHost:str, nFastApiPort:int):
    
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
            loop="uvloop",
            http="httptools",
        )
    
    return ERR_OK

def main():
    
    try:

        InitLogger("log.txt", TRACE_LOG_PATH, TRACE_PREFIX)
        
        dictOpt = {
            # APP_PARMETER_DEFINE.WEB_HOST : "0.0.0.0",
            APP_PARMETER_DEFINE.WEB_HOST : "127.0.0.1",
            APP_PARMETER_DEFINE.WEB_PORT : 9099,
            APP_PARMETER_DEFINE.CONFIG : CONFIG_FILE_PATH            
        }
        
        opts, args = getopt.getopt(sys.argv[1:], "dp",
            [
                "debug", "printlog",
                
                "host=",
                "port=",
                "test",
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
        
        setup_fast_api(app, pipeLineMainApp, daemon_api_router, pipeline_router)

        #TODO: 이후 데몬 처리는 uvicorn 또는 FastApi에 일임한다.
        #venv기반으로 실행, 프로세스 관리가 되도록 기동 사양을 변경한다. (상태 관리등 필요)
        
        #TODO: port 설정 => config 필요. 테스트, mainapp로 이동
        # strFastApiHost = "127.0.0.1"
        strFastApiHost = dictOpt.get(APP_PARMETER_DEFINE.WEB_HOST)
        nFastApiPort = int(dictOpt.get(APP_PARMETER_DEFINE.WEB_PORT))
          
        #  외부에서 실행          
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


# def build_app():

#     InitLogger("log.txt", TRACE_LOG_PATH, TRACE_PREFIX)

#     dictOpt = {
#         APP_PARMETER_DEFINE.WEB_HOST: "127.0.0.1",
#         APP_PARMETER_DEFINE.WEB_PORT: 9099,
#         APP_PARMETER_DEFINE.CONFIG: CONFIG_FILE_PATH,
#     }

#     pipeLineMainApp = PipeLineMainApp()
#     pipeLineMainApp.Initialize(dictOpt)

#     setup_fast_api(app, pipeLineMainApp, daemon_api_router, pipeline_router)

#     return app

# # worker import 시 실행됨
# build_app()
