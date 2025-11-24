from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from transformers import pipeline
import uvicorn
from typing import Optional
import re

# FastAPI 앱 생성
app = FastAPI(title="PII Masking Service")

# 전역 변수로 모델 저장
pii_detector = None

class PIIMasker:
    """민감정보 탐지 및 마스킹 클래스"""
    
    def __init__(self, model_path: str):
        """
        Args:
            model_path: 로컬 모델 경로
        """
        self.model_path = model_path
        self.detector = None
        self._load_model()
    
    def _load_model(self):
        """모델 로드"""
        print(f"로컬 경로 [{self.model_path}]에서 모델 로드를 시작합니다...")
        self.detector = pipeline(
            "token-classification",
            model=self.model_path,
            aggregation_strategy="simple"
        )
        print("모델 로드가 완료되었습니다.")
    
    def detect_pii(self, text: str) -> list:
        """
        텍스트에서 PII 탐지
        
        Args:
            text: 검사할 텍스트
            
        Returns:
            탐지된 PII 정보 리스트 [{'word': '...', 'start': int, 'end': int, 'entity_group': '...'}, ...]
        """
        if not self.detector:
            raise RuntimeError("모델이 로드되지 않았습니다.")
        
        results = self.detector([text])
        
        if results and len(results) > 0:
            return results[0]  # 첫 번째 문장의 결과 반환
        return []
    
    def mask_pii(self, text: str, mask_char: str = "*") -> dict:
        """
        텍스트의 민감정보를 마스킹
        
        Args:
            text: 원본 텍스트
            mask_char: 마스킹에 사용할 문자 (기본값: *)
            
        Returns:
            {
                'has_pii': bool,
                'original_text': str,
                'masked_text': str,
                'pii_detected': list
            }
        """
        pii_entities = self.detect_pii(text)
        
        if not pii_entities:
            return {
                'has_pii': False,
                'original_text': text,
                'masked_text': text,
                'pii_detected': [],
                'message': '민감정보가 없습니다.'
            }
        
        # 마스킹 처리 (뒤에서부터 처리하여 인덱스 오류 방지)
        masked_text = text
        pii_info = []
        
        # start 위치 기준 역순 정렬
        sorted_entities = sorted(pii_entities, key=lambda x: x['start'], reverse=True)
        
        for entity in sorted_entities:
            start = entity['start']
            end = entity['end']
            word = entity['word']
            entity_type = entity.get('entity_group', 'UNKNOWN')
            
            # 마스킹 문자열 생성
            mask_length = len(word)
            mask_str = mask_char * mask_length
            
            # 텍스트 마스킹
            masked_text = masked_text[:start] + mask_str + masked_text[end:]
            
            pii_info.append({
                'word': word,
                'type': entity_type,
                'position': f"{start}-{end}"
            })
        
        return {
            'has_pii': True,
            'original_text': text,
            'masked_text': masked_text,
            'pii_detected': pii_info,
            'message': f'{len(pii_info)}개의 민감정보가 탐지되어 마스킹되었습니다.'
        }


# Request/Response 모델
class MaskRequest(BaseModel):
    text: str
    mask_char: Optional[str] = "*"

class MaskResponse(BaseModel):
    has_pii: bool
    original_text: str
    masked_text: str
    pii_detected: list
    message: str


@app.on_event("startup")
async def startup_event():
    """서버 시작 시 모델 로드"""
    global pii_detector
    
    # 모델 경로 설정
    local_model_path = "/app/models/korean-pii-masking"
    
    try:
        pii_detector = PIIMasker(local_model_path)
        print("✅ PII Masking 서비스가 준비되었습니다.")
    except Exception as e:
        print(f"❌ 모델 로드 실패: {e}")
        raise


@app.get("/")
async def root():
    """헬스 체크"""
    return {
        "service": "PII Masking Service",
        "status": "running",
        "model_loaded": pii_detector is not None
    }


@app.post("/mask", response_model=MaskResponse)
async def mask_text(request: MaskRequest):
    """
    텍스트의 민감정보를 마스킹합니다.
    
    Args:
        request: MaskRequest 객체
            - text: 검사할 텍스트
            - mask_char: 마스킹 문자 (선택, 기본값: *)
    
    Returns:
        MaskResponse: 마스킹 결과
    """
    if not pii_detector:
        raise HTTPException(status_code=503, detail="모델이 로드되지 않았습니다.")
    
    try:
        result = pii_detector.mask_pii(request.text, request.mask_char)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"마스킹 처리 중 오류 발생: {str(e)}")


@app.post("/detect")
async def detect_pii(request: MaskRequest):
    """
    텍스트에서 민감정보만 탐지합니다 (마스킹 없이).
    
    Args:
        request: MaskRequest 객체
            - text: 검사할 텍스트
    
    Returns:
        탐지된 PII 정보
    """
    if not pii_detector:
        raise HTTPException(status_code=503, detail="모델이 로드되지 않았습니다.")
    
    try:
        pii_entities = pii_detector.detect_pii(request.text)
        
        if not pii_entities:
            return {
                'has_pii': False,
                'message': '민감정보가 없습니다.',
                'pii_detected': []
            }
        
        pii_list = [
            {
                'word': entity['word'],
                'type': entity.get('entity_group', 'UNKNOWN'),
                'position': f"{entity['start']}-{entity['end']}"
            }
            for entity in pii_entities
        ]
        
        return {
            'has_pii': True,
            'message': f'{len(pii_list)}개의 민감정보가 탐지되었습니다.',
            'pii_detected': pii_list
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"탐지 중 오류 발생: {str(e)}")


if __name__ == "__main__":
    # 서버 실행
    uvicorn.run(app, host="0.0.0.0", port=9292)
