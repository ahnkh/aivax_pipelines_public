

# router_user.py
from fastapi import APIRouter

from lib_include import *
from type_hint import *

# pipline 전역 리소스 로딩 관련, 우선 함수로 정의하고, 향후 클래스로 변경 검토
from mainapp.pipeline_global_load_functions import *

#uti 이하 모듈들. 리펙토링
from utils.pipelines.auth import bearer_security, get_current_user
from utils.pipelines.main import get_last_user_message, stream_message_template
from utils.pipelines.misc import convert_to_raw_url

app = ApiRouterEx(
    # prefix="/",
    # tags=["pipline"],
)

#TODO: 기존 API의 구조는 1차 리펙토링때는 유지한다.


@app.get("/v1/models")
@app.get("/models")
# async def get_models(user: str = Depends(get_current_user)):
async def get_models():
    """
    Returns the available pipelines
    """
    
    #매 요청마다 최신화 한다.
    
    dictPipeline:dict = get_all_pipelines()
    
    #TODO: 사실 필요없다. 2차 리펙토링때 제거
    # app.AddState(ApiRouterEx.STATE_KEY_PIPELINE_MAP, dictPipeline)
    
    return {
        "data": [
            {
                "id": pipeline["id"],
                "name": pipeline["name"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "openai",
                "pipeline": {
                    "type": pipeline["type"],
                    **(
                        {
                            "pipelines": (
                                pipeline["valves"].pipelines
                                if pipeline.get("valves", None)
                                else []
                            ),
                            "priority": pipeline.get("priority", 0),
                        }
                        if pipeline.get("type", "pipe") == "filter"
                        else {}
                    ),
                    "valves": pipeline["valves"] != None,
                },
            }
            
            for pipeline in dictPipeline.values()
        ],
        "object": "list",
        "pipelines": True,
    }


@app.get("/v1")
@app.get("/")
async def get_status():
    return {"status": True}


@app.get("/v1/pipelines")
@app.get("/pipelines")
async def list_pipelines(user: str = Depends(get_current_user)):
    
    if user == API_KEY:
        return {
            "data": [
                {
                    "id": pipeline_id,
                    "name": PIPELINE_NAMES[pipeline_id],
                    "type": (
                        PIPELINE_MODULES[pipeline_id].type
                        if hasattr(PIPELINE_MODULES[pipeline_id], "type")
                        else "pipe"
                    ),
                    "valves": (
                        True
                        if hasattr(PIPELINE_MODULES[pipeline_id], "valves")
                        else False
                    ),
                }
                for pipeline_id in list(PIPELINE_MODULES.keys())
            ]
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


async def download_file(url: str, dest_folder: str):
    
    filename = os.path.basename(urlparse(url).path)
    
    if not filename.endswith(".py"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="URL must point to a Python file",
        )

    file_path = os.path.join(dest_folder, filename)

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status != 200:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Failed to download file",
                )
            with open(file_path, "wb") as f:
                f.write(await response.read())

    return file_path

@app.post("/v1/pipelines/add")
@app.post("/pipelines/add")
async def add_pipeline(
    form_data: AddPipelineForm, user: str = Depends(get_current_user)
):
    if user != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    try:
        url = convert_to_raw_url(form_data.url)

        print(url)
        file_path = await download_file(url, dest_folder=PIPELINES_DIR)
        await reload()
        return {
            "status": True,
            "detail": f"Pipeline added successfully from {file_path}",
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

@app.post("/v1/pipelines/upload")
@app.post("/pipelines/upload")
async def upload_pipeline(
    file: UploadFile = File(...), user: str = Depends(get_current_user)
):
    if user != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    file_ext = os.path.splitext(file.filename)[1]
    if file_ext != ".py":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only Python files are allowed.",
        )

    try:
        # Ensure the destination folder exists
        os.makedirs(PIPELINES_DIR, exist_ok=True)

        # Define the file path
        file_path = os.path.join(PIPELINES_DIR, file.filename)

        # Save the uploaded file to the specified directory
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Perform any necessary reload or processing
        await reload()

        return {
            "status": True,
            "detail": f"Pipeline uploaded successfully to {file_path}",
        }
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@app.delete("/v1/pipelines/delete")
@app.delete("/pipelines/delete")
async def delete_pipeline(
    form_data: DeletePipelineForm, user: str = Depends(get_current_user)
):
    if user != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    pipeline_id = form_data.id
    pipeline_name = PIPELINE_NAMES.get(pipeline_id.split(".")[0], None)

    if PIPELINE_MODULES[pipeline_id]:
        if hasattr(PIPELINE_MODULES[pipeline_id], "on_shutdown"):
            await PIPELINE_MODULES[pipeline_id].on_shutdown()

    pipeline_path = os.path.join(PIPELINES_DIR, f"{pipeline_name}.py")
    if os.path.exists(pipeline_path):
        os.remove(pipeline_path)
        await reload()
        return {
            "status": True,
            "detail": f"Pipeline {pipeline_id} deleted successfully",
        }
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {pipeline_id} not found",
        )

