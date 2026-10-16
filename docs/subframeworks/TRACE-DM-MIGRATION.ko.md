# TRACE-DM 범위별 이전 방법론

- Framework ID: `TRACE-DM-MIGRATION`
- Contract version: `0.1.0`
- Compatible parent: TRACE-DM `0.2.0`, TRACE core `1.1.x`
- 상태: additive reference contract. 기존 문서 lifecycle과 별도로 선택 적용한다.
- 저작자: D. JEONG. 라이선스: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## 목적과 핵심 의미

이 방법론은 특정 프로젝트의 문서·코드·자료를 새 프로젝트로 이전할 때, 정본과 이력, 복구 능력, 업무 쓰기 권한을 잃지 않는 순서를 정의한다. 단순 파일 복사나 클라우드 동기화 완료를 정본 이전으로 해석하지 않는다. 실행자는 상위 TRACE의 사용자 승인과 경계를 먼저 확인해야 한다.

이전 transaction, 개별 문서 lifecycle, 업무 쓰기 권한, canonical owner는 서로 다른 상태다. 기존 `draft`, `active`, `archived` 등의 enum에 `migrating`이나 `completed`를 끼워 넣지 않는다. 변경이 필요한 관리정보는 새 revision으로 추가하고, 과거 기록은 그대로 보존한다.

특히 **7단계의 완료는 수신·검증 완료이지 정본 전환이나 업무 재개가 아니다.** 목적지 화면에는 “이전 완료(수신 검증 완료·정본 전환 대기)”라고 표시한다. 원본과 목적지의 업무 쓰기는 7→8단계 사이에도 차단한다. 양측 증적을 결합한 단일 cutover receipt와 전체 완료 검증이 끝난 뒤에도, 업무 재개 승인이 없다면 목적지는 비활성 상태를 유지한다.

## 1. 이전 범위를 설정한다

`document-migration-plan.yaml`에서 원본·목적지 partition, 정확한 host와 project, 기준 root, 포함·제외 경로, artifact ID/revision/hash, 외부 의존성, 공유 canonical owner와 parent closure를 고정한다. 실행자가 다른 기기나 하위 폴더를 편의상 프로젝트 root로 대신 정하지 않는다. 파일 경로의 prefix만으로 자료의 소유권을 추론하지 않는다.

ignored/untracked 파일, 외부 저장소, 개발·실행 엔진 리소스, 환경 설정, 컨테이너 bind mount도 범위 검토에 포함한다. 외부 원본과 공유 원장은 가져올지, 검증된 scoped export만 만들지, 원 owner를 참조할지 결정한다. 공유 canonical record 자체를 이전 대상과 중복 등록하지 않는다.

계획의 `migration_mode`는 기본 `physical_relocation` 또는 명시 승인된 `reference_only`다. 물리 이전은 실제 payload 패키징·전달·복원·의존성 검증을 요구한다. 참조만 이전은 `document-migration-reference-plan.yaml`처럼 **참조 관리 artifact만** 새 partition으로 이전한다. 외부 payload의 stable ID·canonical owner·보존 위치·접근 정책·고정 version/hash·지속성 정책·dependency/rollback 증적을 reference manifest에 결합하고 외부 parent closure를 유지한다. 원데이터의 소유권·보관 위치는 바꾸지 않는다. 대형 artifact라는 이유로 물리 이전을 자동으로 참조-only로 축소하지 않는다. 모드 변경은 새 계획 digest와 재승인 대상이다.

전체 계획의 canonical JSON SHA-256을 승인 receipt에 연결한다. 승인에는 실행 근거와 확정된 방법론 Git commit을 함께 기록한다. root·범위·보관·writer 통제·의존성이 바뀌면 새 계획 revision과 digest를 만들고 변경 범위를 재승인한다. 계획 digest는 실행 승인을 대체하지 않으며, 예제의 placeholder 승인이나 commit은 실제 증명이 아니다.

독립 백업, 빈 경로 복원 시험, 원본 보존·staging·수신·복원에 필요한 용량, 보존기간, 실패·재개·보상 절차도 이 단계에서 정한다. Git commit, stash, 클라우드 sync나 파일 목록을 데이터 백업으로 간주하지 않는다. 권한 문제, NAS 복원 실패, 원본 또는 동기화 hash 불확실성이 있으면 다음 단계로 가지 않는다.

