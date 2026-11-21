# Riemann Hypothesis Tool

Ubuntu에서 한 명령으로 실행 환경을 준비하고, Alpöge의 RH / Dirichlet GRH Lean 프로젝트를 빌드한 뒤 최종 정리의 공리를 검사합니다. 원본 코드를 내려받아 해시를 확인하고, 프로젝트가 지정한 Lean과 Mathlib 버전을 사용합니다.

## 시작하기

저장소를 복제한 뒤 **Ubuntu 터미널**에서 실행합니다.

```bash
git clone https://github.com/veritas1209/Riemann_hypothesis_tool.git
cd Riemann_hypothesis_tool
```

설치와 검증을 시작하는 명령은 하나입니다.

```bash
bash verify.sh
```

지원 대상은 **Ubuntu 22.04 / 24.04의 x86_64 Linux와 기존 WSL2 Ubuntu 환경**입니다. Windows PowerShell에서 직접 실행하는 도구는 아니며, WSL 배포판 자체를 설치하지는 않습니다.

필요한 시스템 패키지가 없으면 `apt`로 설치합니다. 대형 Lean 빌드에 필요한 메모리 매핑 수를 확보하기 위해 `vm.max_map_count`가 `1,048,576`보다 작으면 `sysctl -w`로 그 값까지 올립니다. 이 설정은 시스템 전체에 적용되지만 현재 부팅에만 유지되며 영구 설정 파일은 수정하지 않습니다. 관리자 권한은 패키지 설치와 이 설정 변경에 사용하고, 필요한 경우 `sudo`가 암호를 요청할 수 있습니다. 다운로드와 의존성 준비에는 인터넷 연결이 필요합니다.

기본 작업 폴더는 저장소 아래의 `.replay/`입니다. 여유 있는 다른 디스크를 사용하려면 본인에게 쓰기 권한이 있는 경로를 지정합니다.

```bash
bash verify.sh --work-dir /data/lean-replay
```

작업 폴더는 Linux 파일시스템에 있어야 합니다. WSL에서는 `/mnt/c` 같은 Windows 드라이브에 작업 폴더를 두면 실행기가 중단하므로 Ubuntu의 홈 디렉터리 등 Linux 파일시스템을 사용하세요. 소스 파일과 빌드 결과의 수가 매우 많습니다.

기본 실행은 터미널 전경에서 진행됩니다. SSH 연결이 끊겼을 때 작업이 계속된다고 보장하지 않으므로, 원격 서버에서 장시간 실행할 때는 `tmux` 같은 세션 도구 안에서 실행하는 것을 권장합니다. 이 실행기는 `tmux`를 자동 설치하거나 세션을 자동 생성하지 않습니다.

## 무엇을 자동으로 하는가

1. 운영체제·아키텍처·메모리·디스크와 필요한 시스템 도구를 확인합니다.
2. 작업 폴더의 `tools/elan/`에 전용 Lean 환경을 준비하고 Lean `v4.33.1`을 설치합니다.
3. 원본 14개 압축 조각을 내려받아 전체 크기와 SHA-256을 확인합니다.
4. 원본이 고정한 Mathlib 버전과 캐시를 준비합니다.
5. 표준 Lake를 사용해 의존관계 순서대로 모듈을 묶어 빌드하고 체크포인트를 저장합니다.
6. 최종 `Main` 빌드 후 정리 5개의 전이적 공리를 검사합니다.

Lean 도구 체인은 작업 폴더 안의 전용 elan 환경에서 관리하며, 기존 사용자 elan의 기본 도구 체인을 변경하지 않습니다.

원본 아카이브는 **239,275,554 bytes, 약 239 MB**입니다. 풀린 Lean 소스는 약 1.03 GB이며, 도구 체인·의존성·캐시·빌드 결과에 필요한 공간은 별도입니다.

| 고정 입력 | 값 |
| --- | --- |
| Lean | `leanprover/lean4:v4.33.1` |
| Mathlib commit | `0df444a360eaa60ab8c11dca51a86af692955474` |
| 원본 아카이브 SHA-256 | `9631b4f020199e580ba86178cb56c6e9b09f690c2369a80dc4334aa93f49c7fc` |

빌드가 성공한 묶음의 `.setup.json` 임시 파일만 정리합니다. 이 정리는 소스와 컴파일 결과를 보존하면서 중복된 import 정보가 디스크에 계속 쌓이는 것을 줄이기 위한 것입니다.

## 얼마나 큰 컴퓨터가 필요한가

기본 실행기는 다음 값을 **보수적인 실행 시작 기준**으로 사용합니다. 완주가 보장되는 최소 사양이나 최종 사용량의 상한은 아닙니다.

| 항목 | 기본 동작 |
| --- | --- |
| 최초 원본 준비 시 가용 디스크 | 작업 폴더가 있는 파일시스템에 160 GiB 이상 |
| 최초 원본 준비 시 가용 RAM | 32 GiB 이상 |
| 자동 병렬 수 | 최대 8 |
| 실행 중 디스크 점검 | 새 묶음 시작 전에 가용 공간이 20 GiB 미만이면 중단 |
| 실행 중 RAM 점검 | 새 묶음 시작 전에 가용 RAM이 8 GiB 미만이면 중단 |
| 하드 메모리 제한 | 자동 설정하지 않음 |

원본이 이미 준비된 재실행에서는 실행 중 기준인 디스크 20 GiB와 RAM 8 GiB를 사용합니다. 실행 중 자원 점검은 묶음 사이에서 이루어집니다. 운영체제가 강제하는 메모리 상한이 아니므로 한 묶음의 실행 중 자원 사용량은 증가할 수 있습니다.