@app.post("/v1/pipelines/reload")
@app.post("/pipelines/reload")
async def reload_pipelines(user: str = Depends(get_current_user)):
    if user == API_KEY:
        await reload()
        return {"message": "Pipelines reloaded successfully."}
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

@app.get("/v1/{pipeline_id}/valves")
@app.get("/{pipeline_id}/valves")
async def get_valves(pipeline_id: str):
    if pipeline_id not in PIPELINE_MODULES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {pipeline_id} not found",
        )

    pipeline = PIPELINE_MODULES[pipeline_id]

    if hasattr(pipeline, "valves") is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Valves for {pipeline_id} not found",
        )

    return pipeline.valves

@app.get("/v1/{pipeline_id}/valves/spec")
@app.get("/{pipeline_id}/valves/spec")
async def get_valves_spec(pipeline_id: str):
    if pipeline_id not in PIPELINE_MODULES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {pipeline_id} not found",
        )

    pipeline = PIPELINE_MODULES[pipeline_id]

    if hasattr(pipeline, "valves") is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Valves for {pipeline_id} not found",
        )

    return pipeline.valves.schema()

@app.post("/v1/{pipeline_id}/valves/update")
@app.post("/{pipeline_id}/valves/update")
async def update_valves(pipeline_id: str, form_data: dict):

    if pipeline_id not in PIPELINE_MODULES:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {pipeline_id} not found",
        )

    pipeline = PIPELINE_MODULES[pipeline_id]

    if hasattr(pipeline, "valves") is False:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Valves for {pipeline_id} not found",
        )

    try:
        ValvesModel = pipeline.valves.__class__
        valves = ValvesModel(**form_data)
        pipeline.valves = valves

        # Determine the directory path for the valves.json file
        subfolder_path = os.path.join(PIPELINES_DIR, PIPELINE_NAMES[pipeline_id])
        valves_json_path = os.path.join(subfolder_path, "valves.json")

        # Save the updated valves data back to the valves.json file
        with open(valves_json_path, "w") as f:
            json.dump(valves.model_dump(), f)

        if hasattr(pipeline, "on_valves_updated"):
            await pipeline.on_valves_updated()
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )

    return pipeline.valves

@app.post("/v1/{pipeline_id}/filter/inlet")
@app.post("/{pipeline_id}/filter/inlet")
async def filter_inlet(pipeline_id: str, form_data: FilterForm, _request: Request):
    
    '''
    '''
    
    #순환참조 주의
    from mainapp.pipeline_main_app import PipeLineMainApp
    
    #TODO: 기존 함수는 그대로 유지하고, 신규 API를 추가한다.
    # dictPipelines:dict = app.state.PIPELINES 
    
    mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)    
    dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
    
    #test
    # mainApp:PipeLineMainApp = app.state.main_app
    
    # if pipeline_id not in app.state.PIPELINES:
    if pipeline_id not in dictPipelineMap:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Filter {pipeline_id} not found",
        )

    try:
        
        #TODO: 이부분은 우선 무시하자.
        # pipeline = app.state.PIPELINES[form_data.body["model"]]
        pipeline = dictPipelineMap[form_data.body["model"]]
        
        if pipeline["type"] == "manifold":
            pipeline_id = pipeline_id.split(".")[0]
    except:
        pass

    pipeline = PIPELINE_MODULES[pipeline_id]

    try:
        if hasattr(pipeline, "inlet"):
            body = await pipeline.inlet(form_data.body, form_data.user, __request__ = _request)
            return body
        else:
            return form_data.body
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )
      
    #함수 종료의 구분을 위한 추가.  
    # return {}

        

