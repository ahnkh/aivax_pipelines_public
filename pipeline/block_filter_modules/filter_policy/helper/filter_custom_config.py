
#외부 라이브러리
from lib_include import *

from type_hint import *

'''
filter customize 정책관리
우선 bypass 정책, 

정책의 복잡도를 최대한 가린다.

'''

class FilterCustomConfig:
    
    def __init__(self):
        
        # self.__dictFilterConfig:dict = None
        
        self.__customFilterConfigItem:PipelineCustomFilterConfigItem = None
        # pass
    
    
    # 초기화, 외부에서 가공후 전달하고, config의 연산 기능은 제공하지 않는다.
    def Initialize(self, dictFilterConfig:dict):
        '''
        TODO: config는 최대한 1차 depth로 관리
        '''
        
        # self.__dictFilterConfig = dict()
        self.__customFilterConfigItem = PipelineCustomFilterConfigItem()
        
        #TODO: 좋지 않다. model을 만들자.
        # self.__dictFilterConfig.update(dictFilterConfig)
        
        self.__updateFilterConfigItem(self.__customFilterConfigItem, dictFilterConfig)
        
        return ERR_OK
    
    ################################ getter/setter/property
    
    @property
    def filterConfig(self) -> PipelineCustomFilterConfigItem:
        '''
        '''
        
        return self.__customFilterConfigItem
    
    
    # @property
    # def ssl_proxy_bypass_mask(self) -> int:
    #     '''
    #     '''
    #     ssl_proxy_bypass_bitmask:int = self.__customFilterConfigItem.ssl_proxy_bypass_bitmask        
    #     return ssl_proxy_bypass_bitmask
    
    # custom 정책, sslproxy bypass
    def GetSSLProxyBypassMask(self) -> int:
        
        '''        
        '''
        
        # ssl_proxy_bypass_bitmask:int = self.__dictFilterConfig.get(FilterDefine.FILTER_CONFIG_SSL_PROXY_BYPASS_BITMASK, FilterDefine.SSL_PROXY_BYPASS_ALLOW)
        ssl_proxy_bypass_bitmask:int = self.__customFilterConfigItem.ssl_proxy_bypass_bitmask        
        return ssl_proxy_bypass_bitmask
        
    # #pipeline, 모든 필터의 탐지 옵션
    # def IsAllPipelineFilterDetect(self) -> bool:
    #     '''
    #     차단이후의 파이프라인의 동작 제어
    #     TODO: 반환방법.. 깔끔하지는 않다. 좀더 좋은 방법을 찾아봐야 한다.
    #     '''
    #     return self.__customFilterConfigItem.all_pipeline_filter_detect
    
    
    ######################################## private
    
    # 외부 설정에 대한 내부 업데이트
    def __updateFilterConfigItem(self, customFilterConfigItem:PipelineCustomFilterConfigItem, dictFilterConfig:dict) -> int:
        
        '''
        향후에는 DB에서 읽어와야 한다.
        '''
        
        customFilterConfigItem.ssl_proxy_bypass_bitmask = dictFilterConfig.get(FilterDefine.FILTER_CONFIG_SSL_PROXY_BYPASS_BITMASK, FilterDefine.SSL_PROXY_BYPASS_ALLOW)
        
        #regex full scan flag, 
        customFilterConfigItem.regex_filter_config.full_scan_flag = dictFilterConfig.get(FilterDefine.FILTER_REGEX_FULL_SCAN_FLAG, False)
        
        #차단후 다음 탐지 수행 여부
        customFilterConfigItem.next_detect_after_block = dictFilterConfig.get(FilterDefine.FILTER_NEXT_DETECT_AFTER_BLOCK, False)        
        return ERR_OK