## 2. 설정한 범위를 이전 상태로 변경한다

승인된 계획을 참조하는 transaction을 생성하고 실제 writer 정지와 fencing을 검증한 뒤 `migrating_frozen`으로 전이한다. 문서 상태를 일괄 `migrating`으로 바꾸는 작업이 아니다. source cutoff inventory와 범위 digest를 고정하고 `scope_freeze` 증적에 연결한다.

writer 목록에는 에디터·자동 작업·개발/실행 엔진·컨테이너·다른 세션·다른 호스트·오프라인 동기화 기기를 포함한다. 부모 경로 writer가 자식 범위에 쓸 수 있는지, lease 만료 뒤 stale writer가 다시 쓸 수 있는지도 검사한다. 프로세스를 확인할 수 없거나 writer를 통제하지 못하면 `blocked`로 중단한다. lock 파일 존재, 두 번의 hash 일치나 오래된 상태 문자열로 writer 부재를 선언하지 않는다.

독립 백업과 복원 시험을 고정된 cutoff에 결합한다. 백업이 cutoff와 다르면 필요한 추가 백업을 검증한다. `scope_verified`, `dependency_verified`, `writer_quiescence_verified`, `scope_freeze`, `backup`, `restore_rehearsal`은 서로 다른 증적 종류다.

## 3. 이전이 끝날 때까지 해당 범위를 동결한다

원본의 승인된 artifact/record 범위만 업무 변경을 금지한다. 목적지는 생성 시부터 일반 업무 쓰기를 금지하고, 별도 승인된 migration actor의 staging 및 통제 기록 작성만 허용한다. 진행·실패·전달·보상 receipt는 별도 통제 채널에 append할 수 있지만 원본 payload를 경로 수정 작업장으로 사용하지 않는다.

다른 업무가 사용하는 공유 TRACE-DM 전체를 장기 동결하지 않는다. 혼합 원장은 원형 snapshot·cutoff anchor로 고정하고, owner 원장에서 다른 업무의 append는 계속 허용한다. 이전 대상 record의 변경만 거부하거나 보류한다. 도구가 record-level 경계를 보장하지 못하면 짧은 cutoff 구간의 합의된 writer 정지와 검증된 scoped export를 사용한다. 범위 격리를 증명할 수 없으면 완료할 수 없다.

메모리 lock과 클라우드 sync는 분산 fencing이 아니다. durable authority registry/CAS 또는 검증된 coordinator barrier 등 실제 통제 수단과 증거가 필요하다. 중첩 경로·stale/offline writer 통제가 끊기면 양쪽을 fail-closed로 유지한다. 정상 완료 뒤에도 원본 migrated scope는 `preservation_frozen`으로 남긴다.

## 4. 이전 패키지를 구성·압축하고 해시 검증 후 보관한다

동결 범위에서 payload, 파일별 size/hash, stable ID와 revision, 출처, 공유 owner 결정, 외부 의존성, 이전 경로 map, rollback 지침과 필요한 metadata를 패키지로 구성한다. 내부 `TRACE-DM-MANIFEST.json`은 payload 파일을 검증하며 자기 자신의 hash를 파일 목록에 넣지 않는다. 압축 직전과 직후 source inventory를 비교한다.

혼합 ledger와 GateReceipt는 원형을 보존한다. scoped export에는 원 record identity·선택 규칙·cutoff·원 파일 digest·원본 chain anchor·parent closure·export digest를 남긴다. 선택된 행만 복사해 원 chain인 척 재시작하거나 shared owner를 새 프로젝트 owner로 치환하지 않는다.

외부 보관 receipt는 archive 전체 bytes의 SHA-256, 보관 위치·기간·접근 조건을 기록한다. 내부 per-file manifest, archive digest, 실제 복원 시험, 최종 cutover receipt는 서로 다른 검증이다. archive와 함께 바꿀 수 있는 인접 checksum 파일만으로 진위나 승인을 증명하지 않는다. 신뢰된 승인 receipt/채널로 기대 digest를 고정해야 한다.

새 빈 검증 경로에 안전하게 복원해 파일별 hash/size와 필요한 metadata·의존성을 대조한다. archive digest 일치만으로 복원 성공을 선언하지 않는다. **보관 원본 archive는 불변 유지하며, 전달에는 복사본을 사용한다.** 패키지 보관을 이유로 원본 payload를 삭제하지 않는다.

