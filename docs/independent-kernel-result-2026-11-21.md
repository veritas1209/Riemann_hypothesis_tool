# RH / Dirichlet GRH Lean 코드의 독립 커널 재검증 결과

**2026년 11월 21일, TRX50에서 우리가 빌드한 `.olean`을 독립 검사기 con-leche로 읽어 최종 정리 5개와 그 전이 의존 선언 1,468,414개를 재검증했습니다.** 검사기는 `--verified` 모드에서 모든 대상 선언을 받아들였고 종료 코드 0을 반환했습니다. 검사 명령의 경과 시간은 **8분 38.39초**, GNU time이 측정한 최대 RSS는 **약 27.86 GiB**였습니다.

이는 [앞선 전체 빌드·공리 검사](verification-result-2026-11-21.md)에 이어 수행한 별도 검사입니다. 증명과 원본 Lean 코드의 공로는 **Levent Alpöge와 협력자**에게 있으며, 우리의 기여는 별도 장비에서의 검증 재현과 실행 기록 공개입니다.

## 확인한 결과와 범위

| 항목 | 결과 |
| --- | --- |
| 입력 | TRX50의 기존 전체 빌드에서 생성한 `.olean` 파일 |
| 선택한 대상 | `Main`의 최종 정리 5개와 그 증명·타입이 참조하는 전이 의존 선언 |
| 입력 방식 | con-leche의 `--olean` 직접 읽기 |
| 검사 모드 | `--verified` |
| 검사한 선언 레코드 | 1,468,414개 |
| 최종 판정 | `con-leche: accepted 1468414 declarations (--verified)` |
| 검사 명령 종료 코드 | 0 |
| 상태 기록의 시작 시각 | 2026-11-21 18:59:45 KST |
| 상태 기록의 완료 시각 | 2026-11-21 19:08:26 KST |

대상은 `riemannHypothesis`, `riemannZeta_eq_zero_re`, `LFunction_ne_zero_of_half_lt_re`, `LFunction_eq_zero_re`, `LFunction_eq_zero_re_of_isPrimitive`입니다. 각 이름을 `--constant`로 지정했습니다.

로그에는 의존성을 찾는 과정에서 186,200개 모듈을 읽고 2,701,132개 상수를 병합한 뒤, 선택한 다섯 정리로부터 1,468,414개 검사 레코드를 생성한 과정이 남아 있습니다. 읽은 모듈 수에는 Lean과 외부 의존성이 포함됩니다. 이 결과가 원본 175,187개 소스 모듈의 모든 선언을 각각 검사했다는 뜻은 아닙니다.

