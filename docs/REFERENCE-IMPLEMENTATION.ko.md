# TRACE 참조 구현 사용 가이드

## 1. 목적과 범위

이 저장소는 규격 문서뿐 아니라 TRACE 계약을 검증하고 agent 실행 경계를 시험할 수 있는 최소 참조 구현을 제공한다. 실제 LLM provider, 원격 shell, cloud credential과 운영 데이터는 포함하지 않는다.

참조 구현의 목적은 다음 두 가지다.

1. TRACE status가 사람이 적은 주장에 머무르지 않도록 schema·semantic·Gate 검증을 자동화한다.
2. 제품별 agent runtime이 공통으로 따라야 할 tool, context, state와 subagent 계약의 실행 가능한 기준을 제공한다.

## 2. 설치와 검증

Python 3.11 이상이 필요하다.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/validate_templates.py
.venv/bin/python -m pytest -q
```

CI는 같은 검증을 Python 3.12에서 수행한다.

## 3. 구성요소

| 경로 | 역할 |
|---|---|
| `schemas/trace-gate.schema.json` | core, TRACE-DM, TRACE-AH 계약 schema |
| `schemas/document-migration.schema.json` | 기존 lifecycle과 분리된 선택적 TRACE-DM-MIGRATION 0.1.0 계약 |
| `src/trace_gate/migration.py` | scoped plan, 단계·수신/활성화 분리, evidence/cutover digest, 재개·보상 semantic |
| `src/trace_gate/migration_archive.py` | strict USTAR/gzip digest·파일 검증 및 새 private staging 안전 해제 |
| `src/trace_gate/migration_links.py` | 내부 상대 링크 descriptor·target graph·cycle 검증 |
| `src/trace_gate/migration_link_restore.py` | opt-in private staging의 마지막 상대 symlink 복원·전수 사후 검증 |
| `src/trace_gate/migration_stream.py` | 별도 bounded raw chunk-set pack/restore·part/whole hash·명시적 resume |
| `src/trace_gate/migration_io.py` | 공통 no-follow/exclusive I/O·private claim·local stage lock·directory digest |
| `src/trace_gate/validation.py` | 중복 key, JSON Schema와 semantic validator |
| `src/trace_gate/gates.py` | 결정적 Gate evaluator와 receipt digest |
| `src/trace_gate/context.py` | 목적 기반 context selection과 prompt envelope |
| `src/trace_gate/semantic.py` | 선택 그룹 semantic candidate의 provenance·권한·freshness 검증 |
| `docs/managed-document-manifest.yaml` | 모든 Markdown의 공통 metadata partition manifest |
| `scripts/update_document_manifest.py` | 문서 revision·SHA-256·size·freshness 갱신 |
| `src/trace_gate/runtime.py` | bounded model/tool/verifier loop와 checkpoint |
| `src/trace_gate/multi_agent.py` | writer lease, cancellation과 merge conflict 검사 |
| `templates/` | 구현자가 채워야 할 계약 예제 |
| `tests/` | 정상·네거티브·실패·resume·권한 테스트 |

## 4. Gate 평가

```bash
python scripts/evaluate_gate.py \
  templates/gate-decision.yaml \
  examples/gate-facts.yaml
