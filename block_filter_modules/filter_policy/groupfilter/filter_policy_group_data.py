
#외부 라이브러리
from lib_include import *

'''
그룹별 정책 필터 - 데이터 관리
1차 단계 - 필터별 정책
next - 사용자, 서비스 등 반영. filter 별로 사용자, 서비스, 그룹, default 정책이 수집되어야 한다.
'''

class FilterPolicyGroupData:
    
    def __init__(self):
        
        # filter 별 정책 map, 우선 2단계를 고려하고, 향후 확장한다.
        '''
        dictPolicyRuleMap = {
            "regex_filter": {
                "user" : {},
                "service" : {},
                "group" : {},
                "default" : {},
            }
        }
        '''
        self.__dictPolicyRuleMap:dict = None
        
        pass
    
    # 수집된 정책, 초기화, filter 별로 초기화 한다.
    def ClearPolicyRule(self, strFilterID:str):
        '''
        새로운 값으로 교체한다.
        '''
        # dictPolicyScopeMap:dict = self.__dictPolicyRuleMap[strFilterID]
        # dictPolicyScopeMap.clear()
        
        self.__dictPolicyRuleMap[strFilterID] = {}        
        # pass
    
    #filter 정책 추가.
    def AddPolicyRule(self, strFilterID:str, dictPolicyRuleScopeMap:dict):
        
        '''
        '''
        
        self.__dictPolicyRuleMap[strFilterID] = dictPolicyRuleScopeMap        
        return ERR_OK
    
    #policy 정책 반환, scope 단위로 반환한다. 키 순서 중요.
    def GetPolicyRule(self, strFilterID:str) -> dict:
        
        '''
        '''
        
        #TODO: 없는 ID라도 공백으로 반환
        return self.__dictPolicyRuleMap.get(strFilterID, {})
    
    #룰 개수 반환
    def GetRuleCount(self, strFilterID:str) -> int:
        
        '''
        저장된 
        '''
        
        dictPolicyRuleScopeMap = self.GetPolicyRule(strFilterID)
        
        nPolicyRuleCount:int = 0
        
        for strScope in dictPolicyRuleScopeMap.keys():
            
            lstPolicyRule:list = dictPolicyRuleScopeMap.get(strScope)
            
            nPolicyRuleCount += len(lstPolicyRule)
        
        return nPolicyRuleCount
    
    def Initialize(self, ):
        
        '''
        '''
        
        self.__dictPolicyRuleMap:dict = {}
        
        return ERR_OK