# ─────────────────────────────────────────────────────────────────────────────
# 가져온 코드입니다 (vendored).
#
#   출처   docx-to-hwpx-conversion 스킬의 scripts/convert.py
#   판본   2026-08-28 자 사본, 원본 sha256 앞자리 322c4fe00ca76ab7
#   고침   없음 — 원본 그대로다. 고칠 때는 `PAPERKO:` 주석으로 표시한다.
#
# ⚠ 원본에 저작권·라이선스 표기가 없습니다. 사적으로 쓰는 데는 지장이 없으나
#   **공개 배포 전에는 권리자에게 확인하거나 대체 구현이 필요합니다.**
#   자세한 것은 ../README.md 를 보십시오.
# ─────────────────────────────────────────────────────────────────────────────
"""
docx-to-hwpx 변환 스크립트

사용법:
    python convert.py <input.docx> [output.hwpx] [--skeleton path/to/skeleton.hwpx]

기본 동작:
    - skeleton 미지정 시: 같은 디렉토리의 assets/default_skeleton.hwpx 사용
    - output 미지정 시: <input>.hwpx로 저장

기술적 특징:
    한컴이 만든 hwpx를 reference skeleton으로 활용해 정교한 스타일 시스템(Heading,
    Source Code, Compact, Body Text 등)을 그대로 사용한다. docx의 SourceCode 스타일
    문단에 있는 syntax highlighting 토큰(CommentTok, FunctionTok 등)은 한컴 hwpx의
    대응 charPr로 정확히 매핑되어 코드 색상이 보존된다.
"""
import argparse
import os
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from copy import deepcopy

from docx import Document
from docx.oxml.ns import qn

# OMML → HWP 수식 변환기
_script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _script_dir)
from omml_to_hwp import omml_to_hwp_equation, needs_equation_object, script_to_plain_text


# ────────────────────────────────────────────────────────────
# 네임스페이스
# ────────────────────────────────────────────────────────────
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
M = '{http://schemas.openxmlformats.org/officeDocument/2006/math}'
HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
HH = '{http://www.hancom.co.kr/hwpml/2011/head}'

NAMESPACES_TO_REGISTER = [
    ('hs', 'http://www.hancom.co.kr/hwpml/2011/section'),
    ('hp', 'http://www.hancom.co.kr/hwpml/2011/paragraph'),
    ('hp10', 'http://www.hancom.co.kr/hwpml/2016/paragraph'),
    ('hc', 'http://www.hancom.co.kr/hwpml/2011/core'),
    ('hh', 'http://www.hancom.co.kr/hwpml/2011/head'),
    ('hhs', 'http://www.hancom.co.kr/hwpml/2011/history'),
    ('hm', 'http://www.hancom.co.kr/hwpml/2011/master-page'),
    ('hpf', 'http://www.hancom.co.kr/schema/2011/hpf'),
    ('ha', 'http://www.hancom.co.kr/hwpml/2011/app'),
    ('dc', 'http://purl.org/dc/elements/1.1/'),
    ('opf', 'http://www.idpf.org/2007/opf/'),
    ('ooxmlchart', 'http://www.hancom.co.kr/hwpml/2016/ooxmlchart'),
    ('hwpunitchar', 'http://www.hancom.co.kr/hwpml/2016/HwpUnitChar'),
    ('epub', 'http://www.idpf.org/2007/ops'),
    ('config', 'urn:oasis:names:tc:opendocument:xmlns:config:1.0'),
]
for prefix, uri in NAMESPACES_TO_REGISTER:
    ET.register_namespace(prefix, uri)


# ────────────────────────────────────────────────────────────
# docx 스타일 → 한컴 hwpx styleIDRef/paraPrIDRef/charPrIDRef 매핑
# (한컴 reference의 style 정의에 기반)
# ────────────────────────────────────────────────────────────
DOCX_STYLE_TO_HWPX_STYLE = {
    # 영어 스타일 (docx-js, MS Word 기본)
    'Heading1': 36, 'Heading2': 37, 'Heading3': 38, 'Heading4': 39,
    'Heading5': 40, 'Heading6': 41, 'Heading7': 42, 'Heading8': 43, 'Heading9': 44,
    'SourceCode': 56, 'Compact': 18, 'BodyText': 10, 'FirstParagraph': 31,
    'Caption': 13, 'TableCaption': 62, 'ImageCaption': 46,
    'Title': 63, 'Subtitle': 60, 'Author': 6, 'Date': 22,
    'BlockText': 9, 'Definition': 25, 'DefinitionTerm': 26,
    'FootnoteText': 34, 'Abstract': 1, 'AbstractTitle': 2,
    'Normal': 0,
    # 한글 학회지 스타일 (KSMI 등의 한글 docx 템플릿)
    '바탕글': 0,
    '본문': 10,
    '1.': 36, '1':36,                # 1차 헤딩
    '1.1': 37, '1.1.': 37,           # 2차 헤딩
    '1.1.1': 38, '1.1.1.': 38,       # 3차 헤딩
    '1.1.1.1': 39,                   # 4차 헤딩
    '제목': 63,
    '요지': 1, '초록': 1, 'Abstract요': 1,
    'Keywords': 13, '핵심용어': 13,
    '저자': 6, '저자정보': 6, '소속': 6, '국문소속': 6, '영문소속': 6, '이멜주소': 6,
    'table,fig': 13, '표제목': 62, '그림제목': 46, 'Figure': 46, 'Table': 62,
    '수식': 0,                       # 수식 번호 매기는 paragraph
    '참고문헌': 0, 'References': 0,
    '서지정보': 0,
}

DOCX_STYLE_TO_HWPX_PARAPR = {
    # 영어
    'Heading1': 12, 'Heading2': 13, 'Heading3': 14, 'Heading4': 15, 'Heading5': 16,
    'SourceCode': 22, 'Compact': 9, 'BodyText': 6, 'FirstParagraph': 11,
    'Caption': 7, 'Title': 26, 'Normal': 4,
    # 한글
    '바탕글': 4,
    '본문': 6,
    '1.': 12, '1': 12,
    '1.1': 13, '1.1.': 13,
    '1.1.1': 14, '1.1.1.': 14,
    '제목': 26,
    '요지': 5, '초록': 5,
    '저자': 5, '국문소속': 5, '이멜주소': 5,
    'table,fig': 7,
    '수식': 4,
}

DOCX_STYLE_TO_HWPX_CHARPR = {
    # 영어
    'Heading1': 19, 'Heading2': 20, 'Heading3': 21, 'Heading4': 22, 'Heading5': 23,
    'SourceCode': 16, 'Compact': 5, 'BodyText': 5, 'FirstParagraph': 5,
    'Caption': 8, 'Title': 32, 'Normal': 5,
    # 한글
    '바탕글': 5,
    '본문': 5,
    '1.': 19, '1': 19,
    '1.1': 20, '1.1.': 20,
    '1.1.1': 21, '1.1.1.': 21,
    '제목': 32,
    '요지': 5, '초록': 5,
    '저자': 7,
    'table,fig': 8,
    '수식': 5,
}

# docx의 syntax-highlighting 토큰(rStyle) → 한컴 charPrIDRef
RSTYLE_TO_CHARPR = {
    'CommentTok': 10, 'KeywordTok': 12, 'ControlFlowTok': 12, 'DataTypeTok': 13,
    'FunctionTok': 18, 'BuiltInTok': 7, 'AttributeTok': 4, 'NormalTok': 16,
    'OperatorTok': 26, 'StringTok': 9, 'VerbatimStringTok': 9, 'SpecialStringTok': 29,
    'CharTok': 9, 'SpecialCharTok': 9, 'DecValTok': 6, 'BaseNTok': 6, 'FloatTok': 6,
    'ConstantTok': 11, 'VariableTok': 33, 'AlertTok': 2, 'ErrorTok': 2,
    'WarningTok': 3, 'AnnotationTok': 3, 'InformationTok': 3, 'CommentVarTok': 3,
    'PreprocessorTok': 28, 'ImportTok': 25, 'ExtensionTok': 16,
    'DocumentationTok': 15, 'RegionMarkerTok': 16, 'OtherTok': 27,
}


# ════════════════════════════════════════════════════════════
# docx 콘텐츠 추출
# ════════════════════════════════════════════════════════════

