# Changelog

## Unreleased — TRACE-DM-MIGRATION 0.1.0

- 8단계 scoped migration 방법론과 수신 완료·bilateral cutover·업무 재개 분리 계약을 추가했다.
- 기존 TRACE-DM lifecycle/core schema를 변경하지 않는 additive schema와 템플릿 6개를 추가했다.
- 승인 plan digest, scope/parent/shared owner, writer fencing 증적, relocation revision, 단일 cutover receipt·입력 digest, idempotent 재개·실패 이력·신규 데이터 보존 보상 계약을 검증한다.
- 제한형 USTAR/gzip 안전 해제와 archive/내부 manifest/per-file·복원 hash 검증, path/link/권한/압축폭탄 음성 테스트를 추가했다.
- 내부 상대 링크 descriptor의 target 문자열/ID/hash·containment·존재·순환·중간 링크 검토 template과 정적 validator를 추가했다. 실제 링크 복원은 미지원으로 명시했다.
- 실제 distributed writer fencing, NAS 복원, 큰/링크 포함 패키지 adapter, 프로젝트 이전·업무 활성화는 제공하지 않는다. 기존 enum 변경이나 운영 승격이 아니다.

## 1.1.0 — 2026-08-02

### Added

- TRACE Agent Harness Subframework `0.1.0`
- TRACE core, TRACE-DM와 TRACE-AH 통합 JSON Schema
- semantic validator와 fail-closed 네거티브 테스트
- deterministic Gate evaluator, input digest와 receipt digest
- TRACE-DM context selector와 provider-neutral Prompt Envelope
- typed model/tool/verifier Run Kernel
- read-only 병렬 batch와 serial mutation Tool Gateway
- append-only JSONL checkpoint와 resume
- bounded subagent scope, writer lease, cancellation과 conflict-safe merge
- GitHub Actions conformance workflow와 실행 예제
- TRACE-DM `0.2.0`의 선택적 객체 그룹 embedding과 governed semantic retrieval
- 그룹별 flat·graph·provider-managed index 선택 계약
- 저장소 Markdown 전체의 TRACE-DM 공통 metadata partition manifest와 drift 검증

### Changed

- TRACE core 문서 버전을 `1.1.0`으로 갱신했다.
- `gate-decision.yaml`은 사람이 `status`를 입력하지 않고 evaluator가 facts에서 계산하도록 변경했다.
- TRACE-DM에서 검증된 Summary의 context projection 역할과 원본 확대 조건을 명확히 했다.

### Compatibility note

1.0.0 Gate Decision을 이전할 때 기존 status는 historical evidence로 보존한다. `thresholds`를 `required`로 옮기고 evaluator ID·version을 지정한 뒤 원본 facts에서 새 receipt를 생성한다.
