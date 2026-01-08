
#외부 라이브러리
from lib_include import *

'''
filter customize 정책관리
우선 bypass 정책, 

정책의 복잡도를 최대한 가린다.

'''

class FilterCustomConfig:
    
    def __init__(self):
        
        self.__dictFilterConfig:dict = None
        pass
    
    
    # 초기화, 외부에서 가공후 전달하고, config의 연산 기능은 제공하지 않는다.
    def Initialize(self, dictFilterConfig:dict):
        '''
        TODO: config는 최대한 1차 depth로 관리
        '''
        
        self.__dictFilterConfig = dict()
        
        self.__dictFilterConfig.update(dictFilterConfig)
        
        return ERR_OK
    
    # custom 정책, sslproxy bypass
    def GetSSlProxyBypassMask(self, ) -> int:
        
        '''        
        '''
        
        ssl_proxy_bypass_bitmask:int = self.__dictFilterConfig.get(FilterDefile.FILTER_CONFIG_SSL_PROXY_BYPASS_BITMASK, FilterDefile.SSL_PROXY_BYPASS_ALLOW)
        
        return ssl_proxy_bypass_bitmask