이번 결과는 Lean의 기존 컴파일 성공 및 공리 감사에 별도 커널 구현의 검사를 더합니다. con-leche는 자체 항 표현과 검사 알고리즘을 사용하지만 Lean으로 컴파일되므로 컴파일러·런타임과 `.olean` 해독 방식에 대한 신뢰는 남습니다. 검사기의 구조와 보증 범위는 [고정 커밋의 README](https://github.com/leanprover/con-leche/blob/5dc36dc15b7bee891a02406b78a8ac816a47baad/README.md)에 설명되어 있습니다.

별도 challenge와의 정리 대조 및 논문에 대한 사람의 수학적 심사는 이번 범위에 포함하지 않았습니다. 입력은 우리 빌드의 `.olean`이며 NDJSON export는 사용하지 않았습니다.

## 실행 환경과 입력 식별

| 항목 | 값 |
| --- | --- |
| CPU | AMD Ryzen Threadripper PRO 9955WX, 16코어 / 32논리 CPU |
| 시스템 RAM | 약 251 GiB |
| 운영체제 | Ubuntu 24.04.3 LTS |
| 원본 프로젝트 | `/data/lean-projects/remark` |
| 원본 빌드의 Lean | `leanprover/lean4:v4.33.1` |
| Mathlib commit | `0df444a360eaa60ab8c11dca51a86af692955474` |
| 검사기 | [leanprover/con-leche](https://github.com/leanprover/con-leche) |
| 검사기 commit | `5dc36dc15b7bee891a02406b78a8ac816a47baad` |
| 검사기를 빌드한 Lean | `leanprover/lean4:v4.36.0` |
| 검사 스레드 | 32 |
| 직접 검사 서비스의 cgroup 제한 | `MemoryHigh=100 GiB`, `MemoryMax=120 GiB`, `MemorySwapMax=1 GiB` |

원본 `.olean`의 검색 경로에는 프로젝트·Mathlib·기타 의존성과 **원본 Lean 4.33.1**의 표준 라이브러리를 지정했습니다. 검사기를 빌드한 Lean 4.36.0의 라이브러리를 증명 입력으로 대체하지 않았습니다.

| 식별 대상 | SHA-256 |
| --- | --- |
| 원본 압축 아카이브 | `9631b4f020199e580ba86178cb56c6e9b09f690c2369a80dc4334aa93f49c7fc` |
| 검사 전후의 `Main.olean` | `9146dfb4148e24d2252e04c50742f3e49dd126dfcc5006d10bf5f873ddf796ad` |
| 실행한 con-leche 바이너리 | `588c0b86a3cb5e9fbee489ec1d609435599d82b155df1bb4f5e6886d43e3a12e` |
| 직접 검사 실행기 | `a8c7c0b88e2d7d8ccaa9ceea79b35bbe09f803560e72e4ab5386ec96accdcf1f` |

`Main.olean`의 해시는 검사 전후에 같았습니다. 실행 명령·입력 경로·검사기 바이너리 해시·최종 판정은 [상태 JSON](../data/independent-kernel-2026-11-21/direct-olean-status.json)에, 도구 버전과 수집 정보는 [출처 기록](../data/independent-kernel-2026-11-21/independent-provenance.json)에 있습니다.

## 시간과 메모리

| 측정 항목 | 결과 |
| --- | --- |
| GNU time의 검사 명령 경과 시간 | **518.39초, 8분 38.39초** |
| 검사기 자체 로그의 읽기·파싱 단계 | 65.9초 |
| 검사기 자체 로그의 검사 단계 | 452.2초 |
| GNU time의 사용자 / 시스템 CPU 시간 | 14,385.14초 / 261.51초 |
| GNU time의 평균 CPU 사용률 | 2,825% |
| GNU time의 최대 RSS | **29,213,132 KiB, 약 27.86 GiB** |
| GNU time의 swap 횟수 | 0 |

설치와 검사기 빌드 시간은 검사 명령의 8분 38.39초에 포함하지 않았습니다. 상태 실행기는 10초 간격으로 종료 여부를 확인했으므로 상태 JSON의 경과 시간 520.058초와 실제 검사 명령의 경과 시간에는 차이가 있습니다.

검사 중 별도 NDJSON 내보내기 작업이 병행되었으며 직접 검사 통과 후 중지했습니다. 그 작업의 출력은 이번 성공 판정에 사용하지 않았습니다. 시간과 최대 RSS는 직접 검사 프로세스의 측정값으로, 다른 부하가 없는 단독 벤치마크나 서버 전체 메모리 사용량, 전체 설치·빌드의 최소 RAM을 나타내지 않습니다.

## 같은 빌드에서 재현하는 명령

아래는 **원본 전체 빌드와 위 고정 커밋의 con-leche 빌드가 이미 준비된 TRX50 환경**에서 실행하는 명령입니다. 원본 빌드의 Lean 4.33.1로 검색 경로를 구한 뒤, Lean 4.36.0으로 빌드한 검사기 바이너리를 직접 실행합니다.

```bash
cd /data/lean-projects/remark
export ELAN_HOME=/home/user/.elan
export PATH="$ELAN_HOME/bin:$PATH"
export ELAN_TOOLCHAIN=leanprover/lean4:v4.33.1
checker=/data/lean-projects/remark-independent-kernel-20261121/con-leche/.lake/build/bin/con-leche
lean_path="$(lake env printenv LEAN_PATH)"
core_path="$(lean --print-prefix)/lib/lean"
ulimit -c 0
/usr/bin/time -v "$checker" \
  --verified --jobs=32 --progress=10s \
  --olean --search-path="${lean_path}:${core_path}" \
  --constant=riemannHypothesis \
  --constant=riemannZeta_eq_zero_re \
  --constant=LFunction_ne_zero_of_half_lt_re \
  --constant=LFunction_eq_zero_re \
  --constant=LFunction_eq_zero_re_of_isPrimitive \
  Main
```

실제 실행기는 종료 코드가 0이고 `accepted N declarations (--verified)` 형식의 양수 선언 수가 정확히 한 번 출력됐는지 확인했습니다. 실제 명령 전체는 상태 JSON과 GNU time 원문에 보존했습니다.

## 공개한 실행 기록

- [검사 원문 로그 전체](../data/independent-kernel-2026-11-21/direct-olean-check.log) · [GNU time 측정 원문](../data/independent-kernel-2026-11-21/direct-olean-check.time.txt)
- [실행 명령과 완료 상태](../data/independent-kernel-2026-11-21/direct-olean-status.json) · [도구·입력 출처 기록](../data/independent-kernel-2026-11-21/independent-provenance.json)
- [실제로 사용한 직접 검사 실행기](../data/independent-kernel-2026-11-21/direct-olean-run.py)
- [위 다섯 파일의 SHA-256 목록](../data/independent-kernel-2026-11-21/SHA256SUMS)

저장소를 받은 뒤 기록 파일의 바이트 일치를 확인하려면 다음을 실행합니다.

```bash
cd data/independent-kernel-2026-11-21
sha256sum -c SHA256SUMS
```
