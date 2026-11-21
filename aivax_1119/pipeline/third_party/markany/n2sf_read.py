#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""N2SF 등급 태그 판독기 (참조 구현, 리눅스·윈도 공용)

MarkAny DSP_N2SFTAG.dll 의 N2SF_ReadTags 판독 규칙을 그대로 옮긴 것이다.
문서 파일 안에 기록된 N2SF_* 태그만 읽는다. ADS(NTFS 보조 스트림)는 보지 않는다.

  - 서명(N2SF_Sig) 은 검증하지 않는다. 값을 그대로 돌려줄 뿐이다.
    따라서 편집기로 등급 값을 고친 문서를 가려내지 못한다(가이드 5장).
  - 파일을 수정하지 않는다.

의존성
  - Python 3.8+
  - olefile   : doc/xls/ppt/hwp/hwt 판독에 필요 (pip install olefile)
  - pikepdf   : 암호화 PDF 판독에만 필요 (pip install pikepdf). 없으면 암호화 PDF 는 ERROR

사용
  python3 n2sf_read.py 파일 [파일 ...]          # 판독 결과 JSON 한 줄씩
  python3 n2sf_read.py --allow O 파일 ...        # 반출 판정까지 (허용 등급 쉼표 구분)
  python3 n2sf_read.py --allow O --block-unsupported 파일 ...   # 미지원 형식도 차단

