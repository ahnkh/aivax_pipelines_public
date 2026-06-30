
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
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Depends, status, HTTPException, UploadFile, File
from fastapi.concurrency import run_in_threadpool

from starlette.responses import StreamingResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from libglobal.global_const import *

from libutil.logger import *

from libconv.py_conv import *

from libjson.json_helper import JsonHelper
from libutil.file_io_helper import FileIOHelper
from libutil.string_buffer_bulk_writer import StringBufferBulkWriter

from libutil.schedule_util import ScheduleUtil

from libhttp.restapi.api_response_handler import ApiResponseHandler

from libjson.json_helper import JsonHelper

from libsql.connector.db_connector import DBConnector
from libsql.connector.mariadb_connector import MariaDBConnector
from libsql.connector.sqlite_connector import SQLiteConnector
from libsql.query_helper.query_helper import QueryHelper

from libhttprequest.local_define.http_request_define import HttpRequestDefine
from libhttprequest.http_request_interface import HttpRequestInterface

from libnetwork.network_util import NetworkUtil

from liboffice.office_document_reader import OfficeDocumentReader

from common_modules.const_define.kshell_global_define import KShellGlobalDefine
from common_modules.const_define.kshell_parameter_define import KShellParameterDefine
from common_modules.const_define.factory_instance_define import FactoryInstanceDefine, InstanceModulePathDefine
from common_modules.const_define.db_sql_define import DBSQLDefine, DBQueryObject

from common_modules.const_define.json_local_config_define import JsonLocalConfigDefine
from common_modules.const_define.web_api_define import WebApiDefine
from common_modules.const_define.error_define import ErrorDefine

from common_modules.const_define.factory_instance_define import FactoryInstanceDefine, InstanceModulePathDefine
from common_modules.const_define.db_sql_define import DBSQLDefine, DBQueryObject

from common_modules.instance_factory.global_instance_factory import GlobalInstanceFactory

from common_modules.db_modules.sql_client_interface import SQLClientInterface
from common_modules.db_modules.sql_map_modules.sql_map_interface import SQLMapInterface

ERR_OK = 1
ERR_FAIL = -1 

class APP_PARMETER_DEFINE:
    WEB_HOST = "host"
    WEB_PORT = "port"
    CONFIG = "config"
    TEST = "test"    

class LOG_INDEX_DEFINE:
    KEY_LLM_FILTER = "llm_filter"
    KEY_INPUT_FILTER = "input_filter"
    KEY_OUTPUT_FILTER = "output_filter"
    KEY_REGEX_FILTER = "regex_filter"
    
    KEY_AIVAX_LOG = "aivax_log"
    
    TYPE_LOG_INPUT = 0
    TYPE_LOG_OUTPUT = 1

class LOCAL_CONFIG_DEFINE:
    KEY_DB_SERVER_DEFAULT_IP = "default_server_ip"
    KEY_DB_SERVER_DEFAULT_PORT = "default_server_port"
    KEY_DB_SERVER_DEFAULT_SCHEME = "default_schema"
    
    KEY_DB_POLL_CYCLE_SECOND = "db_poll_cycle_second"
    
    VAL_DB_SERVER_DEFAULT_IP = "127.0.0.1"
    VAL_DB_SERVER_DEFAULT_PORT = "3000"
    VAL_DB_SERVER_DEFAULT_SCHEME = "http"
    
    VAL_DB_POLL_CYCLE_SECOND = 60

class IPC_ROUTER_DEFINE:
    
    REQUEST_ROUTER_POINT = "router.point"
    
    ROUTER_PIPELINE_FILTER = "multiple_filter"

class AI_SERVICE_DEFINE:
    
    SERVICE_UNDEFINE = 0
    SERVICE_CHAT_GPT = 1
    SERVICE_GEMINI = 2
    SERVICE_CLAUDE = 3
    SERVICE_GROK = 4
    SERVICE_PERPLEXITY = 5
    
    CODE_ASSIST_COPILOT = 10
    CODE_ASSIST_CURSOR = 11
    CODE_ASSIST_CLAUDE = 12
    
    # NAME_SERVICE_UNDEFINE = "undefined" #미지정이면, 일단 GPT로.
    # NAME_SERVICE_UNDEFINE = "openapi.chatgpt" #미지정이면, 일단 GPT로.
    # NAME_SERVICE_CHAT_GPT = "openapi.chatgpt"
    
    NAME_SERVICE_UNDEFINE = "unknown" 
    NAME_SERVICE_CHAT_GPT = "GPT"
    NAME_SERVICE_GEMINI = "Gemini"
    NAME_SERVICE_CLAUDE = "Claude"
    NAME_SERVICE_GROK = "Grok"
    NAME_SERVICE_PERPLEXITY = "Perplexity"
    
    NAME_CODE_ASSIST_COPILOT = "openapi.copilot"
    NAME_CODE_ASSIST_CURSOR = "cursor.ai"
    NAME_CODE_ASSIST_CLAUDE = "claude.code"
    