def extract_paragraph_segments(p_element, include_rstyle=False):
    """문단을 세그먼트 리스트로 분해. include_rstyle은 코드 syntax highlighting용."""
    segments = []

    def add_text(text, bold=False, italic=False, rstyle=None):
        if not text:
            return
        if (segments and segments[-1]['kind'] == 'text'
                and segments[-1]['bold'] == bold
                and segments[-1]['italic'] == italic
                and segments[-1].get('rstyle') == rstyle):
            segments[-1]['text'] += text
        else:
            segments.append({'kind': 'text', 'text': text, 'bold': bold,
                             'italic': italic, 'rstyle': rstyle})

    def add_math(omml_elem):
        """수식은 OMML element 자체를 저장. 나중에 hp:equation 객체로 변환."""
        segments.append({'kind': 'math', 'omml': omml_elem, 'text': '',
                         'bold': False, 'italic': False, 'rstyle': None})

    def add_image(rId, cx_emu, cy_emu, name=None):
        """이미지 세그먼트 추가."""
        segments.append({'kind': 'image', 'rId': rId,
                         'cx_emu': cx_emu, 'cy_emu': cy_emu, 'name': name,
                         'text': '', 'bold': False, 'italic': False, 'rstyle': None})

    def walk(elem, r_bold=False, r_italic=False, r_rstyle=None):
        for child in elem:
            tag = child.tag.split('}')[-1]
            cns = child.tag.split('}')[0][1:] if '}' in child.tag else ''

            # 수식 노드 (OMML) - element 자체를 보존
            if cns == 'http://schemas.openxmlformats.org/officeDocument/2006/math':
                if tag in ('oMath', 'oMathPara'):
                    add_math(child)
                    continue

            # 그림 (w:drawing) - blip의 r:embed로 rId 추출
            if child.tag == f'{W}drawing':
                A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'
                R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
                WP_NS = 'http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing'

                # blip의 r:embed = rId
                rid = None
                for blip in child.iter(f'{{{A_NS}}}blip'):
                    rid = blip.get(f'{{{R_NS}}}embed')
                    if rid:
                        break

                # 크기 (wp:extent) - EMU 단위
                cx_emu, cy_emu = None, None
                for ext in child.iter(f'{{{WP_NS}}}extent'):
                    cx_emu = ext.get('cx')
                    cy_emu = ext.get('cy')
                    if cx_emu and cy_emu:
                        break

                # 이름 (docPr)
                name = None
                for dp in child.iter(f'{{{WP_NS}}}docPr'):
                    name = dp.get('name')
                    break

                if rid:
                    add_image(rid, cx_emu, cy_emu, name)
                continue

            # w:r (run) - bold/italic/rStyle 갱신 후 재귀
            if child.tag == f'{W}r':
                rPr = child.find(f'{W}rPr')
                nb, ni, nrs = r_bold, r_italic, r_rstyle
                if rPr is not None:
                    b = rPr.find(f'{W}b')
                    if b is not None:
                        v = b.get(f'{W}val', '1')
                        nb = v not in ('0', 'false', 'none')
                    i = rPr.find(f'{W}i')
                    if i is not None:
                        v = i.get(f'{W}val', '1')
                        ni = v not in ('0', 'false', 'none')
                    if include_rstyle:
                        rs = rPr.find(f'{W}rStyle')
                        if rs is not None:
                            nrs = rs.get(f'{W}val')
                walk(child, nb, ni, nrs)
                continue

            # 텍스트 노드
            if child.tag == f'{W}t':
                if child.text:
                    add_text(child.text, r_bold, r_italic, r_rstyle)
            elif child.tag == f'{W}tab':
                # CRITICAL: hp:t 안에 raw tab 문자(U+0009)를 넣으면 한컴 crash.
                # 별도 'tab' 세그먼트로 처리하여 <hp:tab/> element로 변환.
                segments.append({'kind': 'tab', 'text': '',
                                 'bold': r_bold, 'italic': r_italic, 'rstyle': r_rstyle})
            elif child.tag == f'{W}br':
                # 줄바꿈도 raw \n이 위험할 수 있음 - 별도 세그먼트로
                segments.append({'kind': 'br', 'text': '',
                                 'bold': r_bold, 'italic': r_italic, 'rstyle': r_rstyle})
            else:
                walk(child, r_bold, r_italic, r_rstyle)

    walk(p_element)
    return segments


def get_paragraph_style(p_element, styles_map=None):
    """
    문단의 스타일 식별.
    styles_map: {styleId: displayName} - styles.xml에서 추출한 매핑
    """
    pPr = p_element.find(f'{W}pPr')
    if pPr is None:
        return 'Normal'
    pStyle = pPr.find(f'{W}pStyle')
    if pStyle is None:
        return 'Normal'
    val = pStyle.get(f'{W}val', 'Normal')
    # styles_map이 있으면 displayName으로 변환 시도 (예: '1' → '1.')
    if styles_map and val in styles_map:
        display_name = styles_map[val]
        # display_name이 우리 매핑에 있으면 그걸 우선 사용
        if display_name in DOCX_STYLE_TO_HWPX_STYLE:
            return display_name
    return val


def load_docx_styles_map(docx_path):
    """
    docx의 word/styles.xml에서 styleId → displayName 매핑 추출.
    예: {'1': '1.', '2': '1.1', '0': '바탕글'}
    """
    styles_map = {}
    try:
        with zipfile.ZipFile(docx_path, 'r') as zf:
            styles_xml = zf.read('word/styles.xml')
    except (KeyError, zipfile.BadZipFile):
        return styles_map
    try:
        root = ET.fromstring(styles_xml)
    except ET.ParseError:
        return styles_map
    for style in root.iter(f'{W}style'):
        sid = style.get(f'{W}styleId')
        if not sid:
            continue
        name_elem = style.find(f'{W}name')
        if name_elem is not None:
            display = name_elem.get(f'{W}val')
            if display:
                styles_map[sid] = display
    return styles_map


def get_section_column_count(p_element):
    """
    문단의 pPr 안에 sectPr가 있으면 그 안의 cols 정보를 반환.
    docx에서 sectPr는 섹션 끝 문단의 pPr 안에 있고, "다음 섹션"의 단 수를 결정.

    Returns: int or None (sectPr가 없으면 None)
    """
    pPr = p_element.find(f'{W}pPr')
    if pPr is None:
        return None
    sectPr = pPr.find(f'{W}sectPr')
    if sectPr is None:
        return None
    cols = sectPr.find(f'{W}cols')
    if cols is None:
        return 1  # sectPr 있지만 cols 없으면 기본 1단
    num = cols.get(f'{W}num', '1')
    try:
        return int(num)
    except ValueError:
        return 1


def get_body_initial_columns(doc):
    """
    문서 전체의 최종 sectPr (body 직속) 에서 초기 단 수 결정.
    docx는 본문 마지막에 한 번 더 sectPr가 오는데, 그게 문서 전체의 기본 섹션 속성.

    Returns: int (기본 1)
    """
    body = doc.element.body
    # body의 직접 자식 sectPr
    sectPr = body.find(f'{W}sectPr')
    if sectPr is None:
        return 1
    cols = sectPr.find(f'{W}cols')
    if cols is None:
        return 1
    num = cols.get(f'{W}num', '1')
    try:
        return int(num)
    except ValueError:
        return 1


