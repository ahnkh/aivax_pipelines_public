
from lib_include import *

from common_modules.type_hint import *


def sqlprintf(strQueryMapCategory:str, strQueryMapID:str, dictParameter:dict, dictDBResult:dict):

    '''
    '''

    GlobalCommonModule.SQLPrintf(strQueryMapCategory, strQueryMapID, dictParameter, dictDBResult)

    return ERR_OK

#sql bulk insert/update
def sqlbulk(strQueryMapCategory:str, strQueryMapID:str, lstBulkData:list, dictDBResult:dict):
    
    '''
    '''
    
    GlobalCommonModule.SQLBulkInsert(strQueryMapCategory, strQueryMapID, lstBulkData, dictDBResult)
    
    return ERR_OK
