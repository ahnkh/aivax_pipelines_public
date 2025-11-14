
import shutil
import aiohttp
import os
import importlib.util
import logging
import time
import json
import uuid
import sys
import subprocess
import traceback
import requests
import datetime

from datetime import timezone

from typing import List, Optional, Dict, Union, Generator, Iterator, Tuple, Any
from urllib.parse import urlparse

#TODO: 개별 모듈과 공통 모듈 분리.

from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Depends, status, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool

from starlette.responses import StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from libglobal.global_const import *

from libutil.logger import  InitLogger, LOG, AddStreamLogger

from libconv.py_conv import *

from libhttp.restapi.api_response_handler import ApiResponseHandler

from libjson.json_helper import JsonHelper

# define, const

ERR_OK = 1
ERR_FAIL = -1 

#실행 파라미터 define
class APP_PARMETER_DEFINE:
    WEB_HOST = "host"
    WEB_PORT = "port"
    CONFIG = "config"
    pass

#log index define => index와 log writer id를 동일하게 관리
class LOG_INDEX_DEFINE:
    KEY_LLM_FILTER = "llm_filter"
    KEY_INPUT_FILTER = "input_filter"
    KEY_OUTPUT_FILTER = "output_filter"
    KEY_REGEX_FILTER = "regex_filter"
    pass

#local 설정 config define, 모듈이 아직 크지 않아서, local config에 정의한다.
class LOCAL_CONFIG_DEFINE:
    KEY_DB_SERVER_DEFAULT_IP = "default_server_ip"
    KEY_DB_SERVER_DEFAULT_PORT = "default_server_port"
    KEY_DB_SERVER_DEFAULT_SCHEME = "default_schema"
    
    KEY_DB_POLL_CYCLE_SECOND = "db_poll_cycle_second"
    
    #상수 기본값도 같이 정의
    VAL_DB_SERVER_DEFAULT_IP = "127.0.0.1"
    VAL_DB_SERVER_DEFAULT_PORT = "3000"
    VAL_DB_SERVER_DEFAULT_SCHEME = "http"
    
    VAL_DB_POLL_CYCLE_SECOND = 60
    pass


#TODO: TRACE LOG 통일, 정리. => 외부 경로로 지정할수 있음. => tracelog 경로 통일
TRACE_LOG_PATH = "./trace-log"
TRACE_PREFIX = "pipe_line"  


#load_env_file()에서 다시 로드한다. 우선 기본값 개념으로 할당.
API_KEY = os.getenv("PIPELINES_API_KEY", "0p3n-w3bu!")
PIPELINES_DIR = os.getenv("PIPELINES_DIR", "./pipelines")




CONFIG_FILE_PATH = "./local_resource/config.json"

CONFIG_OPT_ENABLE = 1 #설정config json True/False 대응, 1: True, 기타 : False
CONFIG_OPT_DISABLE = 0

