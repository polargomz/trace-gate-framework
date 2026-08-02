# Changelog

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