def extract_docx_content(docx_path):
    """docx 본문을 순서대로 추출.
    item 종류:
      - ('para'|'code', segments, style)
      - ('empty',)
      - ('table', table_data)
      - ('column_change', col_count) - 다음 콘텐츠부터 N단으로 전환
    """
    doc = Document(docx_path)
    items = []
    body = doc.element.body

    # styles.xml에서 styleId → displayName 매핑 (한글 docx 호환용)
    styles_map = load_docx_styles_map(docx_path)

    # docx의 단 처리:
    # - body의 자식 sectPr들(인-바디 + body 직속)을 순서대로 수집
    # - 각 sectPr의 cols는 "그 섹션의 단 수"
    # - 인-바디 sectPr i 다음 문단부터 sectPr i+1까지는 (i+1).cols 단
    #
    # 구체적으로:
    #   섹션 0: 본문 처음 ~ 첫 인-바디 sectPr의 문단까지 (= sectprs[0].cols)
    #   섹션 1: 그 다음 ~ 두 번째 인-바디 sectPr (= sectprs[1].cols)
    #   ...
    #   마지막 섹션: 마지막 인-바디 sectPr 다음 ~ body 끝 (= body_sectpr.cols)

    # 모든 sectPr와 그것이 끝나는 문단 위치를 수집
    sectpr_list = []  # [(end_para_index_in_body_paras, cols)]
    body_paras = []  # body의 p 자식들 (순서대로)

    for i, child in enumerate(body):
        if child.tag == f'{W}p':
            body_paras.append((i, child))
            pPr = child.find(f'{W}pPr')
            if pPr is not None:
                sp = pPr.find(f'{W}sectPr')
                if sp is not None:
                    cols = sp.find(f'{W}cols')
                    n = 1
                    if cols is not None:
                        try:
                            n = int(cols.get(f'{W}num', '1'))
                        except ValueError:
                            n = 1
                    sectpr_list.append((len(body_paras) - 1, n))  # 이 문단이 섹션의 끝

    # body 직속 sectPr (마지막 섹션 속성)
    body_sectpr = body.find(f'{W}sectPr')
    body_cols = None
    if body_sectpr is not None:
        cols = body_sectpr.find(f'{W}cols')
        if cols is not None:
            try:
                body_cols = int(cols.get(f'{W}num', '1'))
            except ValueError:
                body_cols = 1
        else:
            body_cols = 1

    # 단 변경이 있는 문서인가? (sectPr가 여러 개거나, 인-바디 sectPr와 body sectPr의 cols가 다르면)
    all_cols = [n for _, n in sectpr_list]
    if body_cols is not None:
        all_cols.append(body_cols)
    has_column_variation = len(set(all_cols)) > 1

    # 단 변경 미사용 시: 일반 처리 (column_change 마커 없음)
    # 단 변경 사용 시: 본문 첫 줄 직전에 첫 섹션의 cols, 그리고 각 섹션 경계에 마커 삽입

    # 본문 첫 단: 단 변경 있으면 sectpr_list[0]의 cols, 없으면 None
    if has_column_variation:
        if sectpr_list:
            initial_columns = sectpr_list[0][1]
        else:
            initial_columns = body_cols if body_cols else 1
        # skeleton 기본값이 1단이므로, 초기 단이 1단이면 명시 마커 불필요
        if initial_columns == 1:
            initial_columns = None
    else:
        initial_columns = None

    # 각 인-바디 sectPr "다음 문단 직전"에 새 단 적용
    # sectPr[i]가 문단 X에 있으면, 문단 X+1 직전에 sectpr_list[i+1].cols (또는 body_cols) 적용
    # 단, 첫 인-바디 sectPr는 본문의 첫 섹션 끝이고, 그 다음 섹션이 두 번째 sectPr가 가리킴
    # 그래서: sectpr_list[i] 만남 → 다음 문단 직전에 적용할 단 = (sectpr_list[i+1] or body_cols)

    sectpr_idx = 0  # 다음에 만날 인-바디 sectPr 위치 (body_paras 인덱스)
    next_section_cols = None  # 다음 문단 직전에 적용할 새 단 수
    body_para_idx = -1  # body_paras 안에서의 현재 위치

    current_columns = 1  # skeleton 기본 단 수
    pending_column_change = None

    first_para_processed = False
    for element in body.iterchildren():
        tag = element.tag.split('}')[-1]

        if tag == 'p':
            body_para_idx += 1

            # 본문 첫 문단 직전: 초기 단 적용 (필요한 경우)
            if not first_para_processed:
                first_para_processed = True
                if initial_columns is not None and initial_columns != current_columns:
                    items.append(('column_change', initial_columns))
                    current_columns = initial_columns

            # 이전 sectPr로 인한 단 전환 적용
            if pending_column_change is not None and pending_column_change != current_columns:
                items.append(('column_change', pending_column_change))
                current_columns = pending_column_change
            pending_column_change = None

            style = get_paragraph_style(element, styles_map)
            is_code = (style == 'SourceCode')
            segments = extract_paragraph_segments(element, include_rstyle=is_code)

            if is_code:
                items.append(('code', segments, style))
                continue

            has_content = any(
                s.get('text', '').strip()
                or s.get('kind') in ('math', 'image', 'tab', 'br')
                for s in segments
            )
            if not has_content:
                items.append(('empty',))
            else:
                for s in segments:
                    if s['kind'] == 'text':
                        s['text'] = s['text'].replace('\n', ' ').replace('\r', '')
                while segments and segments[0].get('kind') == 'text' and not segments[0]['text']:
                    segments.pop(0)
                while segments and segments[-1].get('kind') == 'text' and not segments[-1]['text']:
                    segments.pop()
                if segments:
                    items.append(('para', segments, style))
                else:
                    items.append(('empty',))

            # 이 문단의 pPr 안에 sectPr가 있는지 확인 (섹션 끝 마커)
            pPr = element.find(f'{W}pPr')
            has_sectpr = (pPr is not None and pPr.find(f'{W}sectPr') is not None)
            if has_sectpr and has_column_variation:
                # 다음 섹션의 단 수 결정:
                # 인-바디 sectPr 중 다음 것의 cols, 없으면 body_cols
                sectpr_idx += 1
                if sectpr_idx < len(sectpr_list):
                    pending_column_change = sectpr_list[sectpr_idx][1]
                elif body_cols is not None:
                    pending_column_change = body_cols

        elif tag == 'tbl':
            # 표 직전에 pending 단 전환이 있으면 먼저 삽입
            if pending_column_change is not None and pending_column_change != current_columns:
                items.append(('column_change', pending_column_change))
                current_columns = pending_column_change
            pending_column_change = None

            # 표 데이터: 각 셀을 세그먼트 리스트로 보존 (수식 객체 포함 가능)
            table_data = []
            for tr in element.findall(f'{W}tr'):
                row = []
                for tc in tr.findall(f'{W}tc'):
                    # 셀 내 모든 문단의 세그먼트를 모음
                    cell_segments = []
                    for p in tc.findall(f'{W}p'):
                        segs = extract_paragraph_segments(p)
                        # 빈 텍스트 세그먼트 정리
                        segs = [s for s in segs if
                                (s['kind'] == 'text' and s.get('text', '').strip())
                                or (s['kind'] == 'math' and s.get('omml') is not None)
                                or (s['kind'] == 'image' and s.get('rId'))
                                or (s['kind'] in ('tab', 'br'))]
                        if segs:
                            if cell_segments:
                                # 문단 사이 공백
                                cell_segments.append({'kind': 'text', 'text': ' ',
                                                      'bold': False, 'italic': False,
                                                      'rstyle': None})
                            cell_segments.extend(segs)
                    row.append(cell_segments)
                table_data.append(row)
            items.append(('table', table_data))

    return items


# ════════════════════════════════════════════════════════════
# hwpx 조작
# ════════════════════════════════════════════════════════════

def escape_xml_text(text):
    return text.replace('\x00', '')


def extract_docx_images(docx_path, work_dir, verbose=False):
    """
    docx에서 모든 이미지 파일을 추출하고 hwpx의 BinData/ 폴더에 복사.
    rels 매핑 (rId → 새 image_id) 반환.

    Args:
        docx_path: 입력 docx
        work_dir: hwpx 작업 디렉토리
        verbose: 진행 출력

    Returns:
        dict: {rId: {'image_id': 'image1', 'href': 'BinData/image1.png',
                     'filename': 'image1.png', 'media_type': 'image/png',
                     'docx_path': 'word/media/image1.png'}}
    """
    rels_map = {}

    # 1. docx의 _rels/document.xml.rels 읽기
    with zipfile.ZipFile(docx_path, 'r') as zf:
        rels_data = None
        try:
            rels_data = zf.read('word/_rels/document.xml.rels')
        except KeyError:
            return rels_map

        rels_root = ET.fromstring(rels_data)
        # 네임스페이스 신경 안 쓰고 Relationship 노드만
        for rel in rels_root:
            tag = rel.tag.split('}')[-1]
            if tag != 'Relationship':
                continue
            rId = rel.get('Id')
            rtype = rel.get('Type', '')
            target = rel.get('Target', '')
            if 'image' not in rtype.lower():
                continue
            # target은 'media/image1.png' 형식
            docx_internal = 'word/' + target if not target.startswith('/') else target.lstrip('/')
            filename = os.path.basename(target)
            # 확장자로 미디어 타입 결정
            ext = os.path.splitext(filename)[1].lower()
            media_type = {
                '.png': 'image/png', '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg', '.gif': 'image/gif',
                '.bmp': 'image/bmp', '.tiff': 'image/tiff',
            }.get(ext, 'image/png')

            # image_id: 파일명에서 확장자 제거 (image1.png → image1)
            image_id = os.path.splitext(filename)[0]

            rels_map[rId] = {
                'image_id': image_id,
                'href': f'BinData/{filename}',
                'filename': filename,
                'media_type': media_type,
                'docx_path': docx_internal,
            }

    if not rels_map:
        return rels_map

    # 2. BinData 디렉토리에 이미지 복사
    bindata_dir = os.path.join(work_dir, 'BinData')
    os.makedirs(bindata_dir, exist_ok=True)

    with zipfile.ZipFile(docx_path, 'r') as zf:
        zf_names = set(zf.namelist())
        for rId, info in rels_map.items():
            src = info['docx_path']
            if src in zf_names:
                data = zf.read(src)
                dest = os.path.join(bindata_dir, info['filename'])
                with open(dest, 'wb') as f:
                    f.write(data)

    if verbose:
        print(f"[image] {len(rels_map)}개 이미지를 BinData/에 복사")

    return rels_map


