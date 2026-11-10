
/*
database 생성, 수동 생성 => 스크립트 실행전, 미리 생성되어 있어야 한다.
create database stock_base_info;

-- mysql -u root -p -D stock_dbb < maria_stock_init_base_info.sql
*/

/*
참고 사항 - MariaDB는 대소문자를 가리지 않는다.
*/

---------------------------------------------------------------------------------------------------------------------------------------

/*
* 기본 종목 정보
*/
CREATE TABLE IF NOT EXISTS STOCK_BASE_INFO.STOCK_BASE_INFO   
(    
	NO INT NOT NULL,  /* 등록 번호 (자동등록X) */
	DIVISION VARCHAR(16) DEFAULT "", /* 구분 KOSPI / KOSDAQ*/
	STOCK_CODE VARCHAR(16) NOT NULL, /* 종목코드 */
	STOCK_NAME VARCHAR(128) NOT NULL, /* 종목명 */

	PRIMARY KEY (STOCK_CODE, STOCK_NAME)
);

/*
* csv 종목 코드 순서 번호. (sqlite, maria 동일)
*/
CREATE TABLE IF NOT EXISTS STOCK_BASE_INFO.STOCK_CSV_CODE_DB
(
  	NO INT NOT NULL,  /* 등록 번호 (자동등록X) */
	STOCK_CODE VARCHAR(16) NOT NULL, /* 종목코드 */
	STOCK_NAME VARCHAR(128) NOT NULL, /* 종목명 */

	PRIMARY KEY(NO) /* 중복 등록만 방지, 종목코드가 이미 중복되어 있음. */
);

/*
* 종목 업데이트 이력, 2017 이후, 자동증가 시퀀스는 제거 (PK가 더 중요)
*/
CREATE TABLE IF NOT EXISTS STOCK_BASE_INFO.STOCK_BASE_INFO_HISTORY  
(

	STOCK_CODE VARCHAR(16) NOT NULL, /* 종목코드 */
	CURRENT_STOCK_NAME VARCHAR(128) NOT NULL, /* 현재 종목명 */
	OLD_STOCK_NAME VARCHAR(128) NOT NULL, /* 과거 종목명 */
	REG_DATE INT NOT NULL, /* 등록일자, 변경일자 (INT vs VARCHAR) */
	UPDATE_HISTORY VARCHAR (256) DEFAULT '', /* 업데이트 이력 (최초등록, 종목명변경, 종목삭제, 재등록, 신규 등록) */
	MY_MEMO VARCHAR (256) DEFAULT '', /* 비고 개인 정보 추가 란 */

	/* PRIMARY KEY (STOCK_CODE, CURRENT_STOCK_NAME, REG_DATE) -- 기본키 */
	PRIMARY KEY (STOCK_CODE, REG_DATE) /* 기본키 */
); 

