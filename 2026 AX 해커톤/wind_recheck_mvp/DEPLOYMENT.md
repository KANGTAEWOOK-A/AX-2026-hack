# 서버 실행 및 운영

앱 루트에서 Python 3.12 가상환경을 만들고 의존성을 설치합니다. Ubuntu에서 `ensurepip` 오류가 발생하면 관리자 권한으로 `python3.12-venv` 패키지를 설치한 뒤 다시 생성합니다.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
scripts/run_server.sh
```

`.env`가 있으면 복사 단계를 생략하세요. 기본 포트는 8501이며 `WR_HOST`, `WR_PORT` 환경변수로 바꿀 수 있습니다. `.env`에는 모델 설정을 저장하며 스크립트가 이를 셸 코드로 실행하지 않습니다.

상태 확인:

```bash
curl --fail http://127.0.0.1:8501/api/health
```

이번 서버는 systemd가 PID 1인 환경이 아니라 runit으로 프로세스를 관리합니다. `/etc/service/wind-recheck`에 실행기를 등록했습니다. SSH 연결이 종료되거나 앱 프로세스가 종료되면 supervisor가 앱을 다시 실행합니다. 컨테이너 자체 삭제 시에는 서비스 등록과 가상환경을 복원해야 합니다.

```bash
sudo sv status /etc/service/wind-recheck
sudo sv restart /etc/service/wind-recheck
sudo sv down /etc/service/wind-recheck
```

로그는 `data/server.log`, DB는 `data/recheck.sqlite3`, 이미지는 `data/uploads/`에 저장됩니다. DB·이미지·로컬 모델은 함께 백업하세요. 대회 공개 HTTPS 주소에서 메인 화면·API·이미지 조회를 확인했고 실제 AIStream 호출에도 성공했습니다. 공개 프록시는 8501 포트에 연결되며 경로 접두사는 `.env`의 `WR_ROOT_PATH`로 지정합니다.

현재 서비스에는 사용자별 접근 제어가 없으며 접속자는 등록된 이미지와 검수 기록을 볼 수 있습니다. 공개 시연에는 공개가 허용된 데이터만 넣으세요.

# 다운로드 데이터 점검

완료된 자료는 VS/VL 및 TS/TL ZIP으로 저장되어 있습니다. 기존 `reports/validation_manifest.csv`, `reports/training_manifest.csv`는 명시적 라벨 검사를 거친 목록입니다. 초기 tar 파일은 현재 서버에 없으므로 완료된 ZIP 입력을 사용합니다.

```bash
.venv/bin/python scripts/run_validation_experiment.py --source-zip "<VS ZIP 경로>" --manifest reports/validation_manifest.csv --per-session 2 --run
```

`inspect_download.py`는 다운로드 중인 tar가 있을 때 원본을 변경하지 않고 완전히 수신된 구간만 읽는 보조 도구입니다. `reports/data_audit.json`에 클래스 수, 명시적 라벨 불일치, 이미지 파일명 중복 및 촬영 장소·날짜 그룹 중복을 기록합니다. 샘플은 정상/결함 각 1장으로, 기능 점검용이며 대표성 있는 평가 표본이 아닙니다.

생성한 `validation_smoke.zip`은 원본 이미지와 `manifest.csv`만 포함합니다. 정답 JSON이나 VQA 설명은 AI 추론 입력에 포함하지 않습니다. 모든 manifest는 `split=unassigned`로 생성됩니다. 현재 Training/Validation에는 같은 장소·날짜 36개 그룹이 모두 겹치므로, 이 분할을 그대로 촬영 그룹 독립 평가라고 주장할 수 없습니다. 파일명이 서로 다르더라도 유사 이미지 또는 같은 설비일 수 있으며 추가 확인이 필요합니다. 대회 데이터 사용 승인은 사용자가 확인했습니다. 제공 모델의 기존 학습 자료 중복은 미검증입니다.

테스트의 모의 응답 검증과 실제 AIStream 통신·모델 성능 검증은 별개입니다. 실제 API 명세·통신을 확인했고, 그룹 분리된 70장 파일럿과 학습 그룹에 한정한 추가 42장 수집을 완료했습니다. 112건 모두 최종 추론 성공이며 학습 최소 양성 수에 미달해 규칙 모드로 운영합니다.

## 2026-11-07 공개 배포 확인

공개 주소: https://devopen-gpu.jntp-ax.or.kr/i/tUMhRXFYbSQqYsUc/

실제 실행 경로는 `/data/hackathon/wind_recheck_mvp`입니다. runit 서비스에서 `WR_PORT=8501`을 지정하고 `.env`에 `WR_ROOT_PATH=/i/tUMhRXFYbSQqYsUc`를 설정했습니다. 브라우저에서 메인 화면, CSS/JS, 이미지 조회, 실제 미검출 후보의 검수 목록 생성을 확인했습니다. 최초 70장의 실제 추론과 원래 평가 분할의 비교를 완료했습니다. 추가 42장을 포함한 최종 112건 집계는 `reports/final_metrics.json`에 있습니다.

서버 수정 전 백업: `/data/hackathon/wind-recheck-backups/`. 백업에는 비밀 설정이 포함될 수 있으므로 Git 저장소나 공개 ZIP에 포함하지 않습니다.

평가 진행 상태: `reports/pilot_experiment.json`, 로그: `reports/pilot_experiment.log`. 이 실행은 촬영 그룹을 train/val/test로 분리하고 정답에 무관한 파일명 해시 순서로 표본을 선택합니다. 결과는 탐색적 파일럿이며 제공 모델 원래 학습 자료와의 중복은 확인되지 않았습니다.

## 단계별 운영 명령과 완료 기준

| 단계 | 명령 또는 조작 | 정상 결과·완료 기준 | 오류 대응 |
|---|---|---|---|
| Mac | `bash Start_Wind_Recheck_Mac.command` | localhost 화면과 `/api/health` 응답 | 의존성·가상환경 확인, 실행 중 터미널 유지 |
| SSH | `ssh ff9ebb72-6ae84c12.hackathon` | ubuntu 셸 접속 | 새 PEM 경로·권한과 GPU 게이트웨이 확인 |
| 웹 운영 | `sudo sv status /etc/service/wind-recheck` | run 상태 | `data/server.log` 확인 후 `sudo sv restart /etc/service/wind-recheck` |
| 서버 상태 | `curl --fail http://127.0.0.1:8501/api/health` | ok=true; URL 설정 여부 표시 | 포트·프로세스·.env 확인. 설정 여부는 실제 모델 통신 성공과 구분 |
| 모델 상태 | `curl --fail https://aiopen-serving.jntp-ax.or.kr/jntp-gpu/SV-e434a471/health` | ready, wind 포함 | 429는 Retry-After를 따르고 대기 초과 직후 중복 호출을 피함 |
| 실제 평가 | `reports/pilot_experiment.json` 상태 확인 | state=completed, 계획 표본 수와 처리 수 일치 | state=failed이면 error와 로그를 확인하고 동일 표본으로 재개 |
| 결과 확정 | `.venv/bin/python scripts/finalize_experiment.py` | final_evaluation.json·final_metrics.json 생성 | 미완료 실험은 최종 보고서 생성 거절 |

GitHub 원격 저장소는 별도 계정·저장소 정보가 필요합니다. `.env`, PEM, data/, 가상환경, 다운로드 데이터, 발표 빌드 폴더는 소스에 포함하지 않습니다.