## 5. 새 프로젝트 TRACE-DM을 생성하고 패키지를 복사 전달·검증한다

승인된 정확한 host/project/root에 목적지 TRACE-DM 통제 구조를 만들고 업무 쓰기는 차단한다. 새 governance namespace는 기존 payload stable ID와 구별하며, payload의 원 ID/history/hash는 그대로 유지한다.

보관 archive를 이동하지 않고 복사한다. 전송 중 파일은 `.partial` 등 비활성 위치에 두고 import 대상으로 사용하지 않는다. 완전 수신 후 bytes·archive digest·transaction/plan/scope binding을 대조한다. 로컬 수신, cloud 업로드 완료, 다른 장치 복제 확인은 구별하고 계획에 필요한 전달 범위만 완료 판정한다.

불확실한 timeout 뒤에는 먼저 실제 수신 상태를 조회한다. 같은 idempotency key와 같은 digest면 검증된 결과를 재사용하고, 다른 내용이면 격리한다. 네트워크 복사와 상태 파일만으로 exactly-once나 분산 원자성을 주장하지 않는다. 재시도는 상위 승인과 정책 안에서만 가능하며 자동 반복을 의미하지 않는다.

## 6. 안전하게 압축을 해제하고 새 경로에 맞게 업데이트한다

archive를 실행하거나 기존 업무 정본 위에 풀지 않는다. 목록·경로·형식·용량을 먼저 검증하고 새 빈 private staging에만 해제한다. 절대 경로, drive/UNC, `..`, NUL·제어 문자, 중복·NFC/대소문자 충돌, file-as-directory, 링크 탈출, 특수파일, 예상 밖 권한과 압축폭탄을 거부한다.

symlink/hardlink는 lexical target 검사만으로 안전하지 않다. 실제 해소 경로, 링크 뒤 후속 member, 교체 경쟁도 막아야 한다. 기본 archive 해제기는 모든 링크를 거부한다. 내부 상대 symlink가 필요한 경우 아래의 별도 opt-in adapter에서 일반 파일 검증을 마친 후 링크만 마지막에 재구성한다. hardlink는 해당 adapter에서도 지원하지 않는다. 허용 metadata를 보존하는 것과 archive의 owner·ACL·xattr·권한을 그대로 적용하는 것을 구별한다. script, macro, hook, 개발/실행 엔진이나 import side effect를 실행하지 않는다.

해제 직후 전수 hash·size를 대조한다. 이후 Catalog, Manifest, 현재 resolver, 코드·설정·본문의 경로 의존성을 새 revision에서 업데이트한다. 원형 해제본·archive·historical event/receipt는 수정하지 않는다. `document-relocation-map.yaml`은 artifact ID, old/new revision·management revision, old/new path/hash, 내용 변경 여부를 연결한다. 바이트가 같은 payload는 원 revision/hash를 유지하고 위치 관리정보만 새 revision으로 만든다. 코드의 바이트가 바뀌면 파생 payload revision과 새 hash를 기록한다.

절대 참조, 외부 저장소·엔진 의존성, 내부 상대 링크, 분할 실행 구조, mount, parent closure, 역참조, host/project/cwd와 접근권한을 검사한다. 옛 경로를 전체 검색치환해 불변 증거를 위조하지 않는다. 미지원 metadata·의존성 validator가 있으면 한계와 blocker를 기록한다.

## 7. 목적지 TRACE-DM에 수신·검증 완료를 기록한다

수신 archive, 안전 해제, per-file 무결성, relocation map과 파생 revision, 의존성·공유 owner·parent closure·역참조 검증, rollback anchor를 acceptance evidence에 결합한다. 필수 blocker가 없을 때만 `received_complete`로 기록한다. UI에서 사용하는 destination `completed` 표현도 수신 완료라는 의미를 명시해야 한다.

`reference_only`에서는 payload 전송/압축 해제 증적 대신 reference manifest 보존·접근·version/hash·지속성 검증, 등록·dependency·rollback 검증과 reference acceptance를 요구한다. 이 증적의 subject digest는 동일 reference manifest에 결합한다. 이 모드의 수신 완료는 **참조 metadata 수신·검증 완료**이며 외부 payload의 물리 전달·복원 완료가 아니다. 실제 외부 접근/지속성/복구 증명의 진위는 실행 adapter가 확인해야 한다.

