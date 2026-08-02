# TRACE Gate Framework

추적 가능하고 안전한 소프트웨어 변경·마이그레이션을 위한 증거 기반 실행 프레임워크입니다.

> 속도는 Gate를 생략해서 얻는 것이 아니라, 경계를 명확히 하고 검증과 복구를 자동화해서 얻습니다.

## TRACE

| 축 | 의미 | 핵심 질문 |
|---|---|---|
| **T — Trace** | 요청과 결정의 추적성 | 왜 이 작업을 하며 누가 무엇을 승인했는가? |
| **R — Restrict** | 권한과 영향 범위 제한 | 무엇을 바꿀 수 있고 절대 바꾸면 안 되는가? |
| **A — Assemble** | 되돌릴 수 있는 능력 단위 조립 | 큰 전환을 어떤 작은 기능 조각으로 나눌 것인가? |
| **C — Confirm** | 테스트와 증적으로 사실 확인 | 성공했다는 주장을 어떤 근거로 증명하는가? |
| **E — Elevate** | Gate를 통한 단계적 승격 | 언제 shadow를 primary 또는 official로 승격할 것인가? |

```mermaid
flowchart LR
    T["T · Trace<br/>요청·결정·Ticket"] --> R["R · Restrict<br/>권한·writer·금지 경로"]
    R --> A["A · Assemble<br/>작은 능력 단위 구현"]
    A --> C["C · Confirm<br/>테스트·해시·원격 증적"]
    C --> E["E · Elevate<br/>Shadow → Primary → Official"]
    E --> O["Observe<br/>KPI·Incident·회귀"]
    O --> T
```

## 문서

- [TRACE Gate Framework 전체 문서](docs/TRACE-GATE-FRAMEWORK.ko.md)
- [TRACE Document Management Subframework](docs/subframeworks/TRACE-DOCUMENT-MANAGEMENT.ko.md): 문서 정본·계보와 AI/LLM 목적 기반 사전 접근 선별
- [기여 가이드](CONTRIBUTING.md)

## 바로 사용하기

`templates/`에서 다음 파일을 프로젝트에 복사해 시작할 수 있습니다.

- `request-receipt.yaml`: 작업 전 요청·권한 Receipt
- `ticket.yaml`: 작고 검증 가능한 구현 단위
- `evidence-manifest.yaml`: commit·image·run·hash 증거 사슬
- `gate-decision.yaml`: KPI와 승격 판정
- `HANDOFF.md`: 다음 작업자·AI 세션을 위한 상태 인계
- `document-access-intent.yaml`: AI/LLM 접근 목적·대상·민감도 선언
- `document-read-profile.yaml`: 목적별 최소 tier·context budget·확대 조건
- `document-manifest.yaml`: 문서 revision·무결성·계보·freshness 계약
- `document-gate-receipt.yaml`: 사전 접근 심사와 문서 Gate 판정 증적

가장 작은 도입 단위는 다음 네 가지입니다.

1. Receipt before effect
2. 허용·금지 경계
3. 기계적으로 판정 가능한 Gate
4. 재현 가능한 Evidence

AI/LLM이 문서에 접근하는 프로젝트는 TRACE-DM의 `Access Intent → Pre-access Review → ReadProfile → 최소 Context Pack → GateReceipt` 흐름을 함께 적용할 수 있습니다. 접근 도구가 아니라 선언된 목적에 따라 Summary, Overview, 선택 Records, full history와 raw의 허용 범위를 결정합니다.

## 적용 대상

- 일반 소프트웨어 개발과 배포
- 서버·클라우드·아키텍처 이전
- 데이터 수집·정규화·승격 파이프라인
- 운영 자동화와 AI 에이전트 기반 개발
- 감사 가능성과 데이터 무결성이 중요한 프로젝트

## 버전

현재 문서 버전은 `1.0.0`입니다.

## 템플릿 검증

Python 기반 검증은 Python 3.9 이상의 프로젝트별 가상환경에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/validate_templates.py
```

검증기는 모든 YAML의 문법과 중복 key를 확인하고 TRACE-DM 템플릿의 필수 top-level field와 `subframework_id`를 검사합니다. `.venv`는 저장소에 포함하지 않습니다.

## 라이선스

Copyright © 2026 polargomz.

별도로 표시하지 않은 이 저장소의 문서, 도표와 템플릿은
[Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)에 따라 제공됩니다.

누구나 상업적 목적을 포함해 공유·수정·재배포할 수 있습니다. 재배포할 때는 적절한 저작자 표시와 라이선스 링크를 제공하고, 변경했다면 그 사실을 밝혀야 합니다. 자세한 조건은 [`LICENSE`](LICENSE)를 확인하세요.
