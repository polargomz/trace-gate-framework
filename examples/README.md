# Reference examples

Gate 평가:

```bash
python scripts/evaluate_gate.py templates/gate-decision.yaml examples/gate-facts.yaml
```

모델 공급자에 종속되지 않는 결정적 TRACE-AH 실행 예제:

```bash
python examples/runtime_demo.py
```

예제는 실제 원격 mutation을 수행하지 않는다. 운영 adapter는 반드시 Request Receipt, Tool Definition과 authorization provider를 연결해야 한다.