**이 단계에서 목적지는 canonical owner가 아니고 업무 쓰기도 비활성이다.** 원본은 scope freeze를 유지한다. 사람은 읽기 전용으로 결과를 검토할 수 있다. migration actor만 다음 단계의 통제 기록을 준비할 수 있다. 수신 receipt를 전체 완료나 개발 재개 승인으로 사용하지 않는다.

## 8. 원 범위 이전 완료·보존 동결과 관련 연결 전환을 확정한다

조정자는 source freeze와 destination acceptance가 같은 transaction, 승인 plan digest, scope/cutoff, archive, authority epoch를 참조하는지 확인한 뒤 `cutover_pending`으로 진입한다. 양쪽 업무 writer를 계속 차단한 상태에서 source transfer와 destination prepared 기록을 append한다.

현재 mutable Catalog·resolver·forward/reverse pointer의 새 revision만 승인된 새 정본을 가리키게 한다. 과거 receipt/event의 옛 링크는 남기고 location map으로 해석한다. 공유 원장과 관련 없는 업무의 pointer·owner는 바꾸지 않는다.

기대 authority epoch에서 단일 owner를 전환하고 새 epoch를 기록한다. durable CAS가 없는 환경은 실제 양측 barrier와 검증된 commit marker를 사용하며 분산 원자성은 주장하지 않는다. 한쪽만 반영되면 `partial_commit`으로 남기고 양쪽 업무 쓰기를 차단한다. 실제 owner/pointer 상태를 조회해 승인된 재개 또는 보상 절차로 처리한다.

양측 source transfer/freeze, destination acceptance/prepared, archive·복원·relocation·parent/shared-owner 검증, pointer 전환, 단일 canonical owner를 한 cutover receipt에 결합한다. receipt는 입력 evidence의 ID뿐 아니라 digest도 고정하고 자기 hash 순환을 만들지 않는다. 이 전체 완료 증적을 양측에서 확인한 경우에만 transaction `completed`를 기록한다.

원본 migrated scope는 보존 동결한다. 목적지 업무 활성화는 전체 완료 이후에도 별도 `business_resume_approval_ref`가 있어야 한다. 사용자 실행 승인과 확정된 방법론 commit을 후속 실행 receipt에서 추적할 수 있어야 한다. source 삭제, stub-only 전환, 공유 원장 전체 덮어쓰기와 자동 개발 재시작은 이 절차의 필수 동작이 아니다.

## 상태·재개·rollback 계약

정상 transaction phase는 `planned → migrating_frozen → packaged → delivered → relocated → received_complete → cutover_pending → completed`다. 3단계의 지속 동결은 별도 phase가 아니라 이후 모든 phase의 불변조건이다. `blocked`, `failed`, `partial_commit`, `aborted`는 문서 lifecycle 상태와도 별개다.

실패에는 마지막 검증 phase, blocker, 원인과 재개 조건을 남긴다. 실패/abort 전이는 직전 `last_verified_phase`를 그대로 유지하며, 실패 상태를 통해 새 PASS 증거를 주입하거나 checkpoint를 진전시킬 수 없다. 정상 상태는 `phase == last_verified_phase`여야 하고 실패 후에는 마지막 검증 단계의 바로 다음 정상 단계로만 재개한다. 단계 건너뛰기나 failed receipt의 상태 수정 대신, 같은 transaction/idempotency key에서 증거를 조회하고 새 correction receipt가 이전 실패 ID를 명시적으로 supersede하도록 한다. 원 실패 bytes와 hash는 유지한다. 참조 `transition`은 순수 checkpoint 비교 함수이며 실제 durable CAS나 append-only 저장소가 아니다. 이미 위조된 current checkpoint를 단독 입력하면 저장소 이력까지 인증하지 않으므로, 운영 caller는 신뢰된 직전 상태와 durable CAS를 제공해야 한다.

