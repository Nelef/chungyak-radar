# 청약 레이더

서울·경기 관심 지역 청약 공고(청약홈 + LH + SH·청년안심주택 게시판)를 주기적으로 수집해서
새 공고가 뜨면 텔레그램으로 즉시 알리고, 접수 시작·마감 D-7/D-1에
리마인더를 보내는 개인 NAS용 시스템.

- 새 공고 알림에 두 사람 상황(서울 당해 / 수도권 풀 / 규제지역 재당첨 제한 /
  신혼희망타운 수익공유 확인 / 혼인신고 전·후)에 맞는 행동 태그가 자동으로 붙음
- 웹 대시보드: `http://NAS_IP:8899` — 접수중/예정/마감 상태판

## 1. API 키 발급 (무료, 자동승인)

[공공데이터포털](https://www.data.go.kr) 회원가입 후 아래 두 개를 "활용신청":

1. **한국부동산원_주택청약 분양정보 조회 서비스** → 청약홈 APT·무순위 공고
2. **한국토지주택공사_분양임대공고문 정보** → LH 공공분양(뉴:홈, 3기 신도시, 신혼희망타운)

마이페이지에서 각각의 **일반 인증키(Decoding)** 를 복사.

### 통근시간 태그용 키 (선택, 역시 무료)

공고마다 "🚇 목적지 약 N분 · 환승 N회" 태그를 붙이려면 두 개 더:

1. **카카오 REST API 키** — [developers.kakao.com](https://developers.kakao.com) →
   애플리케이션 추가 → 앱 키의 REST API 키 (주소 → 좌표 변환)
2. **ODsay API 키** — [lab.odsay.com](https://lab.odsay.com) → 회원가입 →
   API 키 발급 (대중교통 길찾기, 무료 1,000회/일)

같은 주소는 캐시하므로 호출량은 공고당 1~2회 수준. 키가 없으면 통근 태그만
빠지고 나머지는 정상 동작. 목적지·기준시간은 `config.yaml`의 `commute`에서 조정
(기본 목적지·60분, 초과 시 ⚠ 표시. `exclude_over: true`면 초과 공고 자체를 제외).

**한계**: 신규 택지지구(3기 신도시 등)는 지번이 아직 없어 좌표 변환이 실패하거나,
현재 교통망 기준이라 개통 예정 노선(교산 3호선 연장 등)이 반영되지 않음.
초과 공고도 기본은 제외하지 않고 ⚠ 태그만 붙이는 이유.

## 2. 텔레그램 봇 만들기

1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 봇 이름 지정 → **토큰** 복사
2. 만든 봇에게 아무 메시지나 한 번 보내기
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열어 `chat.id` 확인
4. **둘이 같이 받으려면**: 두 사람 + 봇이 있는 그룹방을 만들고, 그룹에 메시지를
   보낸 뒤 같은 방법으로 그룹의 chat id(음수)를 사용

## 3. NAS 배포 (Synology Container Manager 기준)

```bash
# 프로젝트 폴더를 NAS에 업로드 후
cp .env.example .env      # 키·토큰 입력, 동작 확인 후 MOCK=0 또는 삭제
docker compose up -d --build
```

- 대시보드: `http://NAS_IP:8899`
- 데이터: `./data/radar.db` (컨테이너 재생성해도 유지)

Container Manager UI를 쓴다면: 프로젝트 → 생성 → 이 폴더 선택 →
`docker-compose.yml` 자동 인식.

## 4. 동작 확인 순서

1. `.env`에서 `MOCK=1` 상태로 기동 → 대시보드에 샘플 공고 3건이 보이면 파이프라인 정상
2. 텔레그램 토큰 넣고 `curl -X POST http://NAS_IP:8899/api/test-telegram` → 메시지 수신 확인
3. API 키 넣고 `MOCK` 지우고 재시작 → `docker compose restart`
4. 대시보드 우상단 **지금 수집** 으로 즉시 수집 가능

## 5. 필터 조정 (`config.yaml`)

- `mode: premarital` → **혼인신고 하는 날 `married`로 변경** (알림 태그 전환)
- `regions.gyeonggi_cities` → 관심 도시 추가/삭제 (현재: 부천·하남·과천·성남·광명·안양·의왕)
- `reminders: [7, 1]` → 리마인더 시점(일). 매일 08:30에 검사
- 수정 후 `docker compose restart`

## 구조

```
app/main.py     FastAPI + 스케줄러 (수집 주기 60분, 리마인더 08:30)
app/sources.py  청약홈·LH API 클라이언트 (+ MOCK 샘플)
app/boards.py   SH·청년안심주택 게시판 크롤러 (미리내집 등 임대 공고)
app/rules.py    지역 필터 + 상황별 태그 + 알림 메시지
app/notify.py   텔레그램 발송
app/db.py       SQLite (공고 저장, 알림 중복 방지)
web/index.html  대시보드
```

## 주의

- API 필드명은 기관 사정으로 바뀔 수 있음. 수집이 0건이면 `docker compose logs`로
  에러 확인 — 코드는 필드 누락 시 건너뛰도록 방어적으로 작성됨.
- 알림은 참고용. **신청 전 반드시 입주자모집공고 원문으로 자격·일정 확인.**
