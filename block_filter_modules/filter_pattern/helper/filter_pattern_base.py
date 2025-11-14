
from abc import ABC, abstractmethod

from lib_include import *

'''
Filter Pattern의 기본 클래스, 외부에서 공통으로 접근시 사용.
'''

class FilterPatternBase:
    
    #상수, 우선 여기에 선언
    POLICY_CHANGED = True
    POLICY_NOT_CHANGED = False
    
    def __init__(self):
        
        #수신받은 정책, 공통으로 저장 관리
        self.__dictDBFilterPolicy:dict = {}
        pass
    
    #정책, DB의 정책을 업데이트, 보관한다.
    def UpdateBaseDBFilterPolicy(self, dictDBFilterPolicy:dict):
        
        '''
        우선 DB의 정책을 전체를 가지는 형태로 관리한다.
        만일 정책별로 키를 가지고 있다면, 키로 필터링된 정책을 제공한다.
        '''
        
        #기존에 정책이 존재한다면, 삭제한다.
        #TODO: hash, 변경여부를 체크하는 로직은 향후에 고려한다.
        
        if 0 == len(dictDBFilterPolicy):
            LOG().error("invalid db filter policy, skip update db filter")
            return ERR_FAIL
        
        if 0 < len(self.__dictDBFilterPolicy):
            self.__dictDBFilterPolicy.clear()
        
        self.__dictDBFilterPolicy.update(dictDBFilterPolicy)
        
        return ERR_OK
    
    # 정책 비교 로직 추가, 같은 정책인지 확인 -> 우선 구현후 별도 확장 여부 검토
    def IsFilterPolicyChanged(self, dictNewDBFilterPolicy:dict) -> bool:
        
        '''
        우선 전체를 순회, 변경되었는지 확인한다., data 필드만 추출한다. 
        TODO: 변경 상태에 대해서 다양한 상태가 있을수 있다. 1차 개발은 변경 여부 (변경/미변경)만 가정
        
        1차 비교 : 룰 카운트가 안맞으면 볼필요 없이 changed
        2차 비교 : 룰 카운트가 같으면, 내부 데이터 비교, 신규 룰로 비교, 
        - 신규룰의 name, rule, action 이 기존과 다른게 하나라도 있으면
        - 전체 삭제후 업데이트하는 로직으로 정리
        
        TODO: 구조상 분리해야 하는 기능이 있으나, 분리하지 않고 마무리 한다.
        '''
        
        lstNewPolicyData:list = dictNewDBFilterPolicy.get("data")
        
        #처음에는 없다.
        lstCurrentPolicyData:list = self.__dictDBFilterPolicy.get("data")
        
        if None == lstCurrentPolicyData:
            LOG().info("filter policy is changed, no current data")
            return FilterPatternBase.POLICY_CHANGED
        
        nNewPolicyCount:int = len(lstNewPolicyData)
        nCurrentPolicyCount:int = len(lstCurrentPolicyData)
        
        if nNewPolicyCount != nCurrentPolicyCount:
            
            LOG().info(f"filter policy is changed, count mismatch, new policy count = {nNewPolicyCount}")
            return FilterPatternBase.POLICY_CHANGED
        
        #TODO: 1개는 hash map 형태로 되어야, 비교가 수월하다.
        #TODO: 우선 모듈화 없이 하나의 함수에서 개발, 이후 분리
        # list를 비교없이 그대로 넣는다.
        
        dictPolicyCompareMap:dict = {}
        
        for dictEachPolicy in lstCurrentPolicyData:
            
            strRuleName:str = dictEachPolicy.get("name")            
            dictPolicyCompareMap[strRuleName] = dictEachPolicy
            
        #신규 룰을 선회, 다른 정책이 발견되면 changed (True)로 반환, 종료
        for dictNewPolicy in lstNewPolicyData:
            
            strNewRuleName:str = dictNewPolicy.get("name")
            strNewAction:str = dictNewPolicy.get("action")
            strNewRulePattern:str = dictNewPolicy.get("rule")
            
            dictCurrentPolicy:dict = dictPolicyCompareMap.get(strNewRuleName)
            
            # 기존 정책이 없는 정책 (신규 추가)
            if None == dictCurrentPolicy:
                
                LOG().info(f"filter policy is changed, rule = {strNewRuleName}")
                return FilterPatternBase.POLICY_CHANGED
            
            # 둘다 있으면 정책의 내용을 비교
            # strCurrentRuleName:str = dictCurrentPolicy.get("name")
            strCurrentAction:str = dictCurrentPolicy.get("action")
            strCurrentRulePattern:str = dictCurrentPolicy.get("rule")
            
            if (strNewAction != strCurrentAction) or (strNewRulePattern != strCurrentRulePattern):
                
                LOG().info(f"filter policy is changed, rule = {strNewRuleName}, pattern = {strCurrentRulePattern}")
                return FilterPatternBase.POLICY_CHANGED
        
        #모든 정책을 순회했으면 changed.
        #UI등에서 정책을 삭제후 추가, 개수는 같은데 정책이 다른경우이면 비교 로직이 명확하지 않은 문제는 있다.
        #양쪽을 다 조회해야 하나, 우선 넘어간다.
        #그래서 향후에는 둘의 데이터를 hash를 만들어서 비교하는 방법도 고려하며, 현 시점에서는 
        #그정도의 정확도는 불필요하다고 판단하여 skip 한다. (메모리 비교이므로, 서버를 재시작하면 우회 대응 가능)

        #반환값 주의, 같으면 False, 다르면 True        
        return FilterPatternBase.POLICY_NOT_CHANGED
    
    #각 하위 클래스에서 상속받아서, 다르게 사용할 수도 있도록 구성
    @abstractmethod
    def notifyUpdateDBPatternPolicy(self, dictFilterPolicy:dict) -> int:
        '''
        '''
        
        return ERR_OK