전환 전 취소는 목적지 미활성·source authority 유지·명시적 `abort_approval_ref`와 writer-hold 증적 digest를 확인한 뒤 `aborted`로 기록한다. 정확히 같은 상태의 replay 외에는 terminal이며, 동결을 자동 해제하거나 정상 흐름으로 복귀하지 않는다. 이후에는 별도 승인된 compensation 또는 새 transaction을 사용한다. 전환 후 rollback은 목적지 writer를 먼저 정지하고 신규 작업·공유 원장의 후속 event를 백업한다. 새 데이터가 있으면 보존·병합/선택 복구 계획 없이 T0를 덮어쓰지 않는다. 별도 보상 transaction, 증가한 epoch, 승인·복원 receipt와 append-only compensation event를 사용한다. `document-migration-compensation.yaml`은 이를 검증하되 직접 복원·권한 활성화하지 않는다.

cloud 충돌, offline writer, 미확인 삭제 전파, 공간·권한 문제는 중단 조건이다. 완료나 source 삭제로 수습하지 않는다. 새 범위·source bytes·의존성이 발견되면 새 계획·패키지 revision과 필요한 재승인을 연결한다.

## 제공 구현과 실제 운영 증명의 경계

| 제공 항목 | 참조 구현이 확인하는 것 | 별도 실행기가 증명할 것 |
|---|---|---|
| 계획·transaction schema | 승인 digest, scope/ID/parent·owner 관계, 단계별 evidence 결합 | 승인자 신원·유효성, 실제 원본 inventory·외부 의존성 |
| phase·cutover semantic | 수신/전체 완료/업무 활성화 분리, 양측 필수 evidence·단일 owner·epoch·digest 일치 | 실제 양측 commit, trusted registry/CAS와 포인터 대조 |
| writer 계약 | memory lock/sync 주장 거부, 중첩·stale/offline 확인 필수 | 모든 실제 writer 차단·fencing proof의 진위·만료 |
| 안전 archive 해제 | digest 고정 snapshot, 제한형 USTAR/gzip, 모든 링크·특수파일·확장 헤더 거부, no-follow/exclusive private staging, 복원 bytes 재해시 | 승인된 host/root, 저장장치·외부 백업 복원, 필요한 metadata adapter |
| opt-in 상대 링크 복원 | 일반 파일 전수 재검증, target 문자열·해시·graph 검증, 마지막 링크 생성·사후 대조 | 실제 writer 정지, 외부 엔진의 의존성·동작 복원 시험 |
| opt-in chunk-set | bounded streaming, 정확한 offset·part/whole hash·inventory·예산·명시적 resume | 대형 실제 artifact·저장장치 성능/장애·전송·복원 시험 |
| reference-only | 참조 관리 scope/외부 payload owner 분리, 고정 binding·단계별 증적 | 외부 위치·권한·version/hash·보존/복구의 실제 유효성 |
| 재개·보상 | identity·plan·이력 불변, 순서·idempotent replay·신규 데이터 보호 계약 | durable 상태 저장·장애 조정·실제 rollback·운영 재개 |

안전 해제기는 기본 archive 256MiB, 전체 해제 stream 272MiB, payload 256MiB, 파일당 64MiB, member 4,096개, 내부 manifest 2MiB로 제한한다. 이 기본값은 변경하지 않았다. USTAR 확장(PAX/GNU), sparse archive, 링크, executable mode와 특수파일은 지원하지 않는다. 큰 자료는 아래의 별도 chunk-set 형식과 명시 승인된 한도를 사용한다. nested archive/dereference나 링크 누락으로 기본 검사를 우회하지 않는다.

복원은 SHA로 고정된 private snapshot에서 수행하고, parent/child descriptor에 `O_NOFOLLOW`와 새 파일 `O_EXCL`을 사용한다. 실패하면 private partial staging이 남을 수 있으며 완료 receipt를 만들지 않는다. 같은 UID/root의 악의적 동작이나 untrusted parent를 통제하는 보장은 제공하지 않는다. 운영 실행기는 writer quiescence와 신뢰된 parent를 확보해야 한다.

예제는 일반화한 synthetic 데이터이며 실제 실행 증적이 아니다. plan·archive·manifest digest만으로 승인자의 신원을 인증하지 않는다. 본 참조 구현에는 credential, 외부 저장소/cloud driver, distributed registry, project registration, 개발/실행 엔진별 validator나 자동 cutover가 없다. opt-in adapter는 로컬 staging/패키지 bytes를 작성하지만 실제 프로젝트 이전·업무 재개 권한을 제공하지 않는다. 이 변경의 시험은 작은 synthetic fixture만 사용하며 실제 자료 호환성·대형 복원 성공을 주장하지 않는다.