AI_SERVICE_NAME_MAP = {
    
    AI_SERVICE_DEFINE.SERVICE_UNDEFINE : AI_SERVICE_DEFINE.NAME_SERVICE_UNDEFINE,
    AI_SERVICE_DEFINE.SERVICE_CHAT_GPT : AI_SERVICE_DEFINE.NAME_SERVICE_CHAT_GPT,
    AI_SERVICE_DEFINE.SERVICE_GEMINI : AI_SERVICE_DEFINE.NAME_SERVICE_GEMINI,
    AI_SERVICE_DEFINE.SERVICE_CLAUDE : AI_SERVICE_DEFINE.NAME_SERVICE_CLAUDE,
    AI_SERVICE_DEFINE.SERVICE_GROK : AI_SERVICE_DEFINE.NAME_SERVICE_GROK,
    AI_SERVICE_DEFINE.SERVICE_PERPLEXITY : AI_SERVICE_DEFINE.NAME_SERVICE_PERPLEXITY,   
}

class FilterDefine:
    
    SSL_PROXY_BYPASS_ALLOW = 0b0000
    SSL_PROXY_BYPASS_BLOCK = 0b0001
    SSL_PROXY_BYPASS_MASKING = 0b0010
    
    FILTER_CONFIG_SSL_PROXY_BYPASS_BITMASK = "ssl_proxy_bypass_bitmask"
    FILTER_REGEX_FULL_SCAN_FLAG = "regex_full_scan_flag"
    FILTER_NEXT_DETECT_AFTER_BLOCK = "next_detect_after_block"  
    pass

class DBDefine:
    
    FILTER_KEY_REGEX = "filter-regex"
    FILTER_KEY_BLOCK_FILE = "filter-file-block"
    FILTER_KEY_SLM = "filter-slm"
        
    POLICY_FILTER_SCOPE_USER = "user"
    POLICY_FILTER_SCOPE_SERVICE = "service"
    POLICY_FILTER_SCOPE_GROUP = "group"
    POLICY_FILTER_SCOPE_DEFAULT = "default"
    
    DB_FIELD_SUBJECT_ID = "subject_id"
    DB_FIELD_SUBJECT_VAL = "subject_val"    
    
    DB_FIELD_RULE_ID = "id"
    DB_FIELD_RULE_REGEX_PATTERN = "regex_pattern"
    DB_FIELD_RULE_REGEX_FLAG = "regex_flag"
    
    DB_FIELD_RULE_NAME = "name"
    DB_FIELD_RULE = "rule"
    DB_FIELD_RULE_ACTION = "action"    
    DB_FIELD_RULE_TARGET = "targets"
    DB_FIELD_RULE_CATEGORY = "category"
    DB_FIELD_RULE_SCOPE = "scope"
    
    DB_FIELID_FILTER_DETECT = "filter_detect"
    DB_FIELID_MODE = "mode"
    DB_FIELID_POLICY = "policy"
    DB_FIELID_POLICY_ID = "policy_id"
    DB_FIELID_POLICY_NAME = "policy_name"
    DB_FIELID_MASKED_CONTENTS = "masked_contents"

    
class FilterDetectDefine:
    
    DETECT_REGEX_MATCH = "match"
    
    SCOPE_USER = "user"
    SCOPE_GROUP = "group"
    SCOPE_SERVICE = "service"
    SCOPE_DEFAULT = "default"    
    # pass
    
class SLMDetectDefine:
    
    SLM_EVIDENCE = "slm_evidence"
    
    # pass
    

class RegexPolicyDefine:
    
    BLOCK_REASON_PROMPT_LIMIT = "prompt size exceeds limit"
    
    pass


class FilePolicyDefine:
    
    DB_POLICY_FILE_BLOCK_ALLOW_EXT = "allow_ext"
    DB_POLICY_FILE_BLOCK_MAX_SIZE = "max_size"
    
    LOCAL_CONFIG_USE_FILE_BLOCK_BYPASS_MODE = "use_file_block_bypass_mode"
    
    LOCAL_CONFIG_FILE_BLOCK_TEMP_BACKUP_DIR = "file_block_temp_backup_dir"
    LOCAL_CONFIG_FILE_BLOCK_BACKUP_DIR = "file_block_backup_dir"
    
    BLOCK_REASON_FILE_EXT_LIMIT = "not allowed file extension"
    BLOCK_REASON_FILE_SIZE_LIMIT = "file size exceeds limit"
    BLOCK_REASON_WATER_MARK_HEADER_DETECT = "watermark validation in header"
    BLOCK_REASON_WATER_MARK_OCR_TEXT_DETECT = "ocr sensitive watermark text"
    
    # 유해/민감 정보 파일, 카테고리
    BLOCK_CATEGORY_WATER_MARK_FILE_DETECT = "유해/민감 정보 파일"
    
    # 파일분석, 정책 명
    BLOCK_MESSAGE_WATER_MARK_FILE_DETECT = "민감정보 - DRM Watermark 포함 파일의 차단"
    BLOCK_MESSAGE_OCR_SENSITIVE_FILE_DETECT = "민감정보 - 반출 불가 문서의 차단"
    
    BLOCK_MESSAGE_FILE_SIZE_LIMIT = "파일 사이즈 제한"
    BLOCK_MESSAGE_FILE_EXT_LIMIT = "파일 확장자 제한"
    # pass
    

class APIServerDefine:
    SESSION_COOKIE = "session_id"
    # pass    

TRACE_LOG_PATH = "./trace-log"
TRACE_PREFIX = "pipe_line"  

API_KEY = os.getenv("PIPELINES_API_KEY", "0p3n-w3bu!")
PIPELINES_DIR = os.getenv("PIPELINES_DIR", "./pipelines")

CONFIG_FILE_PATH = "./local_resource/config.json"

CONFIG_OPT_ENABLE = 1 
CONFIG_OPT_DISABLE = 0

from mainapp.module_function import sqlprintf
from common_modules.module_function import sqlbulk
