
#외부 라이브러리
from lib_include import *

'''
그룹별 정책 필터 - 데이터 관리
1차 단계 - 필터별 정책
next - 사용자, 서비스 등 반영.
'''

class FilterPolicyGroupData:
    
    def __init__(self):
        
        # filter 별 정책 map, 우선 2단계를 고려하고, 향후 확장한다.
        self.__dictPolicyRuleMap:dict = None
        
        pass
    
    #filter 정책 추가.
    def AddPolicyRule(self, strFilterID:str, dictPolicyRule:list):
        
        '''
        '''
        
        self.__dictPolicyRuleMap[strFilterID] = dictPolicyRule        
        return ERR_OK
    
    #policy 정책 반환
    def GetPolicyRule(self, strFilterID:str) -> list:
        
        '''
        '''
        
        #TODO: 없는 ID라도 공백으로 반환
        return self.__dictPolicyRuleMap.get(strFilterID, [])
    
    def Initialize(self, ):
        
        '''
        '''
        
        self.__dictPolicyMap:dict = {}
        
        return ERR_OK