def register_images_in_manifest(work_dir, rels_map):
    """content.hpf 매니페스트에 이미지 item 등록."""
    if not rels_map:
        return
    hpf_path = os.path.join(work_dir, 'Contents', 'content.hpf')
    if not os.path.exists(hpf_path):
        return

    tree = ET.parse(hpf_path)
    root = tree.getroot()

    # manifest 노드 찾기
    OPF = '{http://www.idpf.org/2007/opf/}'
    manifest = None
    for child in root.iter():
        tag = child.tag.split('}')[-1]
        if tag == 'manifest':
            manifest = child
            break

    if manifest is None:
        return

    # 이미 등록된 image_id는 건너뜀
    existing_ids = set()
    for item in manifest.iter():
        tag = item.tag.split('}')[-1]
        if tag == 'item':
            iid = item.get('id')
            if iid:
                existing_ids.add(iid)

    # 새 item 추가
    ns_uri = manifest.tag.split('}')[0][1:] if '}' in manifest.tag else ''
    item_tag = f'{{{ns_uri}}}item' if ns_uri else 'item'

    added = set()
    for info in rels_map.values():
        iid = info['image_id']
        if iid in existing_ids or iid in added:
            continue
        attrs = {
            'id': iid,
            'href': info['href'],
            'media-type': info['media_type'],
            'isEmbeded': '1',
        }
        new_item = ET.SubElement(manifest, item_tag, attrs)
        added.add(iid)

    tree.write(hpf_path, encoding='utf-8', xml_declaration=True)


# EMU(English Metric Unit) → HWPUNIT 변환
# 914400 EMU = 1 inch, 7200 HWPUNIT = 1 inch → EMU / 127 = HWPUNIT
def emu_to_hwpunit(emu_str):
    """EMU 값을 HWPUNIT으로 변환."""
    if not emu_str:
        return 0
    try:
        return int(int(emu_str) / 127)
    except (ValueError, TypeError):
        return 0