```

Evaluator는 다음을 보장한다.

- requirement key를 정렬해 결정적으로 평가
- evidence 누락 시 `blocked`
- threshold 미달 시 `fail`
- 모두 충족할 때만 `pass`
- policy와 facts의 canonical input digest
- evaluator ID와 version
- receipt 자체 digest

`evaluated_at`이 달라지면 receipt digest는 달라지는 것이 정상이다. 동일 시각, policy와 facts로 replay하면 같은 receipt가 생성된다.

## 5. Agent Runtime 연결

제품 adapter는 다음 interface를 구현한다.

- model adapter: message list를 받아 typed action을 반환
- tool handler: schema가 검증된 argument를 받아 structured output 반환
- verifier: 후보 answer를 받아 `passed`와 reason 반환
- authorization resolver: Receipt reference의 진위·범위·만료 확인
- durable event store: append와 ordered replay

참조 `RunKernel`은 단일 프로세스 구현이다. 운영 환경에서는 durable database 또는 object ledger, 분산 lease와 deadline-aware executor로 교체할 수 있다. 교체 구현도 event 순서와 digest, permission fail-closed 의미를 유지해야 한다.

## 6. Context 연결

`select_context`는 다음 조건을 모두 만족한 artifact만 선택한다.

- ReadProfile이 declared purpose와 operation을 허용
- validation status가 `pass`
- freshness가 `current`
- sensitivity가 intent와 profile의 낮은 상한 이하
- 허용 tier가 존재
- context byte budget 안에 포함

제외된 artifact와 이유도 Context Pack에 남는다. 자동으로 더 넓은 tier 또는 sensitivity로 fallback하지 않는다.

## 7. Multi-agent 연결

`AgentCoordinator`는 선택적 예제다. 운영 구현은 다음을 추가해야 한다.

- durable 또는 분산 writer lease
- 인증된 child identity
- parent cancellation delivery acknowledgement
- child Context Pack과 Tool Policy의 축소 상속 증명
- merge 이후 독립 verifier
- lease 만료 중 수행된 mutation의 reconciliation

동일 artifact path에 다른 digest가 제출되면 merge 결과는 `blocked`다. LLM judge가 임의로 하나를 선택하게 해서는 안 된다.

## 7.1 선택적 Semantic Retrieval

`select_semantic_candidates`는 embedding provider나 vector database를 구현하지 않는다. 외부 검색기가 반환한 candidate를 다음 순서로 검증한다.

- Access Intent가 `semantic` 또는 `hybrid` mode와 group을 요청했는지 확인
- ReadProfile과 EmbeddingProfile이 같은 group·purpose를 허용하는지 확인
- EmbeddingManifest의 model contract, validation과 freshness 확인
- candidate의 artifact revision과 chunk가 index manifest에 존재하는지 확인
- 현재 DocumentManifest의 revision, sensitivity, secret와 group membership 재검증
- score threshold와 candidate budget 적용

결과는 content가 아니라 artifact·revision·chunk identity와 provenance만 반환한다. 이후 일반 `select_context`가 허용 tier의 실제 문맥을 선택한다. `exact` mode는 embedding 구성요소 없이 그대로 동작한다.

## 8. 알려진 제한

- cryptographic signature는 필드와 lifecycle만 정의하며 signer를 포함하지 않는다.
- JSON Schema는 모든 비즈니스 Gate 의미를 알 수 없으므로 domain validator가 필요하다.
- 단일 프로세스 mutation lock은 분산 single-writer를 보장하지 않는다.
- Python handler timeout은 실행 후 측정하는 참조 동작이며, 운영 executor는 실제 강제 timeout과 process 격리를 제공해야 한다.
- model adapter의 prompt token 계산은 provider adapter 책임이다. TRACE 공통층은 byte budget과 provenance를 보존한다.
- 참조 구현은 embedding 생성이나 ANN index를 제공하지 않는다. model과 index backend는 group profile 계약을 지키는 adapter로 연결한다.

## 8.1 관리 문서 Metadata

게시용 Markdown에는 반복적인 front matter를 삽입하지 않는다. 대신 TRACE-DM 8항이 허용하는 partition manifest에 문서별 Identity, Scope, Governance, Time, Provenance, Integrity, Validation, Lineage, Storage, Freshness와 Access를 등록한다.

Validator는 repository에서 Markdown을 직접 발견해 manifest coverage와 content-derived revision을 대조한다. Manifest 자체는 자기 hash를 포함하지 않으며 등록한 문서만 SHA-256과 size를 가진다.

이 제한은 권한을 넓혀 우회하지 않고 구현별 Evidence와 Unknown으로 기록한다.

## 9. 선택적 범위별 이전

[8단계 이전 방법론](subframeworks/TRACE-DM-MIGRATION.ko.md)은 기본 계약 6개와 opt-in/참조 계약 5개를 제공한다(전체 template 24개). 기존 document lifecycle/core schema는 그대로 유지한다. `received_complete`는 업무 활성화와 분리하며, 단일 bilateral cutover receipt가 있는 `completed` 상태도 별도 업무 재개 승인이 있어야 한다.

기본 link policy는 descriptor-only다. 별도 승인 `document-migration-link-restore.yaml`과 `restore_relative_links`를 선택하면 일반 파일 hash를 전수 대조한 뒤 상대 symlink를 마지막에 재구성하고 전체 tree/target 문자열을 재검증한다. stage flock은 regular restore와 공유하고 링크 생성 후 regular writes는 거부한다. 기본 archive의 링크 거부를 끄거나 중첩 archive/dereference로 우회하지 않는다. same-UID/root 악성 writer는 이 로컬 통제의 보장 범위 밖이다.

raw target의 `..`로 사라질 중간 구성요소도 정규화 전에 검증한다. 중간 symlink·missing/non-directory는 거부하고 정상 부모 경로와 terminal chain만 허용한다. runtime은 동일 규칙과 no-follow directory 검사를 적용하며 terminal readlink·최종 target hash, 생성 후 OS 대상의 device/inode/kind를 대조한다. metadata-only follow는 payload 쓰기 경로가 아니며 불일치는 완료를 차단한다.

`transition`은 검증된 checkpoint의 순수 비교·재개 모델이고 저장소 CAS가 아니다. failure checkpoint 상승/PASS 증거 주입을 거부하며 명시 승인된 abort는 writer-hold 상태로 종료한다. 신뢰된 current 상태는 caller 책임이다. `extract_verified`는 trusted receipt의 archive/내부 manifest SHA와 새 staging을 요구하고 제한형 USTAR/gzip의 모든 링크·특수파일·확장 헤더를 거부한다. 기본 archive 256MiB 제한은 변경하지 않았다.

대형 artifact에는 기본 disabled streaming policy를 별도 승인해 `pack_chunk_set`/`restore_chunk_set`을 사용한다. raw part 상한 256MiB, buffer 상한 1MiB, metadata/item·aggregate/file·temp/disk 한도를 검증한다. 단일 파일 offset·part/whole hash, inventory closure, 부분 실패 보존과 동일 정책의 명시적 resume를 제공한다. 링크가 남으면 `regular_files_verified`일 뿐 전체 acceptance가 아니다. owner/ACL/xattr/실행권한/sparse extent와 엔진별 의존성 복원은 미지원이며 필수이면 차단한다.

`reference_only`는 외부 payload 대신 참조 metadata를 이전하는 명시 승인 모드다. 외부 canonical owner·위치·권한·고정 version/hash·지속성·dependency/rollback binding과 별도 reference evidence를 검증하고 payload 전달 완료로 오인하지 않는다. 물리 이전과 package-set/reference manifest digest를 섞거나 자동 모드 전환할 수 없다. 실제 migration adapter는 사용자 실행 승인·확정된 방법론 commit·외부 writer/backup/restore/dependency 증적을 결합해야 한다. synthetic fixture만으로 운영 분산 fencing·외부 저장소·대형 프로젝트 복원을 검증했다고 주장하지 않는다.
