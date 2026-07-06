# Velvet Knowledge Hub — operator guide
# VKH 운영 가이드

_Audience: non-technical operator. No coding required._
_대상: 비개발자 운영 담당자. 코딩 불필요._

---

## What is Velvet Knowledge Hub? / VKH란 무엇인가?

Velvet Knowledge Hub(VKH)는 뉴질랜드 녹용 수출업체를 위한 한국 시장 정보 대시보드입니다.
구글 시트에 저장된 데이터를 바탕으로 웹사이트가 자동 생성됩니다.

- **라이브 사이트:** https://wkdjk.github.io/velvet-knowledge-hub/
- **마스터 데이터 시트:** [VKH_Data Google Sheet](https://docs.google.com/spreadsheets/d/1idbPiaK_Scd8znktn2cPutWP5Lg4azo1XBfXNyt5K2U)

---

## How data flows in / 데이터 흐름

```
구글 드라이브 폴더에 파일 저장
        ↓
마스터 시트에서 "VKH → Refresh data from Drive" 클릭
        ↓
약 3분 후 — 라이브 사이트 자동 갱신 완료
```

그리고 매주 월요일 오전 11시(KST)에 자동 업데이트가 실행됩니다.
이 업데이트는 뉴스 수집과 대시보드 재빌드를 자동으로 수행하며, 아무것도 하지 않아도 됩니다.

---

## Data sources and Drive folders / 데이터 출처와 구글 드라이브 폴더

VKH는 5개의 구글 드라이브 폴더에서 데이터를 가져옵니다.

| 폴더 이름 | 내용 | 업데이트 주기 | 드라이브 링크 |
|----------|------|-------------|--------------|
| `nz` | Stats NZ 뉴질랜드 수출 CSV | 월 1회 | [열기](https://drive.google.com/drive/folders/10jlxeYND28jbiI49XXi3H3NOnbttOK0Q) |
| `qia` | 한국 검역정보원(QIA) 검역 데이터 | 필요 시 | [열기](https://drive.google.com/drive/folders/1L2z3xYlpvkHzlO4JrpYfJFpGltzUfa58) |
| `mfds_records` | 식약처(MFDS) 수입식품 정보마루 CSV | 분기 1회 | [열기](https://drive.google.com/drive/folders/1xZI1MFMMVS09OUdXSvsuYjYQwi2OPXCg) |
| `mfds_price` | 식약처 연간 가격 순위 | 연 1회 | [열기](https://drive.google.com/drive/folders/1UUf55PlJjbCPzVxpcHSERnjC5Ah0Za3g) |
| `kstat` | 관세청(KSTAT) 수출입 통계 CSV | 월 1회 | [열기](https://drive.google.com/drive/folders/1ebc4WfgBh-egMQOTX0fujdH6EJpBq-BI) |

각 폴더 안에 있는 `HOW_TO_UPDATE.txt` 파일에 해당 폴더의 파일 형식과 다운로드 방법이 설명돼 있습니다. 이 파일은 시스템이 건너뜁니다 — 덮어쓰거나 삭제하지 마십시오.

---

## How to update data / 데이터 업데이트 방법

### Option A: Drive folder upload + button (recommended) / A안: 드라이브 업로드 후 버튼 클릭 (권장)

1. 해당 기관 웹사이트에서 최신 파일을 다운로드합니다.
2. 위 표의 해당 구글 드라이브 폴더에 파일을 업로드합니다.
   - 기존 파일을 삭제하지 않아도 됩니다. 시스템이 중복을 자동 처리합니다.
3. [마스터 데이터 시트](https://docs.google.com/spreadsheets/d/1idbPiaK_Scd8znktn2cPutWP5Lg4azo1XBfXNyt5K2U)를 엽니다.
4. 상단 메뉴에서 **VKH → Refresh data from Drive** 를 클릭합니다.
5. 화면 하단에 "Update started" 알림이 나타납니다.
6. 약 3분 후 라이브 사이트를 새로 고침하면 데이터가 반영됩니다.

### Option B: Direct sheet editing / B안: 시트 직접 편집

시트에서 직접 데이터를 수정하거나 추가할 수도 있습니다.

**편집 가능한 탭:**

| 탭 이름 | 내용 | 편집 방법 |
|---------|------|----------|
| `VTW_Trade_Monthly` | 무역 통계 (NZ 수출, QIA 검역) | 마지막 행 아래에 새 행 추가. 1행(헤더) 절대 수정 금지 |
| `VFI_Import_Records` | 식약처 수입 기록 | 마지막 행 아래에 새 행 추가. 중복은 자동 처리됨 |
| `VFI_Price_Annual` | 연간 가격 순위 | 마지막 행 아래에 새 행 추가 |
| `KVN_Articles` | 뉴스 기사 | 카테고리 또는 요약 수정 가능 |

**편집 후 대시보드 반영 방법:**
- 상단 메뉴 **VKH → Rebuild dashboard only** 클릭
- 또는 다음 월요일 자동 업데이트까지 대기 (최대 7일)

**절대 수정하지 말아야 할 탭:**
- 모든 탭의 **1행(헤더 행)** — 열 구조가 바뀌면 시스템이 오작동합니다.
- `_keywords` 탭 — 키워드 수정은 기술 담당자에게 요청하십시오.

---

## Automatic weekly update / 자동 주간 업데이트

매주 **월요일 오전 11시 (KST)** 에 자동으로 실행되는 작업:

1. 네이버 뉴스에서 최신 녹용 관련 기사 수집
2. AI가 기사를 카테고리별로 분류 및 영문 요약 생성
3. 대시보드 재빌드 및 라이브 사이트 자동 갱신

이 작업은 아무도 개입하지 않아도 자동으로 완료됩니다.
단, 구글 드라이브 폴더의 파일 업데이트는 자동으로 처리되지 않습니다 — 위의 A안을 사용하십시오.

---

## When the error alert email arrives / 오류 알림 이메일이 왔을 때

자동 업데이트에 문제가 생기면 이메일 알림이 발송됩니다.

이메일에 포함된 내용:
- 어떤 단계에서 오류가 발생했는지 확인하는 링크
- 단계별 점검 방법 (한국어로 작성됨)

**라이브 사이트는 이전 업데이트 기준으로 계속 운영됩니다.** 오류가 발생해도 사이트는 즉시 중단되지 않습니다.

해결이 어려운 경우: 이메일을 기술 담당자에게 전달하십시오.

---

## If automation stops working / 자동화가 멈췄을 때 (수동 대체 절차)

| 자동화 | 멈췄을 때 수동 대체 절차 |
|--------|------------------------|
| 대시보드 빌드 (매주 월요일) | 마스터 시트에서 **VKH → Rebuild dashboard only** 클릭 — 언제든 수동 실행 가능, 사이트는 이전 버전으로 계속 운영됨 |
| 뉴스 수집 (매주 월요일) | 네이버 뉴스에서 직접 검색 → `KVN_Articles` 탭 마지막 행 아래에 제목/링크/직접 요약 수동 입력 후 Rebuild 실행 |
| 백업 (매주 일요일) | 마스터 시트를 **파일 → 사본 만들기**로 로컬 또는 별도 드라이브 폴더에 수동 저장 |

## Monthly routine / 월간 루틴 (소요시간 포함)

| 주기 | 작업 | 소요시간 |
|------|------|---------|
| 매주 월요일 (자동) | 뉴스 수집 + 대시보드 재빌드 | 0분 (자동) |
| 매주 일요일 (자동) | 시트 백업 | 0분 (자동) |
| 월 1회 | Stats NZ / KSTAT CSV 드라이브 업로드 + Refresh 버튼 | 약 3분 |
| 분기 1회 | MFDS 수입식품 정보마루 CSV 업로드 | 약 3분 |
| 연 1회 | MFDS 연간 가격 순위 업로드 | 약 3분 |
| 필요 시 | QIA 검역 데이터 업로드 | 약 3분 |

이 표 밖의 작업은 설계 위반입니다 — 새로운 수동 작업이 필요해지면 표에 추가하는 대신 자동화하거나 없앨 방법을 먼저 검토하십시오.

## News copyright rule / 뉴스 저작권 규칙

VKH는 뉴스 기사 전문을 저장하지 않습니다. 저장하는 항목은 제목, 링크, 그리고 AI가 생성한 자체 영문 요약(`english_summary`)뿐입니다 (`KVN_Articles` 탭 참고). 기사 본문을 긁어오거나 저장하는 기능은 추가하지 마십시오.

## Data provenance / 데이터 출처 추적

각 무역·수입 데이터 행에는 `notes` 컬럼에 `source: 파일명` 형식으로 어느 파일에서 왔는지 기록됩니다 (`ingest_nz_export.py`, `ingest_qia.py`, `ingest_kstat.py`). 별도 등록부 탭은 없습니다 — 파일 유실·중복 사고가 실제로 발생하면 그때 신설을 검토합니다.

## Technical contact / 기술 담당자

VKH 기술 문의: 기술 담당자에게 오류 이메일 전체를 전달하십시오.
코드 수정이 필요한 경우 VS Code + Claude Code 환경에서 작업이 이루어집니다.

---

## Quick reference / 빠른 참조

| 상황 | 해야 할 일 |
|------|----------|
| 새 데이터 파일이 생겼다 | 드라이브 폴더에 업로드 → 시트 버튼 클릭 |
| 시트에서 직접 수정했다 | 시트 버튼 → Rebuild dashboard only |
| 아무것도 안 해도 되는 날 | 월요일 자동 업데이트가 처리함 |
| 오류 이메일이 왔다 | 이메일 링크 클릭해서 확인 → 해결 안 되면 기술 담당자에게 전달 |
| 사이트가 안 보인다 | https://wkdjk.github.io/velvet-knowledge-hub/ 에서 새로 고침 |