### 내부 상대 링크의 opt-in 복원

기본 archive 링크 거부를 해제하지 않는다. 대신 원 링크를 일반 JSON descriptor로 표현해 원 artifact/revision, 경로, **그대로의 target 문자열과 그 문자열의 SHA-256**, 최종 target의 종류·inventory digest를 보존한다. 생성 리소스의 상대 파일 참조와 분할 실행의 공용 디렉터리 참조를 일반화한 synthetic 예제를 제공한다.

`document-migration-link-policy.yaml`의 기본값은 `descriptor_plan_only`/`restore_supported=false`다. 정적 validator는 root containment, target 존재, 링크·디렉터리 순환, 중간 symlink 경유, Unicode/case/identity 충돌, dangling/absolute/drive/UNC/탈출 경로, target 문자열·inventory hash를 검사한다. `..`가 있어도 원 문자열을 바꾸지 않고 root 밖 탈출 여부를 판정한다. 기본 descriptor는 inventory 4,096개·링크 1,024개 이하, chain 64단계 이하로 제한한다.

**raw target의 각 구성요소를 `.`/`..` 제거 전에 순서대로 검증한다.** 제거될 경로도 실제 존재하는 directory여야 하며 symlink를 경유할 수 없다. 예컨대 `alias → sub/dir`일 때 `alias/../file`은 lexical `file`과 OS의 `sub/file` 해석이 달라질 수 있으므로 거부한다. `missing/../file`, `file/../file`, `alias/.`도 거부한다. 정상 `../file`, directory를 통한 연속 `..`는 허용한다. 정확한 마지막 구성요소만 선언된 symlink chain으로 해소할 수 있으며 chain의 각 raw target에도 같은 규칙을 적용한다.

명시 승인된 `document-migration-link-restore.yaml`은 `restore_relative_symlinks`/`restore_supported=true`다. `restore_relative_links`는 승인 policy와 inactive staging receipt를 digest로 결합하고 별도 private state에 claim/실패/완료 기록을 남긴다. inventory·링크 수·payload/file bytes를 승인 한도로 제한한다. 모든 보통 파일을 전수 해시 검증한 후 target-first 순서로 정확히 승인된 링크만 마지막에 생성한다. no-follow dirfd로 target 종류·존재와 파일 hash를 검사하고 링크 원문·전체 tree를 사후 대조한다. 디렉터리 digest는 정렬된 descendant의 상대 path·kind, 파일 SHA, 링크 원문·문자열 SHA를 canonical JSON 배열로 streaming hash한다. inode/time/ACL 복원 증명은 아니다.

기존 파일을 덮어쓰지 않고 symlink parent를 따라 쓰지 않는다. 일반 파일 복원과 링크 복원은 같은 staging flock을 공유한다. 링크 생성 뒤 regular chunk restore는 봉인되어 후속 member 쓰기를 거부한다. 실패 시 partial 링크와 실패 receipt는 남으며 같은 policy·receipt에 대한 명시적 resume 승인만 허용한다. 이 로컬 협력 lock은 분산 fencing이 아니다. 동일 UID/root의 악의적 writer나 untrusted parent를 통제하지 않으므로 운영 quiescence가 필수다.

runtime은 raw 경로에서 방문하는 directory를 no-follow dirfd로 다시 열어 종류·private 권한을 확인한다. terminal chain의 실제 readlink 문자열을 대조하고 최종 보통 파일 hash를 검증한다. 생성 직후·전체 사후 검사·완료 replay에서 원 readlink와 OS가 해석한 대상의 device/inode/kind를 안전하게 검증한 최종 target과 비교한다. 이 마지막 follow는 metadata 조회뿐이며 payload 읽기/쓰기 경로로 사용하지 않는다. 불일치면 완료 receipt를 만들지 않는다. 동결 범위에서의 동등성 검사이지 악성 동일 UID/root writer에 대한 race-free 분산 fencing 보장은 아니다.

### 대형 artifact의 bounded chunk-set