@app.post("/v1/{pipeline_id}/filter/outlet")
@app.post("/{pipeline_id}/filter/outlet")
async def filter_outlet(pipeline_id: str, form_data: FilterForm):
    
    '''
    TODO: 기존 API는 오류만 수정하고, 현상 그대로 유지한다.
    응답에 대한 처리도 일단 보류
    '''
    
    # dictPipelineMap:dict = app.GetState(ApiRouterEx.STATE_KEY_PIPELINE_MAP)
    
    from mainapp.pipeline_main_app import PipeLineMainApp
    mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()
    
    
    # if pipeline_id not in app.state.PIPELINES:
    if pipeline_id not in dictPipelineMap:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Filter {pipeline_id} not found",
        )

    try:
        
        # pipeline = app.state.PIPELINES[form_data.body["model"]]
        pipeline = dictPipelineMap[form_data.body["model"]]
        if pipeline["type"] == "manifold":
            pipeline_id = pipeline_id.split(".")[0]
    except:
        pass

    pipeline = PIPELINE_MODULES[pipeline_id]

    try:
        if hasattr(pipeline, "outlet"):
            body = await pipeline.outlet(form_data.body, form_data.user)
            return body
        else:
            return form_data.body
    except Exception as e:
        print(e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{str(e)}",
        )

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def generate_openai_chat_completion(form_data: OpenAIChatCompletionForm):
    
    messages = [message.model_dump() for message in form_data.messages]
    user_message = get_last_user_message(messages)
    
    # dictPipelineMap:dict = app.GetState(ApiRouterEx.STATE_KEY_PIPELINE_MAP)
    
    from mainapp.pipeline_main_app import PipeLineMainApp
    mainApp:PipeLineMainApp = app.GetState(ApiRouterEx.STATE_KEY_MAINAPP)
    
    dictPipelineMap:dict = mainApp.GetMainAppLinkedPipelineModules()

    if (
        form_data.model not in dictPipelineMap
        or dictPipelineMap[form_data.model]["type"] == "filter"
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pipeline {form_data.model} not found",
        )

    def job():
        print(form_data.model)

        pipeline = dictPipelineMap[form_data.model]
        pipeline_id = form_data.model

        print(pipeline_id)

        if pipeline["type"] == "manifold":
            manifold_id, pipeline_id = pipeline_id.split(".", 1)
            pipe = PIPELINE_MODULES[manifold_id].pipe
        else:
            pipe = PIPELINE_MODULES[pipeline_id].pipe

        if form_data.stream:

            def stream_content():
                res = pipe(
                    user_message=user_message,
                    model_id=pipeline_id,
                    messages=messages,
                    body=form_data.model_dump(),
                )
                logging.info(f"stream:true:{res}")

                if isinstance(res, str):
                    message = stream_message_template(form_data.model, res)
                    logging.info(f"stream_content:str:{message}")
                    yield f"data: {json.dumps(message)}\n\n"

                if isinstance(res, Iterator):
                    for line in res:
                        if isinstance(line, BaseModel):
                            line = line.model_dump_json()
                            line = f"data: {line}"

                        elif isinstance(line, dict):
                            line = json.dumps(line)
                            line = f"data: {line}"

                        try:
                            line = line.decode("utf-8")
                            logging.info(f"stream_content:Generator:{line}")
                        except:
                            pass

                        if isinstance(line, str) and line.startswith("data:"):
                            yield f"{line}\n\n"
                        else:
                            line = stream_message_template(form_data.model, line)
                            yield f"data: {json.dumps(line)}\n\n"

                if isinstance(res, str) or isinstance(res, Generator):
                    finish_message = {
                        "id": f"{form_data.model}-{str(uuid.uuid4())}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": form_data.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "logprobs": None,
                                "finish_reason": "stop",
                            }
                        ],
                    }

                    yield f"data: {json.dumps(finish_message)}\n\n"
                    yield f"data: [DONE]"

            return StreamingResponse(stream_content(), media_type="text/event-stream")
        else:
            
            res = pipe(
                user_message=user_message,
                model_id=pipeline_id,
                messages=messages,
                body=form_data.model_dump(),
            )
            logging.info(f"stream:false:{res}")

            if isinstance(res, dict):
                return res
            elif isinstance(res, BaseModel):
                return res.model_dump()
            else:

                message = ""

                if isinstance(res, str):
                    message = res

                if isinstance(res, Generator):
                    for stream in res:
                        message = f"{message}{stream}"

                logging.info(f"stream:false:{message}")
                return {
                    "id": f"{form_data.model}-{str(uuid.uuid4())}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": form_data.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": message,
                            },
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                }

    return await run_in_threadpool(job)