종료코드: 0 = 모든 파일이 판독(또는 판정 허용)됨, 1 = 하나라도 아님, 2 = 사용법 오류
"""
import json
import re
import struct
import sys
import zipfile

__version__ = "1.0 (DLL 26.10.x 판독 규칙 기준)"

# ── 판독 결과 상태 ─────────────────────────────────────────────────────────
OK = "OK"                    # 판독 성공. tags 가 비어 있으면 "태그 없음"
UNSUPPORTED = "UNSUPPORTED"  # 태그를 넣는 형식이 아님 (DRM 암호화 문서도 여기에 온다)
ERROR = "ERROR"              # 열기·파싱 실패 (손상, 의존성 없음 등)


def xml_unescape(s):
    """DLL 과 같은 규칙: 기본 엔터티 5개만 푼다. 숫자 문자참조(&#..;)는 그대로 둔다."""
    out = []
    i = 0
    table = (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"))
    while i < len(s):
        if s[i] != "&":
            out.append(s[i]); i += 1; continue
        for ent, ch in table:
            if s.startswith(ent, i):
                out.append(ch); i += len(ent); break
        else:
            out.append("&"); i += 1
    return "".join(out)


# ── 형식 판별 (확장자가 아니라 내용으로) ───────────────────────────────────
CFB_SIG = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"


def _skip_prolog(b):
    i = 3 if b[:3] == b"\xEF\xBB\xBF" else 0
    n = len(b)
    while True:
        while i < n and b[i:i + 1] in (b" ", b"\t", b"\r", b"\n"):
            i += 1
        if b[i:i + 2] == b"<?":
            e = b.find(b">", i)
            if e < 0:
                return n
            i = e + 1; continue
        if b[i:i + 4] == b"<!--":
            e = b.find(b"-->", i + 4)
            if e < 0:
                return n
            i = e + 3; continue
        return i


def _is_hwpml(b):
    if len(b) < 7 or b[:2] in (b"\xFF\xFE", b"\xFE\xFF"):   # UTF-16 HML 은 지원 안 함
        return False
    i = _skip_prolog(b)
    return b[i:i + 6] == b"<HWPML" and b[i + 6:i + 7] in (b" ", b"\t", b"\r", b"\n", b">")


def sniff(b):
    if b[:4] == b"PK\x03\x04":
        return "ZIP"
    if b[:8] == CFB_SIG:
        return "CFB"
    if b[:5] == b"%PDF-":
        return "PDF"
    if _is_hwpml(b):
        return "HWPML"
    return None


# ── ZIP 계열: OOXML / HWPX / ODF ──────────────────────────────────────────
RE_OOXML = re.compile(r'name="(N2SF_[^"]+)">'
                      r'<(?:[A-Za-z_][-A-Za-z0-9_.]*:)?lpwstr>([^<]*)'
                      r'</(?:[A-Za-z_][-A-Za-z0-9_.]*:)?lpwstr>')
RE_HPF = re.compile(r'<opf:meta name="(N2SF_[^"]+)"[^>]*>([^<]*)</opf:meta>')
RE_ODF = re.compile(r'<meta:user-defined[^>]*meta:name="(N2SF_[^"]+)"[^>]*>([^<]*)</meta:user-defined>')


def _zip_part(z, names, want):
    """엔트리 이름은 대소문자 무시로 찾는다(DLL 의 miniz 기본 동작)."""
    real = names.get(want.lower())
    if real is None:
        return None
    return z.read(real).decode("utf-8", "replace")


def read_zip(data, path):
    import io
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return ERROR, [], "zip 파싱 실패: %s" % e
    names = {n.lower(): n for n in z.namelist()}
    for part, rx in (("docProps/custom.xml", RE_OOXML), ("Contents/content.hpf", RE_HPF)):
        xml = _zip_part(z, names, part)
        if xml is not None:
            return OK, [(k, xml_unescape(v)) for k, v in rx.findall(xml)], part
    xml = _zip_part(z, names, "meta.xml")
    if xml is not None and "office:document-meta" in xml:
        return OK, [(k, xml_unescape(v)) for k, v in RE_ODF.findall(xml)], "meta.xml"
    if "[content_types].xml" in names:
        return OK, [], "OOXML (custom.xml 없음)"          # OOXML 이지만 태그 없음
    return UNSUPPORTED, [], "OOXML/HWPX/ODF 가 아닌 zip"


# ── CFB(OLE2) 계열: doc/xls/ppt/hwp/hwt ───────────────────────────────────
FMTID_USERDEF = bytes([0x05, 0xD5, 0xCD, 0xD5, 0x9C, 0x2E, 0x1B, 0x10,
                       0x93, 0x97, 0x08, 0x00, 0x2B, 0x2C, 0xF9, 0xAE])


def _cp_codec(cp):
    return {1200: "utf-16-le", 949: "cp949", 65001: "utf-8", 1252: "cp1252", 0: "cp949"}.get(cp, "cp1252")


def parse_userdefined(raw):
    """MS-OLEPS 요약정보 스트림에서 UserDefined 섹션의 (이름, 값) 목록. 실패 시 ValueError."""
    if len(raw) < 28:
        raise ValueError("property set 너무 짧음")
    g32 = lambda b, o: struct.unpack_from("<I", b, o)[0]
    numsets = g32(raw, 24)
    if 28 + numsets * 20 > len(raw):
        raise ValueError("property set 헤더 손상")
    for i in range(numsets):
        base = 28 + i * 20
        if raw[base:base + 16] != FMTID_USERDEF:
            continue
        off = g32(raw, base + 16)
        if off + 8 > len(raw):
            raise ValueError("섹션 오프셋 손상")
        sec = raw[off:]
        nprops = g32(sec, 4)
        if nprops > 4096 or 8 + nprops * 8 > len(sec):
            raise ValueError("섹션 손상")
        table = [(g32(sec, 8 + j * 8), g32(sec, 12 + j * 8)) for j in range(nprops)]
        dict_off = next((o for p, o in table if p == 0), 0)
        cp_off = next((o for p, o in table if p == 1), 0)
        codepage = 0
        if cp_off and cp_off + 6 <= len(sec):
            codepage = struct.unpack_from("<h", sec, cp_off + 4)[0] & 0xFFFF
        uni = codepage == 1200
        names = []
        if dict_off:
            ne = g32(sec, dict_off)
            p = dict_off + 4
            for _ in range(ne):
                if p + 8 > len(sec):
                    break
                pid, ln = g32(sec, p), g32(sec, p + 4)
                p += 8
                nb = ln * 2 if uni else ln
                if p + nb > len(sec):
                    raise ValueError("사전 손상")
                nm = sec[p:p + nb].decode(_cp_codec(codepage), "replace").split("\x00")[0]
                p += nb
                if uni and (p - dict_off) % 4:
                    p += 4 - (p - dict_off) % 4
                if pid >= 2:
                    names.append((pid, nm))
        offs = dict(table)
        out = []
        for pid, nm in names:
            vo = offs.get(pid)
            if vo is None or vo + 8 > len(sec):
                continue
            vt, cch = g32(sec, vo), g32(sec, vo + 4)
            if vt == 31 and vo + 8 + cch * 2 <= len(sec):          # VT_LPWSTR
                val = sec[vo + 8:vo + 8 + cch * 2].decode("utf-16-le", "replace").rstrip("\x00")
            elif vt == 30 and vo + 8 + cch <= len(sec):            # VT_LPSTR
                val = sec[vo + 8:vo + 8 + cch].split(b"\x00")[0].decode(_cp_codec(codepage), "replace")
            else:
                continue
            out.append((nm, val))
        return out
    return []


def read_cfb(data, path):
    try:
        import olefile
    except ImportError:
        return ERROR, [], "olefile 모듈 없음 (pip install olefile)"
    try:
        ole = olefile.OleFileIO(data)
    except Exception as e:
        return ERROR, [], "CFB 파싱 실패: %s" % e
    try:
        # 1) 정상 위치  2) 구버전 태거가 쓰던 한컴 요약정보(하위호환)
        for stream in ("\x05DocumentSummaryInformation", "\x05HwpSummaryInformation"):
            if not ole.exists(stream):
                continue
            try:
                kv = parse_userdefined(ole.openstream(stream).read())
            except ValueError as e:
                return ERROR, [], "%s: %s" % (stream[1:], e)
            tags = [(k, v) for k, v in kv if k.startswith("N2SF_")]
            if tags:
                return OK, tags, stream[1:]
        return OK, [], "요약정보에 N2SF 없음"
    finally:
        ole.close()


# ── HWPML(.hml, 확장자가 .hwpx 로 위장된 경우 포함) ──────────────────────
TAIL_SCAN = 64 * 1024


def _decl_codec(b):
    e = b.find(b"?>")
    if e < 0 or e > 512:
        return "utf-8"
    m = re.search(rb"encoding\s*=\s*([\"'])([^\"']*)\1", b[:e])
    if not m:
        return "utf-8"
    enc = m.group(2).decode("ascii", "replace").lower()
    if enc in ("utf-8", "utf8"):
        return "utf-8"
    if enc in ("euc-kr", "ks_c_5601-1987", "cp949", "ksc5601"):
        return "cp949"
    return None


def read_hwpml(data, path):
    codec = _decl_codec(data)
    if codec is None:
        return UNSUPPORTED, [], "지원하지 않는 XML 선언 인코딩"
    t = data.rfind(b"</TAIL>")
    start = t if t >= 0 else max(0, len(data) - TAIL_SCAN)
    e = data.find(b"</N2SF>", start)
    if e < 0:
        return OK, [], "N2SF 블록 없음"
    b = data.rfind(b"<N2SF>", 0, e)
    if b < 0 or b < start:
        return OK, [], "N2SF 블록 없음"
    blk = data[b + 6:e]
    out = []
    i = 0
    while i < len(blk):
        if blk[i:i + 1] != b"<":
            i += 1; continue
        gt = blk.find(b">", i)
        if gt < 0:
            break
        name = blk[i + 1:gt]
        if not name.startswith(b"N2SF_"):
            i = gt + 1; continue
        end_tag = b"</" + name + b">"
        ve = blk.find(end_tag, gt + 1)
        if ve < 0:
            break
        val = xml_unescape(blk[gt + 1:ve].decode(codec, "replace"))
        out.append((name.decode("ascii", "replace"), val))
        i = ve + len(end_tag)
    return OK, out, "HWPML 꼬리 <N2SF>"


# ── PDF ───────────────────────────────────────────────────────────────────
RE_PDF = re.compile(rb"<n2sf:(\w+)>([^<]*)</n2sf:")


def _scan_pdf(buf, out):
    """뒤에 나온 값이 앞의 값을 덮는다(증분 갱신 = 뒤쪽이 최신)."""
    for k, v in RE_PDF.findall(buf):
        key = "N2SF_" + k.decode("ascii", "replace")
        val = xml_unescape(v.decode("utf-8", "replace"))
        for idx, (kk, _) in enumerate(out):
            if kk == key:
                out[idx] = (key, val); break
        else:
            out.append((key, val))


def read_pdf(data, path):
    out = []
    _scan_pdf(data, out)                     # 평문 PDF: 파일 전체를 그대로 스캔
    if b"/Encrypt" not in data:
        return OK, out, "XMP n2sf"
    # 암호화 PDF(빈 사용자 암호 + 권한 제한)는 XMP 도 암호화돼 있다 → 복호해서 다시 본다
    try:
        import pikepdf
    except ImportError:
        return ERROR, out, "암호화 PDF — pikepdf 모듈 없음 (pip install pikepdf)"
    try:
        import io
        with pikepdf.open(io.BytesIO(data)) as pdf:     # 빈 사용자 암호로 연다
            meta = pdf.Root.get("/Metadata")
            if meta is not None:
                _scan_pdf(meta.read_bytes(), out)
    except pikepdf.PasswordError:
        # DLL 과 같게 "태그 없음"으로 본다 → 판정 102(차단). 미지원으로 돌리면 통과돼 버린다
        return OK, out, "사용자 암호가 걸린 PDF (태그 판독 불가 = 태그 없음)"
    except Exception as e:
        return ERROR, out, "암호화 PDF 복호 실패: %s" % e
    return OK, out, "XMP n2sf (암호화 PDF)"


# ── 진입점 ────────────────────────────────────────────────────────────────
READERS = {"ZIP": read_zip, "CFB": read_cfb, "HWPML": read_hwpml, "PDF": read_pdf}


def read_tags(path):
    """반환: {"status": OK|UNSUPPORTED|ERROR, "format": ..., "tags": {N2SF_*: 값}, "detail": ...}"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return {"status": ERROR, "format": None, "tags": {}, "detail": "열기 실패: %s" % e}
    fmt = sniff(data)
    if fmt is None:
        return {"status": UNSUPPORTED, "format": None, "tags": {}, "detail": "알 수 없는 형식"}
    status, kv, detail = READERS[fmt](data, path)
    tags = {}
    for k, v in kv:                 # 같은 이름이 여러 번 나오면 처음 값 (DLL JSON 직렬화와 같음)
        tags.setdefault(k, v)
    return {"status": status, "format": fmt, "tags": tags, "detail": detail}


def decide(result, allow_levels, unsupported_pass=True):
    """반출 판정 (MarkAny 반출 에이전트와 같은 규칙, 서명 검증 단계만 없음).

    반환: (허용 여부, 코드, 사유)
      0   허용
      11  검사 오류 (ERROR) → 차단
      101 허용 목록에 없는 등급 → 차단
      102 태그 없음(등급 미분류) → 차단
      UNSUPPORTED 는 unsupported_pass 에 따른다 (에이전트 기본 = 통과)
    """
    st = result["status"]
    if st == ERROR:
        return False, 11, "문서 검사 중 오류가 발생했습니다."
    if st == UNSUPPORTED:
        if unsupported_pass:
            return True, 0, ""
        return False, 11, "등급을 확인할 수 없는 형식입니다."
    level = result["tags"].get("N2SF_Level", "")
    if not level:
        return False, 102, "등급 미분류 문서는 반출할 수 없습니다."
    if level not in allow_levels:
        return False, 101, "대외비 등급 문서는 반출할 수 없습니다."
    return True, 0, ""


def main(argv):
    allow = None
    unsupported_pass = True
    files = []
    it = iter(argv)
    for a in it:
        if a == "--allow":
            allow = [x.strip() for x in next(it, "").split(",") if x.strip()]
        elif a == "--block-unsupported":
            unsupported_pass = False
        elif a in ("-h", "--help"):
            print(__doc__); return 0
        else:
            files.append(a)
    if not files:
        print(__doc__, file=sys.stderr); return 2
    rc = 0
    for p in files:
        r = read_tags(p)
        r["file"] = p
        if allow is not None:
            ok, code, reason = decide(r, allow, unsupported_pass)
            r["exportable"], r["code"], r["reason"] = ok, code, reason
            if not ok:
                rc = 1
        elif r["status"] != OK:
            rc = 1
        print(json.dumps(r, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