def make_pic_run(template_run, image_info, cx_emu=None, cy_emu=None,
                 template_pic_element=None):
    """
    이미지를 담은 hp:run 생성.

    Args:
        template_run: 보통 본문 run의 deepcopy (charPrIDRef 등 보존용)
        image_info: extract_docx_images의 결과 (image_id, filename 포함)
        cx_emu, cy_emu: docx 측 너비/높이 (EMU)
        template_pic_element: skeleton에서 가져온 hp:pic 템플릿 (있으면 사용)
    """
    # 크기 변환
    width_hwp = emu_to_hwpunit(cx_emu) if cx_emu else 42000
    height_hwp = emu_to_hwpunit(cy_emu) if cy_emu else 28000
    if width_hwp <= 0: width_hwp = 42000
    if height_hwp <= 0: height_hwp = 28000

    # 새 run
    if template_run is not None:
        new_run = deepcopy(template_run)
        for child in list(new_run):
            new_run.remove(child)
        new_run.set('charPrIDRef', '0')
    else:
        new_run = ET.Element(f'{HP}run')
        new_run.set('charPrIDRef', '0')

    # template_pic_element이 있으면 그 구조를 활용
    if template_pic_element is not None:
        new_pic = deepcopy(template_pic_element)
        # binaryItemIDRef 갱신
        HC = '{http://www.hancom.co.kr/hwpml/2011/core}'
        img_elem = new_pic.find(f'{HC}img')
        if img_elem is not None:
            img_elem.set('binaryItemIDRef', image_info['image_id'])
        # 크기 갱신
        org_sz = new_pic.find(f'{HP}orgSz')
        if org_sz is not None:
            org_sz.set('width', str(width_hwp))
            org_sz.set('height', str(height_hwp))
        sz = new_pic.find(f'{HP}sz')
        if sz is not None:
            sz.set('width', str(width_hwp))
            sz.set('height', str(height_hwp))
        # imgRect 갱신
        img_rect = new_pic.find(f'{HP}imgRect')
        if img_rect is not None:
            pts = img_rect.findall(f'{HC}pt0') + img_rect.findall(f'{HC}pt1') + \
                  img_rect.findall(f'{HC}pt2') + img_rect.findall(f'{HC}pt3')
            # pt0=(0,0), pt1=(w,0), pt2=(w,h), pt3=(0,h)
            for pt in img_rect.findall(f'{HC}pt0'):
                pt.set('x', '0'); pt.set('y', '0')
            for pt in img_rect.findall(f'{HC}pt1'):
                pt.set('x', str(width_hwp)); pt.set('y', '0')
            for pt in img_rect.findall(f'{HC}pt2'):
                pt.set('x', str(width_hwp)); pt.set('y', str(height_hwp))
            for pt in img_rect.findall(f'{HC}pt3'):
                pt.set('x', '0'); pt.set('y', str(height_hwp))
        # shapeComment 갱신
        sc = new_pic.find(f'{HP}shapeComment')
        if sc is not None:
            sc.text = f"그림입니다.\n원본 그림의 이름: {image_info['filename']}"
        new_run.append(new_pic)
    else:
        # 템플릿 없으면 최소 구조 직접 생성
        HC_NS = 'http://www.hancom.co.kr/hwpml/2011/core'

        # 각 그림마다 고유한 id와 instid 부여 (충돌 방지 - crash 원인)
        # 한컴은 큰 양의 int32를 사용하므로 그에 맞춰 생성
        import random
        unique_id = random.randint(1_000_000_000, 2_000_000_000)
        unique_instid = random.randint(1_000_000_000, 2_000_000_000)

        pic_attrs = {
            'id': str(unique_id),
            'zOrder': '1',                  # 한컴은 1 이상 사용
            'numberingType': 'PICTURE',     # 한컴 표준: PICTURE (NONE이면 인식 못 함)
            'textWrap': 'TOP_AND_BOTTOM',
            'textFlow': 'BOTH_SIDES',
            'lock': '0',
            'dropcapstyle': 'None',
            'href': '',
            'groupLevel': '0',
            'instid': str(unique_instid),
            'reverse': '0',
        }
        new_pic = ET.SubElement(new_run, f'{HP}pic', pic_attrs)
        ET.SubElement(new_pic, f'{HP}offset', x='0', y='0')
        ET.SubElement(new_pic, f'{HP}orgSz',
                      width=str(width_hwp), height=str(height_hwp))
        ET.SubElement(new_pic, f'{HP}curSz', width='0', height='0')
        ET.SubElement(new_pic, f'{HP}flip', horizontal='0', vertical='0')
        ET.SubElement(new_pic, f'{HP}rotationInfo',
                      angle='0', centerX=str(width_hwp // 2),
                      centerY=str(height_hwp // 2), rotateimage='1')
        # renderingInfo
        ri = ET.SubElement(new_pic, f'{HP}renderingInfo')
        for mname in ('transMatrix', 'scaMatrix', 'rotMatrix'):
            ET.SubElement(ri, f'{{{HC_NS}}}{mname}',
                          e1='1', e2='0', e3='0', e4='0', e5='1', e6='0')
        # img
        ET.SubElement(new_pic, f'{{{HC_NS}}}img',
                      binaryItemIDRef=image_info['image_id'],
                      bright='0', contrast='0', effect='REAL_PIC', alpha='0')
        # imgRect
        ir = ET.SubElement(new_pic, f'{HP}imgRect')
        ET.SubElement(ir, f'{{{HC_NS}}}pt0', x='0', y='0')
        ET.SubElement(ir, f'{{{HC_NS}}}pt1', x=str(width_hwp), y='0')
        ET.SubElement(ir, f'{{{HC_NS}}}pt2', x=str(width_hwp), y=str(height_hwp))
        ET.SubElement(ir, f'{{{HC_NS}}}pt3', x='0', y=str(height_hwp))
        # 나머지
        ET.SubElement(new_pic, f'{HP}imgClip',
                      left='0', right=str(width_hwp * 3),
                      top='0', bottom=str(height_hwp * 3))
        ET.SubElement(new_pic, f'{HP}inMargin', left='0', right='0', top='0', bottom='0')
        ET.SubElement(new_pic, f'{HP}imgDim',
                      dimwidth=str(width_hwp * 3), dimheight=str(height_hwp * 3))
        ET.SubElement(new_pic, f'{HP}effects')
        ET.SubElement(new_pic, f'{HP}sz',
                      width=str(width_hwp), widthRelTo='ABSOLUTE',
                      height=str(height_hwp), heightRelTo='ABSOLUTE', protect='0')
        ET.SubElement(new_pic, f'{HP}pos',
                      treatAsChar='1', affectLSpacing='0', flowWithText='1',
                      allowOverlap='1', holdAnchorAndSO='0',
                      vertRelTo='PARA', horzRelTo='COLUMN',
                      vertAlign='TOP', horzAlign='LEFT',
                      vertOffset='0', horzOffset='0')
        ET.SubElement(new_pic, f'{HP}outMargin', left='0', right='0', top='0', bottom='0')
        sc = ET.SubElement(new_pic, f'{HP}shapeComment')
        sc.text = f"그림입니다.\n원본 그림의 이름: {image_info['filename']}"

    return new_run


def find_pic_template(section_root):
    """skeleton의 section에서 hp:pic 템플릿 하나 찾기."""
    for pic in section_root.iter(f'{HP}pic'):
        return deepcopy(pic)
    return None


# 뼈대에서 따라오는 **책갈피**를 걷어낸다.
#
# 뼈대는 한컴이 만든 진짜 문서라 그 문서의 책갈피가 88개 들어 있다 — 이름도
# `asad-v1.2-사용자-매뉴얼` 처럼 남의 것이다. 문단을 **템플릿으로 복사해** 쓰므로 그것이
# 만들어진 문서까지 따라와, 한글의 책갈피 목록에 엉뚱한 이름이 뜬다. 번역한 문서에는
# 있을 까닭이 없다.
#
# 책갈피는 `fieldBegin`(type="BOOKMARK")과 짝이 되는 `fieldEnd` 로 이루어진다. **짝을
# 함께** 지운다 — 한쪽만 남으면 한글이 "문서에 이상이 있다" 며 고쳐 연다.

def strip_bookmarks(root):
    """책갈피 필드를 짝째로 걷어낸다. 지운 개수를 돌려준다."""
    parents = {child: parent for parent in root.iter() for child in parent}

    begin_ids = set()
    doomed = []
    for el in root.iter(f'{HP}fieldBegin'):
        if el.get('type') == 'BOOKMARK':
            if el.get('id'):
                begin_ids.add(el.get('id'))
            doomed.append(el)
    for el in root.iter(f'{HP}fieldEnd'):
        if el.get('beginIDRef') in begin_ids:
            doomed.append(el)

    for el in doomed:
        parent = parents.get(el)
        if parent is None:
            continue
        parent.remove(el)
        # 껍데기만 남은 `hp:ctrl` 은 둘 까닭이 없다.
        if parent.tag == f'{HP}ctrl' and len(parent) == 0:
            grand = parents.get(parent)
            if grand is not None:
                grand.remove(parent)

    return len(doomed)


def load_equation_template():
    """assets/equation_template.xml에서 수식 run 템플릿 로드."""
    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'assets', 'equation_template.xml')
    if not os.path.exists(template_path):
        return None
    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()
    # hp: 접두사를 정식 네임스페이스로 다시 변환
    HPNS = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
    # 임시로 hp 네임스페이스를 추가하여 파싱
    wrapped = f'<root xmlns:hp="{HPNS}">{content}</root>'
    root = ET.fromstring(wrapped)
    # 첫 자식이 run
    return deepcopy(root[0])


def make_equation_run(template_run, hwp_script):
    """수식 run을 새로 만들어 script 내용 교체. 각 수식마다 고유 id/zOrder 부여 (충돌 방지)."""
    import random
    new_run = deepcopy(template_run)
    eq = new_run.find(f'{HP}equation')
    if eq is not None:
        # 고유 id 부여 (한컴 reference는 큰 양의 int32 사용)
        eq.set('id', str(random.randint(1_000_000_000, 2_000_000_000)))
        # zOrder도 매번 다르게
        eq.set('zOrder', str(random.randint(1, 100000)))
        sc = eq.find(f'{HP}script')
        if sc is not None:
            sc.text = hwp_script
    return new_run


def make_column_change_paragraph(template_empty_p, col_count):
    """
    단 변경 ctrl이 들어있는 빈 문단을 생성.
    hwpx에서 단(column)은 <hp:run><hp:ctrl><hp:colPr/></hp:ctrl></hp:run>로 표현.

    Args:
        template_empty_p: 빈 문단 템플릿
        col_count: 단 수 (1, 2, 3 등)
    """
    new_p = deepcopy(template_empty_p)
    new_p.set('styleIDRef', '0')  # 바탕글
    new_p.set('paraPrIDRef', '0')

    # 기존 run 모두 제거
    for r in list(new_p.findall(f'{HP}run')):
        new_p.remove(r)

    # ctrl + colPr 만 들어있는 run 생성
    new_run = ET.Element(f'{HP}run')
    new_run.set('charPrIDRef', '0')

    ctrl = ET.SubElement(new_run, f'{HP}ctrl')

    if col_count <= 1:
        # 1단 (전체 폭)
        col_pr = ET.SubElement(ctrl, f'{HP}colPr')
        col_pr.set('id', '')
        col_pr.set('type', 'NEWSPAPER')
        col_pr.set('layout', 'LEFT')
        col_pr.set('colCount', '1')
        col_pr.set('sameSz', '1')
        col_pr.set('sameGap', '0')
    else:
        # 2단 이상 (balanced newspaper)
        col_pr = ET.SubElement(ctrl, f'{HP}colPr')
        col_pr.set('id', '')
        col_pr.set('type', 'BALANCED_NEWSPAPER')
        col_pr.set('layout', 'LEFT')
        col_pr.set('colCount', str(col_count))
        col_pr.set('sameSz', '1')
        col_pr.set('sameGap', '2268')  # 약 0.8cm 간격

    # linesegarray 앞에 run 삽입
    lsa = new_p.find(f'{HP}linesegarray')
    if lsa is not None:
        new_p.insert(list(new_p).index(lsa), new_run)
    else:
        new_p.append(new_run)

    reset_lineseg(new_p)
    return new_p


def reset_lineseg(p_element):
    """
    linesegarray 자체를 완전히 제거하여 한글이 문단을 열 때 자동으로 줄 위치를 계산하도록 한다.

    배경 (v15~v17 시행착오):
    - v15 이전: skeleton의 lineseg 값을 그대로 복사 → 문단마다 vertpos가 잘못되어 겹침
    - v15~v17: lineseg의 모든 값을 0으로 설정 → 한컴이 재계산 안 하고 그대로 겹쳐 그림
    - v18: linesegarray 자체를 제거 → 한컴이 강제로 재계산 (정답)

    한컴 오피스는 linesegarray가 없는 문단을 만나면 파일 열기 시점에 폰트·문단폭·페이지 위치
    를 종합하여 lineseg를 자동 계산한다. hwpx 스키마는 linesegarray를 필수로 요구하지 않으며
    사용자가 폰트를 바꿨을 때와 동일한 재계산 경로가 자동으로 실행된다.
    """
    lsa = p_element.find(f'{HP}linesegarray')
    if lsa is not None:
        p_element.remove(lsa)


def add_red_charpr(header_path):
    """수식 강조용 빨강 charPr 추가. Returns: 새 id."""
    tree = ET.parse(header_path)
    root = tree.getroot()
    char_props = root.find(f'.//{HH}charProperties')
    if char_props is None:
        return None
    existing = char_props.findall(f'{HH}charPr')
    next_id = max(int(cp.get('id')) for cp in existing) + 1

    # id=5 (바탕글 charPr) 우선 복제
    base = next((cp for cp in existing if cp.get('id') == '5'), existing[0])
    red_cp = deepcopy(base)
    red_cp.set('id', str(next_id))
    red_cp.set('textColor', '#FF0000')
    char_props.append(red_cp)
    char_props.set('itemCnt', str(len(char_props.findall(f'{HH}charPr'))))

    xml_str = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    with open(header_path, 'wb') as f:
        f.write(xml_str)
    return next_id


def make_paragraph(template_p, segments, docx_style, red_charpr_id,
                   is_code=False, preserve_first_run=False,
                   equation_template=None,
                   image_rels=None, pic_template=None):
    """템플릿 문단 복제 후 텍스트만 교체. styleIDRef/paraPrIDRef를 docx 스타일에 맞게 설정.
    수식 세그먼트는 equation_template이 주어지면 hp:equation 객체로 변환.
    이미지 세그먼트는 image_rels + pic_template으로 hp:pic 객체로 변환.
    """
    new_p = deepcopy(template_p)

    new_p.set('styleIDRef', str(DOCX_STYLE_TO_HWPX_STYLE.get(docx_style, 0)))
    new_p.set('paraPrIDRef', str(DOCX_STYLE_TO_HWPX_PARAPR.get(docx_style, 4)))

    existing_runs = list(new_p.findall(f'{HP}run'))
    template_run = existing_runs[0] if existing_runs else None

    if preserve_first_run:
        # 첫 문단(secPr/ctrl 포함)은 텍스트 run만 제거
        for run in existing_runs:
            has_ctrl = run.find(f'{HP}ctrl') is not None
            has_secpr = run.find(f'{HP}secPr') is not None
            if not (has_ctrl or has_secpr):
                new_p.remove(run)
    else:
        for r in existing_runs:
            new_p.remove(r)

    lsa = new_p.find(f'{HP}linesegarray')
    style_default_cp = DOCX_STYLE_TO_HWPX_CHARPR.get(docx_style, 5)

    for seg in segments:
        # 이미지 세그먼트
        if seg['kind'] == 'image':
            rid = seg.get('rId')
            if image_rels and rid in image_rels:
                info = image_rels[rid]
                new_run = make_pic_run(template_run, info,
                                       cx_emu=seg.get('cx_emu'),
                                       cy_emu=seg.get('cy_emu'),
                                       template_pic_element=pic_template)
                if lsa is not None:
                    new_p.insert(list(new_p).index(lsa), new_run)
                else:
                    new_p.append(new_run)
            continue

        # 수식 세그먼트: 구조적이면 hp:equation 객체, 단순하면 일반 텍스트로
        if seg['kind'] == 'math':
            hwp_script = ''
            if seg.get('omml') is not None:
                hwp_script = omml_to_hwp_equation(seg['omml'])
            if not hwp_script:
                continue

            # 구조적 수식만 객체화 (단순 변수명은 텍스트로 - 한글 멈춤 방지)
            if (equation_template is not None
                    and seg.get('omml') is not None
                    and needs_equation_object(hwp_script)):
                new_run = make_equation_run(equation_template, hwp_script)
                if lsa is not None:
                    new_p.insert(list(new_p).index(lsa), new_run)
                else:
                    new_p.append(new_run)
                continue

            # 단순 수식 또는 equation_template 없음: 텍스트로 (빨강 charPr)
            plain = script_to_plain_text(hwp_script)
            if plain:
                cp = red_charpr_id if red_charpr_id else style_default_cp
                new_run = (deepcopy(template_run) if template_run is not None
                           else ET.Element(f'{HP}run'))
                for child in list(new_run):
                    new_run.remove(child)
                new_run.set('charPrIDRef', str(cp))
                ET.SubElement(new_run, f'{HP}t').text = escape_xml_text(plain)
                if lsa is not None:
                    new_p.insert(list(new_p).index(lsa), new_run)
                else:
                    new_p.append(new_run)
            continue

        # tab 세그먼트: <hp:run><hp:t><hp:tab/></hp:t></hp:run> 구조
        # 한컴은 hp:t 내부의 raw tab 문자(U+0009)를 처리하지 못하므로 필수
        if seg['kind'] == 'tab':
            new_run = (deepcopy(template_run) if template_run is not None
                       else ET.Element(f'{HP}run'))
            for child in list(new_run):
                new_run.remove(child)
            new_run.set('charPrIDRef', str(style_default_cp))
            t_elem = ET.SubElement(new_run, f'{HP}t')
            # hp:tab을 hp:t의 자식으로 (한컴 reference 패턴 그대로)
            ET.SubElement(t_elem, f'{HP}tab')
            if lsa is not None:
                new_p.insert(list(new_p).index(lsa), new_run)
            else:
                new_p.append(new_run)
            continue

        # br 세그먼트: 문단 내 강제 줄바꿈 (Shift+Enter)
        # 한컴은 <hp:t><hp:lineBreak/></hp:t> 구조로 표현.
        # docx의 <w:br/>를 무시하면 여러 줄이 한 줄로 뭉개짐 (특히 코드블록에서 심각)
        if seg['kind'] == 'br':
            new_run = (deepcopy(template_run) if template_run is not None
                       else ET.Element(f'{HP}run'))
            for child in list(new_run):
                new_run.remove(child)
            new_run.set('charPrIDRef', str(style_default_cp))
            t_elem = ET.SubElement(new_run, f'{HP}t')
            ET.SubElement(t_elem, f'{HP}lineBreak')
            if lsa is not None:
                new_p.insert(list(new_p).index(lsa), new_run)
            else:
                new_p.append(new_run)
            continue

        # 일반 텍스트 세그먼트
        text = escape_xml_text(seg['text'])
        if not text:
            continue

        # raw tab/newline이 텍스트에 섞여있으면 제거 (안전장치)
        text = text.replace('\t', ' ').replace('\r', '')

        if is_code and seg.get('rstyle'):
            cp = RSTYLE_TO_CHARPR.get(seg['rstyle'], style_default_cp)
        else:
            cp = style_default_cp

        if template_run is not None:
            new_run = deepcopy(template_run)
            for child in list(new_run):
                new_run.remove(child)
        else:
            new_run = ET.Element(f'{HP}run')
        new_run.set('charPrIDRef', str(cp))
        ET.SubElement(new_run, f'{HP}t').text = text

        if lsa is not None:
            new_p.insert(list(new_p).index(lsa), new_run)
        else:
            new_p.append(new_run)

    reset_lineseg(new_p)
    return new_p


def make_empty_para(template_empty_p):
    new_p = deepcopy(template_empty_p)
    for t in new_p.iter(f'{HP}t'):
        t.text = ''
    reset_lineseg(new_p)
    return new_p


def replace_table_data(tbl_element, docx_table_data, equation_template=None):
    """skeleton 표를 docx 데이터로 교체. 행/열 수 동적 확장.
    docx_table_data: [[cell_segments, ...], ...] - 각 셀이 세그먼트 리스트.
    """
    if not docx_table_data:
        return tbl_element
    n_rows = len(docx_table_data)
    n_cols = max((len(r) for r in docx_table_data), default=1) or 1
    tbl_element.set('rowCnt', str(n_rows))
    tbl_element.set('colCnt', str(n_cols))

    rows = tbl_element.findall(f'{HP}tr')
    if not rows:
        return tbl_element
    template_tr = deepcopy(rows[0])
    cells = template_tr.findall(f'{HP}tc')
    if not cells:
        return tbl_element
    template_tc = deepcopy(cells[0])

    sz = tbl_element.find(f'{HP}sz')
    total_width = int(sz.get('width', '40000')) if sz is not None else 40000
    col_width = total_width // n_cols

    for tr in rows:
        tbl_element.remove(tr)

    for r_idx, row_data in enumerate(docx_table_data):
        new_tr = ET.SubElement(tbl_element, f'{HP}tr')
        for c_idx in range(n_cols):
            cell_segments = row_data[c_idx] if c_idx < len(row_data) else []
            # 문자열로 전달된 경우 호환성 처리
            if isinstance(cell_segments, str):
                cell_segments = [{'kind': 'text', 'text': cell_segments,
                                  'bold': False, 'italic': False, 'rstyle': None}]

            new_tc = deepcopy(template_tc)
            cellAddr = new_tc.find(f'{HP}cellAddr')
            if cellAddr is not None:
                cellAddr.set('colAddr', str(c_idx))
                cellAddr.set('rowAddr', str(r_idx))
            cellSpan = new_tc.find(f'{HP}cellSpan')
            if cellSpan is not None:
                cellSpan.set('colSpan', '1')
                cellSpan.set('rowSpan', '1')
            cellSz = new_tc.find(f'{HP}cellSz')
            if cellSz is not None:
                cellSz.set('width', str(col_width))
            new_tc.set('header', '1' if r_idx == 0 else '0')

            # 셀 안의 첫 hp:p를 찾아서 그 안의 run을 세그먼트로 재구성
            cell_p = new_tc.find(f'.//{HP}p')
            if cell_p is not None:
                # 기존 run 제거
                template_p_run = None
                for run in list(cell_p.findall(f'{HP}run')):
                    if template_p_run is None:
                        template_p_run = deepcopy(run)
                        # template_run에서 자식 비우기
                        for c in list(template_p_run):
                            template_p_run.remove(c)
                    cell_p.remove(run)

                # 셀의 기본 charPrIDRef (표내용)
                cell_default_cp = 7
                if template_p_run is not None:
                    cell_default_cp = int(template_p_run.get('charPrIDRef', '7'))

                cell_lsa = cell_p.find(f'{HP}linesegarray')

                # 세그먼트별로 run 추가
                for seg in cell_segments:
                    # tab
                    if seg['kind'] == 'tab':
                        new_run = (deepcopy(template_p_run) if template_p_run is not None
                                   else ET.Element(f'{HP}run'))
                        for child in list(new_run):
                            new_run.remove(child)
                        new_run.set('charPrIDRef', str(cell_default_cp))
                        t_elem = ET.SubElement(new_run, f'{HP}t')
                        ET.SubElement(t_elem, f'{HP}tab')
                        if cell_lsa is not None:
                            cell_p.insert(list(cell_p).index(cell_lsa), new_run)
                        else:
                            cell_p.append(new_run)
                        continue

                    # br (문단 내 강제 줄바꿈)
                    if seg['kind'] == 'br':
                        new_run = (deepcopy(template_p_run) if template_p_run is not None
                                   else ET.Element(f'{HP}run'))
                        for child in list(new_run):
                            new_run.remove(child)
                        new_run.set('charPrIDRef', str(cell_default_cp))
                        t_elem = ET.SubElement(new_run, f'{HP}t')
                        ET.SubElement(t_elem, f'{HP}lineBreak')
                        if cell_lsa is not None:
                            cell_p.insert(list(cell_p).index(cell_lsa), new_run)
                        else:
                            cell_p.append(new_run)
                        continue

                    if seg['kind'] == 'math':
                        hwp_script = ''
                        if seg.get('omml') is not None:
                            hwp_script = omml_to_hwp_equation(seg['omml'])
                        if not hwp_script:
                            continue

                        # 표 셀의 수식도 구조적인 것만 객체화
                        if (equation_template is not None
                                and needs_equation_object(hwp_script)):
                            new_run = make_equation_run(equation_template, hwp_script)
                            if cell_lsa is not None:
                                cell_p.insert(list(cell_p).index(cell_lsa), new_run)
                            else:
                                cell_p.append(new_run)
                            continue

                        # 단순 수식: 텍스트로
                        plain = script_to_plain_text(hwp_script)
                        if plain:
                            new_run = (deepcopy(template_p_run) if template_p_run is not None
                                       else ET.Element(f'{HP}run'))
                            new_run.set('charPrIDRef', str(cell_default_cp))
                            ET.SubElement(new_run, f'{HP}t').text = escape_xml_text(plain)
                            if cell_lsa is not None:
                                cell_p.insert(list(cell_p).index(cell_lsa), new_run)
                            else:
                                cell_p.append(new_run)
                        continue

                    # 일반 텍스트
                    text = escape_xml_text(seg.get('text', ''))
                    if not text.strip() and seg == cell_segments[0]:
                        continue  # 앞 공백 제거
                    # raw tab/newline 안전장치
                    text = text.replace('\t', ' ').replace('\r', '').replace('\n', ' ')
                    new_run = (deepcopy(template_p_run) if template_p_run is not None
                               else ET.Element(f'{HP}run'))
                    new_run.set('charPrIDRef', str(cell_default_cp))
                    ET.SubElement(new_run, f'{HP}t').text = text
                    if cell_lsa is not None:
                        cell_p.insert(list(cell_p).index(cell_lsa), new_run)
                    else:
                        cell_p.append(new_run)

            for p in new_tc.iter(f'{HP}p'):
                reset_lineseg(p)
            new_tr.append(new_tc)
    return tbl_element


# ════════════════════════════════════════════════════════════
# 메인 변환 함수
# ════════════════════════════════════════════════════════════

def convert(docx_path, skeleton_hwpx_path, output_hwpx_path, work_dir=None, verbose=True):
    """
    docx → hwpx 변환.

    Args:
        docx_path: 입력 .docx 경로
        skeleton_hwpx_path: 참조 skeleton .hwpx 경로 (스타일 시스템 제공)
        output_hwpx_path: 출력 .hwpx 경로
        work_dir: 작업 디렉토리 (None이면 임시 디렉토리)
        verbose: 진행 출력 여부
    """
    import tempfile
    cleanup_workdir = False
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix='docx2hwpx_')
        cleanup_workdir = True
    elif os.path.exists(work_dir):
        shutil.rmtree(work_dir)
        os.makedirs(work_dir)
    else:
        os.makedirs(work_dir)

    try:
        # 1. skeleton 풀기
        with zipfile.ZipFile(skeleton_hwpx_path, 'r') as zf:
            zf.extractall(work_dir)

        src_infos = {}
        with zipfile.ZipFile(skeleton_hwpx_path, 'r') as zf:
            for info in zf.infolist():
                src_infos[info.filename] = info

        # 2. 빨강 charPr 추가 (수식용)
        header_path = os.path.join(work_dir, 'Contents', 'header.xml')
        red_id = add_red_charpr(header_path)
        if verbose:
            print(f"[header] 빨강 charPr id={red_id} 추가")

        # 2.5. 수식 템플릿 로드
        equation_template = load_equation_template()
        if verbose:
            print(f"[equation] 템플릿 로드: {'성공' if equation_template is not None else '없음(텍스트로 fallback)'}")

        # 2.6. 이미지 추출 + BinData/에 복사 + 매니페스트 등록
        image_rels = extract_docx_images(docx_path, work_dir, verbose=verbose)
        if image_rels:
            register_images_in_manifest(work_dir, image_rels)
            if verbose:
                print(f"[image] 매니페스트에 {len(image_rels)}개 이미지 등록")

        # 3. section0.xml에서 템플릿 추출
        section_path = os.path.join(work_dir, 'Contents', 'section0.xml')
        tree = ET.parse(section_path)
        root = tree.getroot()

        # 뼈대 원본 문서의 책갈피를 먼저 걷어낸다. 여기서 치워야 아래에서 뜨는
        # 템플릿(deepcopy)이 깨끗하고, 사용자가 제 뼈대를 넣어도 같다.
        removed = strip_bookmarks(root)
        if verbose and removed:
            print(f"[skeleton] 책갈피 {removed}개 제거")

        paragraphs = list(root)

        # 그림 템플릿 (skeleton에 있으면 활용, 없으면 직접 생성)
        pic_template = find_pic_template(root)
        if verbose and image_rels:
            print(f"[image] 그림 템플릿: {'skeleton에서 추출' if pic_template is not None else '직접 생성'}")

        template_first = deepcopy(paragraphs[0])  # 첫 문단 (secPr 포함)
        template_body = None
        template_compact = None
        template_code = None
        template_h2 = None
        template_h3 = None
        template_empty = None
        template_table_para = None

        for p in paragraphs:
            sid = p.get('styleIDRef')
            if p.find(f'.//{HP}tbl') is not None and template_table_para is None:
                template_table_para = deepcopy(p)
            if sid == '10' and template_body is None:
                template_body = deepcopy(p)
            elif sid == '18' and template_compact is None:
                template_compact = deepcopy(p)
            elif sid == '56' and template_code is None:
                template_code = deepcopy(p)
            elif sid == '37' and template_h2 is None:
                template_h2 = deepcopy(p)
            elif sid == '38' and template_h3 is None:
                template_h3 = deepcopy(p)
            elif sid == '0' and template_empty is None:
                ts = [t.text for t in p.iter(f'{HP}t') if t.text and t.text.strip()]
                if not ts:
                    template_empty = deepcopy(p)

        # fallback: 부족한 템플릿은 첫 본문 템플릿으로 대체
        fallback = template_body if template_body is not None else template_first
        if template_body is None: template_body = fallback
        if template_compact is None: template_compact = fallback
        if template_code is None: template_code = fallback
        if template_h2 is None: template_h2 = fallback
        if template_h3 is None: template_h3 = fallback
        if template_empty is None: template_empty = fallback

        if verbose:
            print(f"[skeleton] 템플릿 추출 완료 (최상위 문단 {len(paragraphs)}개)")

        # 4. docx 콘텐츠 추출
        items = extract_docx_content(docx_path)
        if verbose:
            style_counts = {}
            col_changes = []
            for item in items:
                if item[0] in ('para', 'code'):
                    s = item[2]
                    style_counts[s] = style_counts.get(s, 0) + 1
                elif item[0] == 'column_change':
                    col_changes.append(item[1])
            print(f"[docx] 추출 {len(items)}개, 스타일: {style_counts}")
            if col_changes:
                print(f"[docx] 단(column) 전환: {col_changes}")

        # 5. 새 문단 구성
        style_to_template = {
            # 영어
            'Heading1': template_first,
            'Heading2': template_h2,
            'Heading3': template_h3,
            'SourceCode': template_code,
            'Compact': template_compact,
            'BodyText': template_body,
            'FirstParagraph': template_body,
            'Normal': template_body,
            # 한글 학회지 스타일
            '바탕글': template_body,
            '본문': template_body,
            '1.': template_first, '1': template_first,
            '1.1': template_h2, '1.1.': template_h2,
            '1.1.1': template_h3, '1.1.1.': template_h3,
            '제목': template_first,
            '요지': template_body, '초록': template_body,
            'table,fig': template_body,
            '수식': template_body,
        }

        new_paragraphs = []
        # ════════════════════════════════════════════════════════════
        # CRITICAL: 첫 출력 element가 반드시 secPr/colPr를 포함해야 한다.
        # secPr는 페이지 크기/마진/마스터페이지/푸터를 한컴에 알려주는 필수 정보.
        # 누락되면 한컴이 페이지 레이아웃 계산 불가 → quit unexpectedly (crash).
        #
        # docx에서는 첫 콘텐츠가 표/이미지일 수 있고, 우리 first_para_emitted 로직은
        # 'para' item만 잡았기 때문에 첫 출력이 table이면 secPr 누락이 발생함.
        #
        # 해결: 무조건 첫 번째 출력 element로 secPr/ctrl만 포함하는 anchor 문단 삽입.
        # ════════════════════════════════════════════════════════════
        anchor_p = deepcopy(template_first)
        # anchor_p에서 텍스트 run(t만 있는 것)은 제거하고 secPr/ctrl 들어있는 run만 보존
        for run in list(anchor_p.findall(f'{HP}run')):
            has_ctrl = run.find(f'{HP}ctrl') is not None
            has_secpr = run.find(f'{HP}secPr') is not None
            # 자식에 secPr나 ctrl이 있으면 보존, 아니면 제거
            if not (has_ctrl or has_secpr):
                anchor_p.remove(run)
        # anchor 문단은 본문 표시되지 않는 setup용. styleIDRef는 그대로 유지
        # (template_first의 styleIDRef가 보존되어야 secPr가 의미 있음)
        reset_lineseg(anchor_p)
        new_paragraphs.append(anchor_p)

        # 이제 일반 콘텐츠는 first_para_emitted=True인 상태로 처리
        first_para_emitted = True

        for item in items:
            t = item[0]
            if t == 'para':
                segs, style = item[1], item[2]
                tmpl = style_to_template.get(style, template_body)
                new_paragraphs.append(make_paragraph(
                    tmpl, segs, style, red_id,
                    preserve_first_run=False,
                    equation_template=equation_template,
                    image_rels=image_rels, pic_template=pic_template))
            elif t == 'code':
                new_paragraphs.append(make_paragraph(
                    template_code, item[1], 'SourceCode', red_id, is_code=True,
                    preserve_first_run=False,
                    equation_template=equation_template,
                    image_rels=image_rels, pic_template=pic_template))
            elif t == 'empty':
                new_paragraphs.append(make_empty_para(template_empty))
            elif t == 'column_change':
                # 단 변경 ctrl을 가진 빈 문단 삽입
                col_count = item[1]
                new_paragraphs.append(make_column_change_paragraph(template_empty, col_count))
            elif t == 'table':
                if template_table_para is not None:
                    new_p = deepcopy(template_table_para)
                    tbl = new_p.find(f'.//{HP}tbl')
                    if tbl is not None:
                        replace_table_data(tbl, item[1], equation_template=equation_template)
                    # 표 외부 텍스트 (캡션 등) 정리
                    table_ts = set()
                    if tbl is not None:
                        for tt in tbl.iter(f'{HP}t'):
                            table_ts.add(id(tt))
                    for tt in new_p.iter(f'{HP}t'):
                        if id(tt) not in table_ts:
                            tt.text = ''
                    reset_lineseg(new_p)
                    new_paragraphs.append(new_p)
                else:
                    # 표 템플릿이 없으면 텍스트로 평탄화
                    for row in item[1]:
                        # row의 각 셀(세그먼트 리스트)를 문자열로 변환
                        row_texts = []
                        for cell in row:
                            if isinstance(cell, str):
                                row_texts.append(cell)
                            else:
                                # 세그먼트 리스트
                                parts = []
                                for s in cell:
                                    if s['kind'] == 'text':
                                        parts.append(s.get('text', ''))
                                    elif s['kind'] == 'math' and s.get('omml') is not None:
                                        parts.append(omml_to_hwp_equation(s['omml']))
                                row_texts.append(''.join(parts).strip())
                        row_text = ' | '.join(row_texts)
                        if row_text.strip():
                            segs = [{'kind': 'text', 'text': row_text,
                                     'bold': False, 'italic': False, 'rstyle': None}]
                            new_paragraphs.append(make_paragraph(
                                template_body, segs, 'Normal', red_id,
                                equation_template=equation_template))

        if verbose:
            print(f"[output] 생성 문단: {len(new_paragraphs)}")

        # 6. 루트 재구성
        for child in list(root):
            root.remove(child)
        for p in new_paragraphs:
            root.append(p)

        xml_str = ET.tostring(root, encoding='utf-8', xml_declaration=True)
        with open(section_path, 'wb') as f:
            f.write(xml_str)

        # 7. ZIP 재패키징 (mimetype은 첫 번째 + STORED)
        if os.path.exists(output_hwpx_path):
            os.remove(output_hwpx_path)

        with zipfile.ZipFile(output_hwpx_path, 'w') as zf:
            mimetype_info = zipfile.ZipInfo('mimetype')
            mimetype_info.compress_type = zipfile.ZIP_STORED
            with open(os.path.join(work_dir, 'mimetype'), 'rb') as f:
                zf.writestr(mimetype_info, f.read())
            for root_dir, dirs, files in os.walk(work_dir):
                for filename in files:
                    file_path = os.path.join(root_dir, filename)
                    arc_name = os.path.relpath(file_path, work_dir).replace(os.sep, '/')
                    if arc_name == 'mimetype':
                        continue
                    with open(file_path, 'rb') as f:
                        data = f.read()
                    ct = zipfile.ZIP_DEFLATED
                    if arc_name in src_infos:
                        ct = src_infos[arc_name].compress_type
                    zf.writestr(arc_name, data, compress_type=ct)

        if verbose:
            size = os.path.getsize(output_hwpx_path)
            print(f"\n✓ 출력: {output_hwpx_path} ({size:,} bytes)")

    finally:
        if cleanup_workdir and os.path.exists(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)


# ════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='docx → hwpx 변환 (한컴 스타일 시스템 활용)')
    parser.add_argument('input', help='입력 .docx 파일 경로')
    parser.add_argument('output', nargs='?', default=None,
                        help='출력 .hwpx 파일 경로 (기본: 입력 파일명.hwpx)')
    parser.add_argument('--skeleton', default=None,
                        help='참조 skeleton .hwpx 경로 (기본: 내장 default_skeleton.hwpx)')
    parser.add_argument('--quiet', action='store_true', help='진행 출력 억제')
    args = parser.parse_args()

    # 입력 검증
    if not os.path.exists(args.input):
        print(f"✗ 입력 파일을 찾을 수 없음: {args.input}", file=sys.stderr)
        sys.exit(1)

    # 출력 경로 결정
    if args.output is None:
        base = os.path.splitext(args.input)[0]
        args.output = base + '.hwpx'

    # skeleton 경로 결정
    if args.skeleton is None:
        # 스크립트 같은 디렉토리의 상위 assets/default_skeleton.hwpx
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # scripts/ 디렉토리에 있으면 상위로
        default = os.path.join(os.path.dirname(script_dir), 'assets', 'default_skeleton.hwpx')
        if not os.path.exists(default):
            # 같은 디렉토리에서도 찾기
            default = os.path.join(script_dir, 'default_skeleton.hwpx')
        if not os.path.exists(default):
            print(f"✗ skeleton 파일을 찾을 수 없음. --skeleton 옵션으로 지정해주세요.",
                  file=sys.stderr)
            sys.exit(1)
        args.skeleton = default

    if not os.path.exists(args.skeleton):
        print(f"✗ skeleton 파일을 찾을 수 없음: {args.skeleton}", file=sys.stderr)
        sys.exit(1)

    convert(args.input, args.skeleton, args.output, verbose=not args.quiet)


if __name__ == '__main__':
    main()