`migration_stream.py`의 `pack_chunk_set`/`restore_chunk_set`은 `TRACE-DM-CHUNKSET/1` raw part 형식이다. 하나의 큰 파일도 결정론적 순서의 여러 part로 나누며, package-set에 file path·offset·length·part hash·whole-file hash·exact inventory·plan/config digest를 기록한다. archive SHA와 `package_set_sha256`은 다른 필드이고 서로 대체하지 않는다. 누락·중복·순서·gap/overlap·cross-plan·변조를 거부하며 빈 파일도 inventory로 검증한다. 통제 JSON의 canonical bytes hash까지 검증한다. 압축 archive를 잘게 잘라 링크 검사를 생략하는 방식이 아니다.

`document-migration-stream-policy.yaml`은 기본 disabled 예제다. part 상한 256MiB, I/O buffer 상한 1MiB(예제 64KiB)는 확대할 수 없다. aggregate/file/count/metadata/disk/free-space/temp/failure-retention 한도를 별도 승인 digest에 결합한다. 예제의 aggregate 128GiB·단일 파일 96GiB·metadata 64MiB·bundle/staging 132GiB·free reserve 4GiB는 설정 예시이지 실제 시험 규모나 실행 승인이 아니다. payload는 RAM에 전부 올리지 않고, metadata는 byte/item 수로 제한한다.

pack은 동결 source inventory를 전후 검증하며 part를 exclusive 생성한다. package-set과 완료 receipt가 나오기 전 part는 비활성이다. 중단된 짧은 part는 명시 resume 시 실패 보관함에 격리하고 기존 실패를 보존한다. 완전한 part는 해시 재검증해 재사용한다. restore는 part를 먼저 전수 검증한 뒤 private staging에 재구성하고 파일 전체 hash를 대조한다. 재개 시 기존 prefix와 일치하는 bytes만 인정하고 불일치는 truncate/덮어쓰기 없이 거부한다. source/plan/config 변경·예산 초과는 실패폐쇄하고 자동 재시도하지 않는다.

링크 descriptor가 있으면 regular restore 결과는 `regular_files_verified`/`links_pending`이지 전체 acceptance가 아니다. 링크 adapter의 사후 검증까지 통과해야 한다. hardlink·특수파일, owner/ACL/xattr·실행 권한·sparse extent 재현은 지원하지 않는다(일반 파일 bytes는 private 0600으로 재구성). 이러한 metadata가 필수인 실제 범위는 별도 adapter와 복원 시험 전 차단한다. 외부 전송·NAS/cloud 보존·엔진 의존성·durable CAS는 여전히 실행기 책임이다.

## 사용과 검증

새 schema는 `schemas/document-migration.schema.json`이며 기존 core schema·문서 enum을 변경하지 않는다. 새 계약 version `0.1.0`을 선택 적용하고, 기존 TRACE-DM `0.2.0` metadata envelope에 companion artifact로 등록한다.

기본 계약 템플릿 6개에 링크 descriptor·opt-in 복원·streaming policy·package-set·reference-only plan 5개를 더했다. 기존 13개와 함께 총 24개를 검증한다.

```bash
.venv/bin/python scripts/validate_templates.py templates/document-migration-plan.yaml
.venv/bin/python scripts/validate_templates.py
.venv/bin/python -m pytest -q
```

승인된 패키지에 한해서는 `trace_gate.migration_archive.extract_verified`를 adapter에서 호출한다. 기대 archive/manifest SHA는 신뢰된 외부 receipt에서 받아야 하고, destination은 새 private staging이어야 한다. 이 API의 `pass`는 안전 해제·파일 검증 결과일 뿐 source freeze, 수신 acceptance, cutover나 업무 활성화가 아니다.

필수 실패 시험은 `tests/test_migration.py`, `test_migration_archive.py`, `test_migration_links.py`, `test_migration_optin.py`, `test_migration_reference.py`, `test_migration_raw_links.py`에 있다. premature write, cutover 누락, failed/unknown evidence, 위조 failure checkpoint/PASS 주입·abort, plan/epoch drift, parent/shared owner, mixed export, 이력 수정·신규 데이터 손실, path/link/TOCTOU·폭탄, raw dot-component의 실제 OS 해석·target hash 불일치, chunk 경계·buffer·예산·부분 실패/재개, reference-only 증적/owner·모드 혼동을 검사한다. 실행기는 실제 writer·외부 저장소·sync·엔진 의존성 실패 시험을 추가해야 한다.
