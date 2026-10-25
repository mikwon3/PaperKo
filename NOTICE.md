# PaperKo

Copyright 2026 Minho Kwon (@mikwon3)

PaperKo 는 **GNU Affero General Public License v3.0 (AGPL-3.0)** 으로 배포합니다.
전문은 [LICENSE](LICENSE) 에 있습니다.

PDF 를 읽고 쓰는 핵심 의존성 **PyMuPDF 가 AGPL-3.0** 이므로, 그와 결합한 PaperKo
전체가 AGPL-3.0 을 따릅니다. 이에 따라 앱 전체 소스를 같은 조건으로 공개합니다.

# 제3자 구성요소

PaperKo 가 함께 담거나 내려받아 쓰는 남의 것들이다.

아래 판본·저작권은 **설치된 꾸러미와 파일에서 직접 읽은 값**이다. "확인 필요" 로
적힌 것은 이 저장소 안에 근거가 없어 확인하지 못한 것이다 — 추측으로 적지 않았다.

## PyMuPDF — 라이선스의 근거

```
pymupdf 1.28.0
License: Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License
```

PDF 를 읽고 쓰는 핵심 의존성이고, **AGPL 3.0 또는 Artifex 상용 라이선스**다.
AGPL 은 결합 저작물 전체의 소스를 같은 조건으로 공개할 것을 요구한다. PaperKo 는
소스를 공개하고 **AGPL-3.0** 으로 배포하여 이 조건을 충족한다. (소스를 비공개한
상용 배포를 원하면 Artifex 상용 라이선스가 필요하다. 법률 자문이 아니다.)

## 파이썬 의존성

| 꾸러미 | 판본 | 라이선스 (꾸러미 표기 그대로) |
|---|---|---|
| PyMuPDF | 1.28.0 | **AGPL 3.0 또는 Artifex 상용** |
| httpx | 0.28.1 | BSD-3-Clause |
| pysbd | 0.3.4 | MIT |
| python-docx | 1.2.0 | MIT |
| latex2mathml | — | MIT |
| mathml2omml | — | MIT |

수식 인식(이미지→LaTeX) 폴백으로 담기는 것들(비전 LLM이 1순위, 아래는 오프라인 대비):

| 꾸러미 | 판본 | 라이선스 |
|---|---|---|
| rapid_latex_ocr | — | Apache-2.0 |
| onnxruntime | — | MIT |
| Pillow | — | MIT-CMU (HPND) |
| requests | — | Apache-2.0 |
| RapidLaTeXOCR ONNX 모델(pix2tex 유래) | v0.0.0 | MIT (pix2tex/LaTeX-OCR) |

선택 의존성: `pyahocorasick`(fast), `matplotlib`(math) — 기본으로는 담기지 않는다.

## 저장소에 담겨 있는 것

### 글꼴 — `engine-py/translate_engine/fonts/`

| 글꼴 | 판본 | 저작권 | 라이선스 |
|---|---|---|---|
| NanumGothic | 3.020 | © 2011 NHN Corporation | SIL OFL 1.1 (`OFL.txt`) |
| NanumMyeongjo | 2.032 | © 2010 NHN Corporation | SIL OFL 1.1 (`OFL.txt`) |
| Noto Sans JP | 2.004 | © 2014–2021 Adobe (RFN 'Source') | SIL OFL 1.1 (`Noto-OFL.txt`) |
| Noto Sans SC | 2.004 | © 2014–2021 Adobe (RFN 'Source') | SIL OFL 1.1 (`Noto-OFL.txt`) |

판본과 저작권은 글꼴 파일의 `name` 표에서 읽은 값이다.

폴더의 `OFL.txt` 는 NHN(나눔) 저작권 머리말만 담고 있어서 Noto 두 벌의 표기가
빠져 있었다. `Noto-OFL.txt` 를 새로 두어 채웠다. 예약 이름이 `'Noto'` 가 아니라
`'Source'` 인 것은 Noto Sans CJK 가 Adobe Source Han Sans 에서 파생했기
때문이다 — 글꼴 파일에 적힌 문구를 그대로 옮겼다.

### hwpx 변환기 — `engine-py/translate_engine/hwpx/`

`docx-to-hwpx-conversion` 스킬에서 가져왔다. **원본에 저작권·라이선스 표기가
없다.** 자세한 것은
[engine-py/translate_engine/hwpx/README.md](engine-py/translate_engine/hwpx/README.md).

`assets/default_skeleton.hwpx` 는 한컴 오피스가 만든 문서를 스타일 골격으로
쓰는 것이다. 이것도 함께 확인이 필요하다.

### OCR 학습 데이터 — `engine-py/translate_engine/tessdata/`

`eng.traineddata` (4.1MB). Tesseract OCR 프로젝트의 데이터로 보이나 **출처와
라이선스를 이 저장소 안에서 확인할 수 없다 — 확인 필요.** 받아 온 곳과 판본을
적어 두고 라이선스 사본을 함께 두어야 한다.

## 설치본에 함께 담기는 것

`desktop/paperko/resources/` 아래에 있고 설치본에 그대로 들어간다.

| 구성요소 | 판본 | 라이선스 |
|---|---|---|
| llama.cpp (`llama-server`, `libggml-*`) | 0.1.2-dev, build 10488, commit `9d77fa172` | **확인 필요** — 라이선스 사본이 함께 담겨 있지 않다 |
| Python | 3.12.13 | PSF License |

모델 파일(`*.gguf`)은 담기지 않는다. 사용자가 따로 받는다 — 모델마다 라이선스가
다르므로 앱이 대신 배포하지 않는 편이 낫다.

## 소스에서 가져다 쓰는 것

`desktop/paperko/go.mod` 와 `desktop/paperko/frontend/package.json` 에 적힌
것들 — Wails, React 등. 각 라이선스는 그 꾸러미에 들어 있다.

## 아직 정하지 않은 것

이 저장소 자체의 라이선스. **PyMuPDF 의 AGPL 을 먼저 정리해야** 고를 수 있다.
