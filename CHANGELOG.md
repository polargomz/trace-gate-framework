# Changelog

## Unreleased — TRACE-DM-MIGRATION 0.1.0

- 후속 호환성 교정: migration plan/package의 payload revision과 relocation old/new payload revision 네 필드만 최소 길이 3→1로 바꿔 `r1`/`r5` 원문을 보존한다. 기존 link/stream revision과 맞추며 다른 ID·management revision·core schema는 변경하지 않는다. 네 필드의 허용 alphabet은 유지하고 정규식의 정확한 끝 검사를 보강해 trailing newline도 거부한다. JSON/YAML·synthetic archive roundtrip, 내용/hash 보존·invalid revision·다른 ID 제약 회귀를 추가했다. 다른 core 소비자 전체의 짧은 revision 호환성을 뜻하지 않는다.
- 8단계 scoped migration 방법론과 수신 완료·bilateral cutover·업무 재개 분리 계약을 추가했다.
- 기존 TRACE-DM lifecycle/core schema를 변경하지 않는 additive schema와 템플릿 11개(전체 24개)를 추가했다.
- 승인 plan digest, scope/parent/shared owner, writer fencing 증적, relocation revision, 단일 cutover receipt·입력 digest, idempotent 재개·실패 이력·신규 데이터 보존 보상 계약을 검증한다.
- 제한형 USTAR/gzip 안전 해제와 archive/내부 manifest/per-file·복원 hash 검증, path/link/권한/압축폭탄 음성 테스트를 추가했다.
- failure checkpoint/PASS 주입을 통한 단계 우회와 abort 전이 누락을 재현하고 회귀 테스트로 교정했다. 실패/abort는 직전 checkpoint를 유지하며 abort는 명시 승인·writer-hold 증적을 요구하는 terminal 상태다.
- 상대 링크 descriptor 정적 검증과 opt-in 실제 복원 adapter를 분리했다. 원 target 문자열/ID/hash, containment·cycle·target 검증과 일반 파일 후 마지막 링크 생성, 후속 regular restore 차단을 검사한다. 기본 archive 링크 거부는 유지한다.
- raw `alias/../file`의 lexical/OS 해석 불일치를 실제 synthetic symlink로 재현·교정했다. 정규화로 제거되는 경로까지 중간 symlink·missing/non-directory를 거부하고 정상 `../file`·terminal chain은 유지한다. runtime raw no-follow 검사와 생성/사후/replay의 readlink·OS target identity 대조, hash 불일치·완료 receipt 차단 회귀 테스트를 추가했다.
- 별도 raw chunk-set 형식에 bounded streaming pack/restore, 단일 대형 파일 분할, offset·part/whole hash·exact inventory, 예산·디스크 경계, 실패 보존과 명시적 동일 정책 resume를 추가했다. 기본 archive 256MiB 한도는 유지한다.
- 물리 이전과 reference-only 계획·증적·digest·완료 의미를 구분했다. 외부 payload owner/위치/version/hash/권한/지속성·dependency/rollback을 결합하며 reference-only로 자동 축소하지 않는다.
- 공개 예제를 외부 저장소·개발/실행 엔진 의존성·상대 참조·대형 artifact로 일반화했다. 실제 distributed writer fencing, 외부 저장소 복원·엔진 검증·프로젝트 이전·업무 활성화는 제공하지 않는다. 작은 synthetic fixture PASS는 실제 대형 자료의 복원 증명이 아니다.

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
