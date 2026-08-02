# Contributing

TRACE Gate Framework에 대한 개선 제안과 기여를 환영합니다.

## 제안 원칙

- 새로운 절차는 해결하려는 실제 실패 패턴을 설명해야 합니다.
- Gate는 가능하면 기계적으로 판정할 수 있어야 합니다.
- 구현 완료와 운영 권한 승격을 구분해야 합니다.
- 실패와 미확정 상태를 숨기지 않습니다.
- 비밀정보·개인정보·내부 시스템 식별자를 증적 예시에 포함하지 않습니다.

## 변경 절차

1. Issue에서 문제와 기대 결과를 설명합니다.
2. 작은 범위의 branch와 Pull Request를 사용합니다.
3. 변경된 원칙에 대응하는 예시나 템플릿을 함께 갱신합니다.
4. Markdown과 Mermaid 렌더링을 확인합니다.
5. 호환성을 깨는 변경은 문서 버전과 migration note를 포함합니다.
6. 하부 프레임워크 변경은 Framework ID, 자체 버전과 호환되는 TRACE core 버전을 명시합니다.

## Pull Request 체크리스트

- [ ] 문제와 변경 목적이 명확합니다.
- [ ] 변경 범위 밖의 내용을 섞지 않았습니다.
- [ ] 관련 템플릿을 갱신했습니다.
- [ ] Mermaid code block이 정상적으로 닫혔습니다.
- [ ] 비밀정보와 내부 경로가 없습니다.
- [ ] 하부 프레임워크 변경이면 core 호환성과 관련 템플릿을 확인했습니다.
- [ ] 남은 한계와 후속 작업을 기록했습니다.

## 로컬 템플릿 검증

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python scripts/update_document_manifest.py
.venv/bin/python scripts/validate_templates.py
.venv/bin/python -m pytest -q
```

새 계약은 JSON Schema, semantic 음성 테스트와 문서 예시를 함께 추가합니다. Gate status를 예제에 수동으로 고정하지 않고 evaluator가 재현 가능한 증적에서 계산하게 합니다.

Markdown을 추가하면 `docs/managed-document-manifest.yaml`에 TRACE-DM 공통 metadata envelope를 먼저 등록합니다. 기존 Markdown을 변경하면 manifest 갱신 명령으로 revision, SHA-256, size와 freshness를 동기화합니다. 문서 본문에 자기 hash를 삽입하지 않습니다.

## 기여 라이선스

Pull Request 또는 그 밖의 방법으로 이 저장소에 기여물을 제출하면, 별도로 명시하고 합의하지 않는 한 해당 기여물을 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)으로 제공하는 데 동의하는 것으로 봅니다.
