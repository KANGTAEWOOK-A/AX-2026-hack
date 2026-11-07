# Wind Recheck — 풍력 설비 AI 재검수 우선순위 MVP

제공된 풍력 결함 분석 모델이 **정상/미검출**이라고 판정한 이미지를 다시 볼 우선순위를 계산하고, 제한된 수량(K)에서 실제 누락 결함을 얼마나 포함시키는지 평가하는 웹 서비스입니다.

> **2026-11-07 확인:** 대회 서버 공개 배포 및 실제 AIStream 풍력 API 호출 성공. [공개 서비스](https://devopen-gpu.jntp-ax.or.kr/i/tUMhRXFYbSQqYsUc/). 실제 112장의 그룹 분리 탐색 실험을 완료했습니다. TEST 후보 4장·누락 결함 2장으로 작으며, 현재 규칙은 K=1·3에서 무작위보다 낮았습니다. 학습 누락 사례가 2건뿐이어서 학습 모델은 만들지 않았습니다. 자세한 결과는 `VALIDATION.md`와 `reports/final_metrics.json`에 있습니다. AgentStream 전용 챗봇의 실제 메모 분류와 공개·호출 권한 저장을 확인했습니다. API Key를 서버 비밀 설정에 등록했고 실제 REST 호출과 공개 웹의 분류·저장·재조회를 확인했습니다. 합성 메모 기능 점검이며 산업 현장 문장 정확도·효율 개선은 미검증입니다. 현재 결과로 현장 일반화 성능·비용 절감을 주장하지 않습니다.

## macOS (Apple Silicon / Intel)

Mac에서는 `Start_Wind_Recheck_Mac.command`를 더블클릭하면 첫 실행 시 가상환경 설치 후 웹 서비스가 자동으로 시작됩니다. Python 3.10~3.14이 필요하며 Python 3.12를 권장합니다. 자세한 내용은 `Mac_Quick_Start.md`를 참조하세요.

**주의:** Mac의 `localhost` 실행은 공개 URL 배포가 아닙니다. 실제 심사를 위해서는 별도의 해커톤 서버에 배포해야 합니다.

## 빠른 시작

```bash
cd wind_recheck_mvp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# .env에 본인 AIStream 풍력 predict URL 설정 (아래 참조)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

접속: http://localhost:8000 (또는 서버의 공개 URL). 사용: 업로드 → 추론 실행 → 검수 K 선택 → 검토 → 평가.

확인한 실제 풍력 URL은 `https://aiopen-serving.jntp-ax.or.kr/jntp-gpu/SV-e434a471/predict/wind`입니다. `AISTREAM_WIND_URL`에 전체 URL을 설정하고 요청은 `multipart`, 이미지 필드는 `file`로 사용합니다. 명세는 같은 서비스의 `/docs`에 있습니다. 응답은 `asset_type`, 이미지 크기, 전체 클래스 목록 `classes`, 실제 결함 목록 `defects`와 PNG 분할 마스크를 포함하며 confidence는 제공하지 않습니다. 확인한 풍력 구조에서 `defects=[]`를 후처리 미검출로 해석하고, 그 밖의 해석 불가 응답은 `unknown`으로 남깁니다. 응답 메타데이터는 `/api/response/{id}` 또는 검토창의 접힌 상세정보에서 확인할 수 있습니다.

### 한 번에 받아야 할 데이터

1. 대회 **에너지(A트랙) 풍력(Wind) RGB 원본 이미지**
2. 해당 이미지의 **정답 JSON**, 정상/결함 라벨이 명확한 샘플
3. 모델의 결함 클래스/정상 의미 및 실제 API `/docs`
4. 권장: 촬영 세션/장비/부위 정보(누수 방지와 분석용)
5. 선택: 결함 bbox, segmentation, severity(평가·시각화 단계에만 사용)

**공식 제공 데이터 또는 운영진이 명시적으로 승인한 자료만 사용합니다.** 이번 풍력 AI-Hub 자료의 사용 승인은 사용자가 확인했습니다(2026-11-07). 다른 외부 자료를 임의로 추가하지 않습니다. 태양광, VQA, 정비 비용/이력, 재배 기록은 이 MVP에 필요하지 않습니다.

## 대회 샘플 데이터 가져오기

방법 A: 웹에서 이미지 한 장을 직접 올린 뒤 AIStream 추론 → 정답이 없는 운영 화면 확인.

방법 B: `manifest.csv`가 들어 있는 ZIP을 웹에서 가져오기. 파일 구조 예시:

```
wind-test.zip
├── manifest.csv
└── images/
    ├── sample_001.jpg
    ├── sample_002.jpg
    └── sample_003.jpg
```

manifest.csv (필수 `image_file,truth`; 권장 `split,session,part`):

```csv
image_file,truth,split,session,part
images/sample_001.jpg,normal,train,session_A,BladeA
images/sample_002.jpg,defect,val,session_B,BladeA
images/sample_003.jpg,defect,test,session_C,BladeB
```

- `truth`는 **실제 정답 라벨**이고, 모델이 예측한 값이 아닙니다. `normal`/`defect`만 허용.
- `split=train|val|test|unassigned`; 평가(`/api/evaluate`)는 **test**만 사용합니다.
- 동일 촬영 세션·설비·원본 이미지 변형본은 같은 split으로 묶으세요. 이미지를 무작위로 나누면 누수가 발생할 수 있습니다.
- 실 샘플에 라벨 파일만 있을 경우 `python scripts/build_manifest.py --images <folder> --labels <folder> --out manifest.csv --zip wind_subset.zip --limit 100 --split unassigned`를 사용하세요. 실제 JSON에 `info.db_name`이 명확할 때만 정답으로 채웁니다. 미확인 레코드는 제외/보고합니다.
- ZIP 130MB 이하, 회당 300장 이하. 큰 자료는 테스트용 소규모 ZIP 여러 개로 나누세요.
- 업로드 후 API 추론에서 정상·미검출 후보가 나오는지 확인하세요.

## AI 오류 예측기 학습

`POST /api/train`은 실제 제공 모델이 `normal`로 판정한 **train split** 중, 정답이 `defect`인 항목 3개 이상과 `normal`인 항목 3개 이상을 포함하는 최소 12건이 있어야 학습됩니다. 학습 전에는 규칙 기반 우선순위만 적용합니다.

검증은 `test split`의 실제 정답에서만 수행합니다. `Capture@K`, `Precision@K`, `Remaining@K`를 **같은 입력 집합과 K**에서 무작위, 영상품질 규칙, (신뢰도 제공 시) 낮은 confidence 순과 비교합니다. 추가 학습 모델의 점수는 교정된 확률이 아니므로 `90% 고장 확률` 등으로 표현하지 마세요.

주의: 대회 제공 모델의 기존 학습 데이터와 사용자가 평가용으로 나눈 이미지가 중복되는지 별도 확인이 필요합니다. 이미 모델에 학습된 이미지를 시험에 사용한다면 미관측 현장 일반화 성능으로 발표할 수 없습니다.

## 데이터/성능 관련 주의

- 모델 응답에 confidence가 없으면 신뢰도 정렬 비교를 생략합니다.
- 실제 누락(모델 normal, 정답 defect)이 0개라면 Capture@K를 100%가 아닌 **평가 불가**로 표시합니다.
- JSON 라벨을 모델 추론 입력으로 사용하지 마세요. API에는 원본 이미지만 보냅니다.
- 원천 JSON의 `info.db_name=NormalDB/PositiveDB`는 **정답 식별 목적으로만** 쓸 수 있습니다. 정상·결함 판정은 실제 구조를 확인하고 결정해야 합니다.
- `annotations=[]`을 근거로 무조건 normal이라고 해석하지 않습니다.
- API 실패를 정상으로 저장하지 않습니다.
- 저장 모델(`data/ranker.joblib`)은 이 애플리케이션에서 직접 생성한 경우에만 로드하세요. 외부에서 받은 pickle/joblib은 위험합니다.
- 배포 공개 URL에는 기업 비밀 이미지, 개인정보, API 인증키 등을 노출하지 마세요.
- `.env`, `.pem`, `.key`, `data/` 전체가 Git에 포함되지 않게 `.gitignore`를 점검하세요. 이미 유출된 인증키는 재발급하세요.
- 발표에서 게임 느낌은 **문제 도입과 결과 공개 연출**로만 활용하고 실제 서비스 UI는 산업용으로 유지합니다.

## 테스트

```bash
pytest -q
```

실제 API와 데이터 없이 시연용 가짜 성능을 만들지 않도록, 기본 앱은 빈 상태로 시작합니다. `scripts/create_ui_fixture.py`는 테스트용 이미지와 "모의" 판정을 추가하지만, `origin=simulated`로 표시하며 실험 평가/학습 대상에서 자동 제외합니다. 대회 발표의 모델 성능 수치에 사용하지 마세요.

## 권장 발표 7분

0:00~0:45 “정상이라던 사진 안에 누락 결함이 있다면?” / 0:45~1:30 데이터·AIStream / 1:30~3:30 실제 추론+검수 K 설정 / 3:30~5:00 동일 K 성능 비교 / 5:00~6:15 현장 활용 / 6:15~7:00 한계와 최종 요약.

## 확인된 풍력 API 및 공개 경로

- 명세: https://aiopen-serving.jntp-ax.or.kr/jntp-gpu/SV-e434a471/docs
- 실제 POST URL: `https://aiopen-serving.jntp-ax.or.kr/jntp-gpu/SV-e434a471/predict/wind`
- 요청: `multipart/form-data`, 이미지 필드 `file`. confidence는 반환하지 않습니다.
- 응답의 `classes`는 전체 클래스 목록이고 실제 검출은 `defects`입니다. 확인한 풍력 응답 구조에서 `defects=[]`를 후처리 결함 미검출로 처리합니다.
- 대회 프록시: `.env`의 `WR_ROOT_PATH=/i/tUMhRXFYbSQqYsUc`. Mac 로컬 실행은 빈 값으로 둡니다.
- 현재 Mac Python 3.14.7에서 테스트가 통과했습니다. 새 Linux 환경은 Python 3.12를 사용합니다.
- `scripts/run_validation_experiment.py`는 기본적으로 계획만 작성하며, `--run`을 명시해야 실제 이미지를 등록하고 API를 호출합니다.
- 운영진의 AI-Hub 데이터 사용 승인을 사용자가 확인했습니다(2026-11-07). 제공 모델 학습 데이터와의 중복은 별도 미확인입니다.

API 응답 조회는 결함 목록·bbox 등을 유지하면서 큰 PNG base64 문자열을 생략합니다. 생략 여부와 원래 문자열 길이를 표시하며, 그 밖의 응답이 너무 크면 `truncated`로 명시합니다.

## AgentStream 검토 메모 보조

전체 이미지에서 AIStream 결함 미검출이고 부분 이미지에서 검출 클래스가 저장된 건은 별도의 검수 질문 챗봇으로 확인 질문을 만들 수 있습니다. 해당 챗봇의 호출 권한을 API Key에 연결하고 서버의 `AGENTSTREAM_QUESTION_CHATBOT_ID`에 UUID를 설정합니다. 서버는 이미지, 메모, 정답 라벨 없이 검출 영역과 클래스명만 전달하며, 저장된 검출 근거에 연결되는 질문만 표시합니다.

평가 화면에서는 4개 영역이 모두 성공한 TEST 이미지에 한해 기존 규칙과 부분 검출 우선순위를 동일한 이미지와 검수 예산으로 비교합니다. 적용 범위와 완료 건에 포함된 API 호출 수·응답 시간 합계를 함께 표시합니다. 일부만 완료된 경우 해당 표본에서의 탐색 결과로 해석합니다.

전용 챗봇과 서버 API Key를 `.env`에 설정합니다. 키는 조직 대표가 해당 워크스페이스에서 발급하고, 공개 챗봇의 CHAT_INVOKE 권한을 저장해야 합니다. 기존 키 입력은 `.venv/bin/python scripts/configure_agentstream.py`로 할 수 있습니다. 이 스크립트는 키를 만들거나 재발급하지 않으며 화면·로그에 키를 출력하지 않습니다.

실제 통신 확인은 `.venv/bin/python scripts/check_agentstream.py`를 실행합니다. 합성 메모 1예의 기능 점검이며 산업 데이터 성능으로 주장하지 않습니다.

웹 검토창에서 메모를 작성하고 ‘AgentStream으로 메모 정리’를 선택합니다. 원문을 문장으로 나눠 관찰·불확실성·후속 요청·담당자 언급을 분류합니다. 결과 확인 후 검토자가 완료·보류·대기를 직접 저장합니다. 새 API 대화는 요청마다 독립 생성되며 외부 session_id는 브라우저에 전달하지 않습니다. 익명 검토 세션·분류 소유자·메모 해시를 서버에서 검증하고, 서버 전체 시간당 60회·동시 2개로 호출을 제한합니다.

요청·응답 메모는 AgentStream의 일반 대화 보관 정책을 따를 수 있습니다. 이메일·휴대전화·일부 개인식별번호 패턴은 모델 전송 전에 생략하지만 모든 민감정보를 식별하는 보장은 없습니다. 화면에는 원문을 보존합니다. 결함·심각도·긴급도·고장 확률·검수 순위는 LLM에 맡기지 않습니다.