큰 장비에서는 병렬 수와 묶음 크기를 직접 지정할 수 있습니다.

```bash
bash verify.sh --work-dir /data/lean-replay --threads 32 --batch-size 2048
```

기존 TRX50 실행에서 측정한 CPU·RAM·디스크 사용량과 측정 조건은 **[자원 사용량 문서](docs/resources.md)**에 있습니다. 당시 사용한 별도 cgroup 메모리 제한과 현재 자동 실행기의 기본 동작은 구분되어 있습니다. 새 자동 설치 경로의 전체 완주 시간과 최종 최대 자원 사용량은 아직 측정하지 않았습니다.

## 진행 확인, 중단, 재개

아래 예시는 `/data/lean-replay`를 사용하는 경우입니다. 관리 명령에도 실행할 때와 **같은 작업 폴더**를 지정해야 합니다.

```bash
bash verify.sh --work-dir /data/lean-replay --status
```

현재 묶음이 끝난 뒤 멈추도록 요청합니다.

```bash
bash verify.sh --work-dir /data/lean-replay --stop
```

중단 요청을 해제하고 기존 체크포인트부터 이어갑니다. 이전과 같은 병렬 수를 원하면 옵션도 함께 지정합니다.

```bash
bash verify.sh --work-dir /data/lean-replay --resume --threads 32 --batch-size 2048
```

`--stop`은 실행 중인 Lean 작업을 즉시 강제 종료하지 않습니다. 묶음이 클수록 멈추기까지 시간이 더 걸릴 수 있습니다. 중단 요청 없이 실패했던 실행도 같은 작업 폴더로 다시 실행하면 유효한 체크포인트를 재사용합니다. 입력 소스나 설정이 달라진 경우에는 오래된 체크포인트를 그대로 이어 쓰지 않습니다.

| 옵션 | 용도 |
| --- | --- |
| `--work-dir PATH` | 도구·원본·빌드 결과를 둘 작업 폴더 |
| `--threads N` | 병렬 작업 설정 |
| `--batch-size N` | 한 번에 빌드할 모듈 수 |
| `--prepare-only` | 설치·다운로드·의존성 준비까지만 수행하고 전체 검증 빌드는 시작하지 않음 |
| `--status` | 기존 실행 상태 확인 |
| `--stop` | 현재 묶음 완료 후 중단 요청 |
| `--resume` | 중단 요청 해제 후 이어서 실행 |
| `--help` | 사용법과 전체 옵션 확인 |

예를 들어 실행 환경만 먼저 준비할 수 있습니다.

```bash
bash verify.sh --work-dir /data/lean-replay --prepare-only
```

## 결과 확인

`<work>`는 선택한 작업 폴더입니다.

| 파일 | 의미 |
| --- | --- |
| `<work>/bootstrap-status.json` | 설치·다운로드·환경 준비 결과 |
| `<work>/project/.verification-run/status.json` | 빌드 단계와 진행 상황, 완료 여부 |
| `<work>/project/.verification-run/axiom-audit.json` | 최종 정리 5개의 공리 검사 결과 |
| `<work>/project/.verification-run/logs/` | 묶음별 빌드와 최종 검사 로그 |

환경 준비가 끝났다는 기록은 전체 증명 빌드 완료를 뜻하지 않습니다. 빌드의 `full_build_completed`와 공리 검사의 `axiom_audit_passed`를 각각 확인해야 합니다. 공리 검사는 최종 정리가 `propext`, `Classical.choice`, `Quot.sound` 이외의 공리에 의존하면 통과시키지 않습니다.

**현재 이 도구가 수행하는 범위는 원본 입력 확인, 전체 빌드, 최종 정리의 공리 검사입니다.** 별도 커널에 의한 독립 재검증과 표준 정리와의 의미 일치 검토는 아직 구현하지 않았습니다. 다른 검증자가 공개한 성공 기록을 이 도구의 실행 결과로 표시하지 않으며, 모듈 빌드 완료율을 증명이 참일 확률로 해석하지 않습니다.

## 자동화 코드 테스트

다운로드 오류·해시 불일치·경로 검사와 빌드 실패·중단·재개·공리 검사 처리를 작은 오프라인 fixture로 테스트합니다. 아래 테스트는 대형 증명을 내려받거나 빌드하지 않습니다.

```bash
python3 -m unittest discover -s tests -v
```

GitHub Actions에서도 Ubuntu 22.04와 24.04에서 같은 테스트를 실행합니다. 전체 증명 검증을 GitHub의 기본 CI 작업으로 실행하지는 않습니다.

## 원본과 공개 범위

증명의 공로는 Levent Alpöge와 협력자에게 있습니다. 이 저장소는 실행 도구·자원 측정·문서를 다루며 원본 Lean 소스를 포함하지 않습니다. 실행 시 공식 배포처에서 내려받고 해시로 확인합니다.

[Lean 커뮤니티 저장소의 라이선스 안내](https://github.com/leanprover-community/riemann-hypothesis#license)는 원본 제출물이 라이선스 없이 공개됐다고 명시합니다. 원본과 제3자 구성요소에 이 저장소의 도구 라이선스가 적용되는 것으로 표시하지 않습니다.

- [저자의 공개 문서](https://alpo.ge/remark.pdf)
- [Lean 커뮤니티 검증 설정과 검토 노트](https://github.com/leanprover-community/riemann-hypothesis)
- [Andrew Sutherland의 독립 검증 기록](https://math.mit.edu/~drew/grh/)
- [LUMI 환경의 독립 실행 기록](https://github.com/aapolipponen/rh-lean-check-lumi)
