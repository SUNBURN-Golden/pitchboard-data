# Pitchboard 데이터 기반 고도화 — 설계안 (Phase 1)

- 작성: 2026-10-27 (KST) / 상태: **설계 초안, 구현 없음**
- 범위: 데이터 수집·온톨로지·상태 이력·운영 자동화. **프론트엔드 제외.**
- 이 문서가 건드리지 않은 것: 실행 중인 데몬(`daemon.sh`), `var/inbox.sqlite3`, 사이트 repo, 배포. (DB는 읽기 전용으로만 열람)
- 부속 파일: `source_registry_draft.csv` (현재 seeds 76개 도메인 → 소스 레지스트리 초안)
- 순서: **질문 → 온톨로지 → 수집·검증 → 현실 상태 추적** (준태님 지시)

---

## 0. 현재 상태 요약 (2026-10-27 읽기 전용 점검 기준)

| 항목 | 현황 |
| --- | --- |
| 수집 DB | `crawler/var/inbox.sqlite3` (SQLite/WAL, `schema_migrations` 3개). items 139 = `outlet_guidelines` 77 + `source_lead` 58 + **`call` 4** |
| 근거 구조 | `source_revisions`(143) → `evidence`(89, ≤280자, heading+paragraph_index) → `claims`(283: genre 85, pay_rate 85, submission_category 89, 나머지 deadline/eligibility/rights/fee/simultaneous/ai_policy 각 4) |
| 충돌 | `conflicts` 4건(`unconfirmed_specificity`), `review_decisions` 47건 |
| 작업 기반 | `jobs`(fetch/normalize/recheck, lease·retry_wait·dead_letter), `daily_budget`(후보 500/요청 200), `domain_state`, `robots_cache`, `policy_registry/receipts` |
| 작업 큐 상태 | fetch `retry_wait` 53, `dead_letter` 5, recheck `retry_wait` 47 |
| 접근 중단 | 7개 도메인 `access_review`로 정지 (아래 §6.3) |
| 사이트 데이터 | `pitchboard-site/data`: outlets 96(모두 문자열 위주 필드: `pays`, `word_count`, `status`), calls **3**, watch 1 |

**솔직한 한계**: 지금 "공모 회차(call)" 데이터는 사실상 4건(수집함)·3건(사이트)뿐입니다. 온톨로지는 크게 만들 수 있지만, 질문에 답할 *데이터*는 §7의 수집 확장 전까지 얇습니다. 또한 현재 `items.stage`(candidate→source_checked→…)는 **파이프라인 진행 단계**이고, 공모의 **현실 상태(open/closed…)** 를 담는 필드는 없습니다. 이 둘의 분리가 이번 설계의 핵심입니다.

기존 설계 계약 `SPEC_V1_INBOX.md`의 원칙은 그대로 유지합니다: 자동 게시 금지(`auto_publish=0` CHECK), 원문 스냅샷 미보관(280자 발췌만), 결측은 unknown(0/무료/허용 아님), 범위(scope) 분리(일반 투고 근거가 리뷰/피치 근거로 둔갑하지 않음), 충돌 보존.

---

## 1. 대표 질문 (서비스가 답해야 할 것)

핵심 질문: **"내 조건에 맞고 아직 지원 가능한 기회는?"** 를 아래 구체 질문으로 쪼갭니다. 각 질문은 `답 + 근거 + 확인 시점 + 확신 수준`을 함께 반환해야 하며, 판정은 3값(`yes / no / unknown`)입니다.

| # | 질문 | 필요 데이터 | 판정 규칙 요약 |
| --- | --- | --- | --- |
| Q1 | 오늘(KST) 기준 **지금 지원 가능한** 기회는? | 회차 상태, 마감 구간, 시간대, 확인 시점 | 상태∈{open, extended, standing} AND 마감 미경과. 시간대 불명이면 `maybe`(§4.3) |
| Q2 | **서평(book review)/비평 에세이**만 받는 곳 중 열린 곳은? | 장르·투고 유형(scope) | 범위 일치 증거 있는 것만 `yes`. 일반 투고 근거는 `unknown` |
| Q3 | **참가비 없이** 지원 가능한가? | 참가비 term | "무료" 명시(`explicitly_none`)만 `yes`. 언급 없음 ≠ 무료 → `unknown` |
| Q4 | **원고료 ≥ X (단어당/건당)** 인 곳? | 보상 term(통화·단위·범위) | 통화·단위 정규화 후 비교. 환율 기준일 기록. 단위 불명이면 `unknown` |
| Q5 | **한국 거주자/비미국 작가**가 지원 가능한가? | 자격 term(지역·언어·국적·회원·연령) | 자격 조건 **미확인 ≠ 제한 없음**. "전 세계 가능" 명시만 `yes` |
| Q6 | 이 마감이 **연장/취소/변경**됐나? 언제, 무엇이 바뀌었나? | 상태 이력, 사실 이력 | 원문 수정 감지 시 이전 값 보존 + 변경 사유/근거 |
| Q7 | **동시투고 허용 / AI 사용 정책 / 권리(독점 기간·재게재)** 조건은? | 정책·권리 term | 각 term별 근거. 상충 시 `conflict` |
| Q8 | **7일 안에 마감**되는데 아직 확인이 안 된(오래된) 것은? | 마감, `fresh_until` | 확인 시점이 TTL 넘으면 "재확인 필요"로 표시 |
| Q9 | 같은 매체의 **서로 다른 회차/창구**(예: 4월·10월 reading window)를 구분해 주나? | 회차 키, 매체-회차 관계 | 회차 단위 ID. 이름 유사만으로 병합 금지 |
| Q10 | 이 답변은 **어떤 출처의 어느 문장**에 근거하나, 출처끼리 충돌하나? | 증거 위치, 충돌 그룹 | 모든 응답 필드에 evidence 링크. 충돌은 검수 대기 버킷 |

**응답 버킷** (Q1~Q5 공통, 결정적으로 분리):
1. `확정 적합` — 모든 조건 `yes` 이고 해당 사실이 verified_fact/신선함
2. `적합 가능(미확인 있음)` — 일부 조건 `unknown` (어느 조건이 미확인인지 명시)
3. `검수 필요(충돌)` — 결정 필드 충돌
4. `부적합 / 마감` — 별도 보관(삭제하지 않음)

---

## 2. 온톨로지

### 2.1 개체(Entity)와 관계

```
Organizer(주최자) ─┬─< Outlet(매체)            Outlet ─< CallEdition(공모 회차)
                   └─< CallEdition(주최자가 직접 여는 공모)
CallEdition ─< EligibilityRule ─ ScopeRef(장르/투고유형)
CallEdition ─< CompensationTerm | FeeTerm | RightsTerm | PolicyTerm | DeadlineTerm
CallEdition ─< SubmissionChannel(Submittable/이메일/폼)
CallEdition ─< StatusEvent(상태 이력)
Source ─< SourceRevision ─< Evidence ─< Fact(주장)
Fact ─> subject(Outlet/CallEdition/…) ; Fact ─< ConflictGroup ; Fact ─> supersedes Fact
```

| 개체 | 설명 | 안정 ID | 별칭/식별키 |
| --- | --- | --- | --- |
| `Organizer` | 공모를 여는 조직(재단, 대학, 출판사, 매체 모회사) | `org_<ULID>` | 표기명, 약칭, 도메인 |
| `Outlet` | 서평·비평을 싣는 매체 (현 `entities` 79, 사이트 outlets 96) | `out_<ULID>` | 사이트 numeric `id`, 표기 변형, 한글명, 도메인 목록 |
| `CallEdition` | **회차 단위 공모/리딩 윈도우/상시 투고창구**. 회차 키 = (outlet/organizer, `edition_key` 예 `2026-10`, `submission_kind`) | `call_<ULID>` | 사이트 call `id`(예 `brick-oct-2026`), 원문 제목 |
| `ScopeRef` | 장르·투고 유형 분류 (book review, critical essay, interview, fiction, poetry …) 통제 어휘 | `scope_<slug>` | 동의어 |
| `EligibilityRule` | 지역/국적/거주, 언어, 연령, 회원, 경력, 정체성 등 **조건 1개 = 1행** | `elig_<ULID>` | — |
| `CompensationTerm` | 금액 최소/최대, 통화, 단위(per_word/flat/per_piece/copies), 지급 시점 | `comp_<ULID>` | — |
| `FeeTerm` | 참가·투고·읽기 수수료, 통화, 면제 조건 | `fee_<ULID>` | — |
| `RightsTerm` | 최초게재권, 독점 기간, 재게재, 저작권 귀속, 동시투고 허용 | `rts_<ULID>` | — |
| `PolicyTerm` | AI 사용, 번역, 이미 발표된 글, 응답 소요 등 | `pol_<ULID>` | — |
| `DeadlineTerm` | 열림/마감/결과 발표 시각 구간. 시간대 포함 | `dl_<ULID>` | — |
| `SubmissionChannel` | 투고 방법/URL(Submittable 등) | `chn_<ULID>` | — |
| `Source` | 수집 가능한 위치(URL/피드/SNS 계정) + 레지스트리 속성 | `src_<ULID>` (기존 `sources.id` 재사용) | canonical URL, 리다이렉트 별칭 |
| `SourceRevision` | 특정 시점에 가져온 내용 버전(해시) | 기존 `source_revisions.id` | content_hash |
| `Evidence` | 원문 위치 + 발췌(≤280자) | 기존 `evidence.id` 확장 | — |
| `Fact` | **모든 속성값 1건**(아래 2.2) | `fact_<ULID>` | — |
| `StatusEvent` | 회차 상태 전이 1건 | `st_<ULID>` | — |

ID 규칙: ULID 기반 불변 ID. 기존 정수/UUID는 `alias` 테이블로 보존(`legacy:site_outlet:1`, `legacy:inbox_entity:<uuid>`). **병합은 삭제가 아니라 `retired_to` 이벤트**(기존 `dedup_clusters.retired_to` 방식 유지)이며 되돌릴 수 있어야 합니다.

### 2.2 팩트 모델 (모든 정보 한 건 = Fact)

속성을 컬럼에 덮어쓰지 않고, 모든 값은 `fact` 행으로 저장합니다(이중 시간축: 현실 적용 시점 + 기록 시점).

| 필드 | 설명 |
| --- | --- |
| `fact_id`, `subject_type`, `subject_id` | 대상 |
| `predicate` | 통제 어휘: `deadline.closes_at`, `fee.amount`, `eligibility.region`, `status.declared` … |
| `scope_ref` | 어떤 투고 유형에 적용되는지(없으면 `unspecified` — 다른 scope의 근거로 쓸 수 없음) |
| `value_json` | 정규화된 값(금액 {min,max,currency,unit}, 날짜 구간 등) + `value_raw`(원문 표기, ≤280자) |
| **`value_state`** | `stated` / `explicitly_none`("무료", "제한 없음"이 *원문에 명시*) / `not_stated_in_inspected_scope`(해당 페이지를 검사했으나 언급 없음) / `not_inspected` |
| **`epistemic`** | `verified_fact` / `source_claim` / `ai_inference` / `unverified` |
| `evidence_id` + `evidence_locator` | 근거(revision_id, heading, paragraph_index, char_start/end, quote_hash) |
| `source_id`, `source_authority` | `official_primary` / `attributed_secondary` / `discovery_lead` / `unknown` (기존 값 재사용) |
| `collected_at` | 수집(관측) 시각, UTC ISO-8601 |
| `effective_from`, `effective_to` | **현실에서 적용되는 기간**(원문이 말하는 기간. 예: "2026-10-01~10-31 창구") — 모르면 NULL(=미상, 현재라는 뜻 아님) |
| `recorded_at`, `superseded_at`, `superseded_by` | 이 시스템이 알게 된/대체된 시점 (거래 시간) |
| `verified_at`, `verifier`, `fresh_until` | 검증 시각/주체/신선도 만료 |
| `conflict_group_id` | 충돌 묶음 |
| `correction_of`, `correction_reason` | 정정 이력 |
| `rights_id` | 수집·이용 권한(레지스트리 규칙 스냅샷 참조), `retain_until` |
| `extractor`, `extractor_version`, `run_id` | 생성 주체(규칙/모델/사람) |

**`epistemic` 정의와 승급 규칙** (자동으로 `verified_fact`를 만들 수 있는 경로는 하나뿐):
- `unverified`: 후보/리드(제3자·SNS·검색 발췌). 기본값.
- `source_claim`: 출처가 그렇게 말함(원문 위치 있음). 출처가 공식이어도 검증 단계를 통과하기 전엔 여기.
- `verified_fact`: **검증 단계 통과** = (a) 해당 주체·scope에 대한 `official_primary` 출처, (b) 저장된 발췌가 revision 해시에서 재확인됨, (c) 결정적(비-AI) 파싱이 값과 일치, (d) 충돌 없음, (e) `fresh_until` 이내. 하나라도 깨지면 자동 강등(+이벤트 기록).
- `ai_inference`: 모델/휴리스틱이 만든 값. **어떤 경우에도 매칭을 `yes`로 만들지 않음**(질의 시 `unknown` 취급, 옵션으로 "추론 포함" 표시 가능). 원문에 없는 관계를 확정 데이터로 넣지 않음.

**결측 처리 원칙 (핵심)**: 조건을 못 찾았을 때 저장하는 것은 "없음/무제한"이 아니라 `value_state=not_stated_in_inspected_scope`(또는 `not_inspected`) 입니다. 이는 **검사한 revision·scope에 대해서만** 기록할 수 있습니다(기존 `inspections` 테이블 개념 재사용). 질의에서 이 값은 항상 `unknown`.

### 2.3 시간 필드와 규칙
- 저장은 UTC ISO-8601(+원문 시간대 `tz_source`: `explicit`/`inferred_venue`/`unknown`). 기존 `REAL` epoch 컬럼은 마이그레이션 시 변환(§8).
- 마감은 **구간**으로 저장: `closes_at_earliest`, `closes_at_latest`. 원문이 "Oct 31"만 말하고 시간대가 없으면 그날 끝(23:59:59)이 UTC+14 기준으로 오는 시각(가장 이른 마감)부터 UTC-12 기준으로 오는 시각(가장 늦은 마감)까지를 최대 불확실 구간으로 저장 + `tz_source=unknown`. 서비스 표시는 KST 변환.
- `effective_from` 미상 ≠ 지금부터. NULL은 "미상".
- 사이트 `last_checked`는 `collected_at`의 최신값 투영일 뿐 별도 진실 소스가 아님.

### 2.4 검증 규칙 (`V-xx`, 위반 시 저장 거부 또는 강등)
| ID | 규칙 |
| --- | --- |
| V-01 | 모든 Fact는 `source_id`, `collected_at`, `epistemic`, `value_state` 필수. `value_state=stated`면 `evidence_id` 필수 |
| V-02 | `verified_fact`는 §2.2 (a)~(e) 모두 충족 필요. 위반 시 DB 트리거/검증기가 거부 |
| V-03 | `explicitly_none`은 원문 발췌에 "free/no fee/open to all" 류 명시 패턴이 있을 때만. 없으면 `not_stated…` |
| V-04 | scope 격리: Fact의 `scope_ref`와 다른 scope 질의에 사용 금지(`unspecified`는 어떤 scope의 `yes` 근거도 못 됨) |
| V-05 | 금액은 통화·단위 없으면 정규화 불가 → 값은 `value_raw`만, 비교 질의에서 `unknown` |
| V-06 | 날짜 파싱 실패/모호(예: 04/05) 시 두 해석을 모두 보존하거나 `unverified`; 임의 선택 금지 |
| V-07 | 동일 (subject, predicate, scope)에 서로 다른 값의 활성 Fact가 둘 이상이면 `conflict_group` 자동 생성, 둘 다 보존, 순서·권위로 덮어쓰기 금지 |
| V-08 | Fact는 UPDATE 금지(append-only). 변경은 새 Fact + 이전 `superseded_at/by`. 정정은 `correction_of` + 사유 |
| V-09 | AI/휴리스틱 생성 값은 `ai_inference` 이하만 가능. 관계(Outlet-Organizer 등)도 원문 증거 없이는 확정 관계 불가 |
| V-10 | 증거 발췌 ≤280자, 해시 검증, 개인정보 제거(기존 sanitizer v3 이후 단계 유지) |
| V-11 | 마감 구간은 `latest ≥ earliest`, `closes_at ≥ opens_at` |
| V-12 | `fresh_until` 경과한 `verified_fact`는 질의 시 "재확인 필요" 표시(자동 삭제 아님) |
| V-13 | 접근 불가 소스(`permission=not_collectable`)에서 온 신규 Fact 거부(과거 Fact는 보존, 신선도만 만료) |
| V-14 | 상태 전이는 §3의 허용표 밖이면 거부; 모든 전이는 근거(증거 또는 `system_clock`) 기록 |
| V-15 | 회차 병합은 자동 금지(같은 이름 ≠ 같은 회차). 후보 쌍만 만들고 검수 후 병합 |

---

## 3. 상태 기계 (CallEdition)

저장 상태와 **파생 상태**를 분리합니다. 저장 상태는 증거가 있는 이벤트로만 바뀝니다.

### 3.1 저장 상태
`candidate`(리드, 미검증) → `announced`(존재 확인, 아직 열리지 않음) → `open` → `extended`(마감 연장된 open) → `closed` / `cancelled`; 상시 창구는 `standing`(마감 없음/롤링); 상태를 확정 못 하면 `status_unknown`; 중복·대체는 `superseded`; 장기 보관은 `archived`.

| 전이 | 허용 조건(가드) | 근거 |
| --- | --- | --- |
| candidate → announced/open/standing | 공식 출처 Fact로 존재와 기간/방식 확인 | 증거 필수 |
| announced → open | 원문 "open"류 주장 **또는** `opens_at` 경과(시스템, 파생 근거로 표기) | 증거 or clock |
| open → extended | 마감 Fact가 **더 늦은 값**으로 교체(원문 변경) | 이전/신규 마감 모두 이력 보존 |
| open/extended → closed | (a) 원문이 closed 명시, 또는 (b) `closes_at_latest` 경과 → `closed(basis=deadline_passed)` (출처 확인 전까지 표기) | 증거 or clock |
| open/announced/extended → cancelled | 원문이 취소 명시 | 증거 필수 (시계 단독 불가) |
| closed → open/extended | **재오픈 증거**(마감 연장 공지, 정정) — 이전 closed 이벤트는 정정으로 표시, 삭제 금지 | 증거 필수 |
| any → status_unknown | 출처 접근 불가/원문 삭제(404) 후 `fresh_until` 경과 | 시스템 |
| any → superseded | 같은 회차로 판정된 대체(검수 승인) | review_decision |
| closed/cancelled → archived | 보관 정책 경과 | 시스템 |

The daemon's state-update stage also rechecks existing announced/open/extended/standing
editions against the current non-writing promotion gates. It uses current-version
full-page labels for the latest stored content, or replays that content's retained
evidence excerpts and headings. Only an explicit rejection of every stated backing
basis closes the edition, via an evidence-based `correction` event recording the rule
versions and source/raw/revision/fact/evidence (and cached label) IDs. `closed` here
corrects the prior open-call classification; it does not assert source cancellation
or a passed deadline. Facts are retained. Missing/ambiguous evidence, changed content
without a current rejection, or a surviving eligible basis keeps the edition.

금지: `cancelled → open` 직접 전이(새 회차로 생성), 증거 없는 수동 상태 변경, 시간만으로 `cancelled` 판정.

### 3.2 파생 판정 `applicable_now` (질의 시 계산, 저장 안 함)
`yes`(상태∈{open,extended,standing} 이고 `now < closes_at_earliest`) / `maybe`(마감 구간 안에 now 포함 → 시간대 불확실) / `no`(closed·cancelled 또는 `now > closes_at_latest`) / `unknown`(status_unknown, 또는 `fresh_until` 경과로 재확인 필요). 항상 `as_of`(마지막 확인 시각)와 함께 반환.

### 3.3 변경 → 검색 결과 반영 (원문 수정 전파)
1. 변경 감지 단계가 새 revision 해시를 발견 → 변경 이벤트 `source_changed(source_id, old_rev, new_rev)` 발행(outbox).
2. 추출·검증 후 Fact diff: 값이 바뀐 Fact는 이전 Fact를 `superseded` 처리하고 신규 Fact 추가, 이전 값은 이력에서 조회 가능.
3. 영향받는 `CallEdition`만 재계산(`recompute_scope = call_id`). 결과는 서비스 투영 테이블 `pb_search_projection`에 **버전 포함 UPSERT** (`projection_version`, `source_revision_ids`, `as_of`). 삭제된/철회된 Fact는 투영에서 제거(톰스톤 기록).
4. 품질 점검 단계가 "원문 변경 후 N분 안에 투영이 신규 값을 반영했는가"(freshness SLA)와 "투영의 모든 값이 활성 Fact를 가리키는가"를 확인. 실패 시 실패 큐.
5. 서비스는 투영만 읽음(공유 접근 금지). 검색 인덱스 갱신은 투영 버전 이벤트를 구독 — **이번 범위는 투영 테이블/JSON 세대(`var/exports` 방식 재사용)까지**, 프론트는 제외.

---

## 4. 데몬 단계(Stage) 계약

공통 규약(모든 단계):
- **단일 실행 잠금**: 프로세스 `flock`(기존 `daemon.lock`) + DB 리스(`jobs.lease_*`)로 단계·대상별 중복 실행 방지. 멱등 키 = `(stage, subject_id, input_digest)`; 같은 키 재실행은 no-op.
- **체크포인트**: 단계 종료 시 `stage_run(run_id, stage, subject_id, input_digest, output_digest, status, started/finished, cost)` 기록. 재개는 *마지막으로 성공한 단계의 output*에서 다음 단계부터.
- **제한된 재시도**: 단계별 `max_attempts`(기본 3), 지수 백오프 + 지터, 4xx(401/403/404/410)는 재시도 안 함 → 정책 판단 큐로, 429/5xx만 재시도. 소진 시 `dead_letter`.
- **실패 큐**: `failure_queue(stage, subject_id, error_code, first_seen, attempts, resume_from)`; 운영자 처리 전 자동 재투입 금지(단, 일시 오류는 다음 윈도우 1회 자동 재투입). 
- **비용 상한**: 일/단계별 — 요청 수(현 200/일 유지), 후보 수(현 500/일), 모델 호출 토큰·금액(기본 **0**, 별도 승인 시에만), 실행 시간. 상한 도달 시 단계는 `deferred`로 종료(실패 아님).
- **정지 스위치**: 도메인 `halted`, 전역 `kill_switch` 파일; 둘 다 확인 후 시작.
- **보고**: 정상은 조용히, 시작·실패·상한 도달만 보고(토큰 절약 원칙).

| # | 단계 | 입력 → 출력 | 체크포인트 키 | 실패 시 재개점 | 비고 / 기존 재사용 |
| --- | --- | --- | --- | --- | --- |
| 1 | **소스 발견** | 레지스트리·sitemap·RSS·공식 사이트 내 링크 → 신규 `Source` 후보 | source canonical URL | 해당 소스부터 | 기존 `collectors/auto_discovery.py`, `collector_checkpoints` |
| 2 | **증분 수집** | 소스 → `SourceRevision`(해시, 정규화 텍스트 메모리 처리, 발췌만 저장) | (source_id, ETag/Last-Modified/해시) | 소스 단위 | `fetch_policy.py`(robots, DNS/peer 검사, 도메인 간격), 304/ETag 사용 |
| 3 | **변경 감지** | 이전/신규 revision → `source_changed` 이벤트(변경 구간 위치) | (old_rev,new_rev) | 쌍 단위 | 변경 없으면 이후 단계 생략(비용 절감) |
| 4 | **정보 추출** | 변경된 구간 → 후보 Fact(`source_claim`/`unverified`) | (revision_id, extractor_version) | revision 단위 | 규칙 기반(`extract.py`) 우선; 모델은 옵션·비용 상한 하에서 `ai_inference`만 |
| 5 | **동일 대상 식별** | 후보 Fact → subject(Outlet/CallEdition) 매핑 | (fact 후보 digest) | 후보 단위 | 정확 매칭(도메인·정규화명·회차키) 자동, 유사 매칭은 `semantic_pairs`처럼 **검수 후보만** |
| 6 | **근거 검증** | Fact+evidence → epistemic 판정, 충돌 그룹 | (fact_id, rule_version) | Fact 단위 | V-01~V-15 적용, `verified_fact` 승급은 여기서만 |
| 7 | **상태·이력 갱신** | 검증된 Fact diff → 새 Fact append, `StatusEvent`, 이전 Fact `superseded` | (call_id, input fact set digest) | 회차 단위, 트랜잭션 | append-only. 시계 기반 전이(deadline_passed)도 여기서 |
| 8 | **서비스 결과 재계산** | 변경된 회차 → `pb_search_projection`, 후보 매칭 캐시 | (call_id, projection_version) | 회차 단위 | 투영만 서비스가 읽음. 자동 게시 없음(`publish_pending`은 로컬 제안으로만) |
| 9 | **품질 점검** | 전체/표본 → 지표·경고 | (check_id, day) | 점검 단위 | 아래 지표 |

**품질 지표(일 1회)**: 소스별 마지막 성공 수집 경과, `fresh_until` 경과 Fact 비율, 증거 해시 불일치 수, `verified_fact` 중 근거 없음(0이어야 함), 충돌 미해결 수/경과일, 파이프라인 정체(단계별 큐 길이), `dead_letter` 수, 투영-활성 Fact 불일치(0이어야 함), "시드 0건 수집" 같은 **빈 실행 감지**(지난 실행에서 신규/변경 0이 N회 연속이면 경고 — 10/24~27 시드 미설정 사고 재발 방지), 일 상한 도달.

단계 실행 주기(제안): 1~3 일 1회(06:00 KST, 기존 스케줄 유지), 4~8 변경 있을 때 즉시 연쇄, 9 일 1회. 데몬 자체는 지금 방식(`daemon.sh`)을 **수정하지 않고** 새 단계 러너를 별도 진입점으로 추가한 뒤 전환(§8).

---

## 5. 서비스 간 분리 (공유 vs 분리)

| 구분 | 공유(5개 서비스 공통 라이브러리) | Pitchboard 전용(분리) |
| --- | --- | --- |
| 코드 | 작업 큐/리스, 예산·상한, 도메인 간격, robots/정책 캐시, 체크포인트, 소스 레지스트리 스키마, sanitizer | 온톨로지 스키마, 추출 규칙, 검증 규칙 V-xx, 상태 기계, 투영 |
| 데이터 | **인프라 DB** (`infra.sqlite3`: jobs, stage_run, failure_queue, budgets, domain_state, robots_cache, policy_*) | **서비스 DB** (`pitchboard_kg.sqlite3`: 2장의 개체·fact·status_event·projection) |
| 접근 | 공통 라이브러리 API | Pitchboard 저장소 계층(`pb_repo`)만 서비스 DB를 읽고 씀. 다른 서비스 봇은 접근 불가(파일 권한·경로 분리) |
| 연결 | 인프라 → 서비스: `evidence_event` 인입 API(이벤트에는 ID·해시·발췌만) / 서비스 → 인프라: 재수집·재검증 요청 job |

초기에는 공통 라이브러리를 이 repo 안의 `common/` 패키지로 두고, 다른 서비스가 쓰기 시작하면 별도 repo로 분리합니다(§9).

---

## 6. 소스 레지스트리

### 6.1 레코드 스키마 (`source_registry`)
`source_id`, `canonical_url`, `host`, `source_type`(official_guidelines / sitemap / rss / aggregator / directory / social_lead / manual_clip), `authority`(official_primary 등), `owner_entity_id`, **`collectable_fields`**(예: status, deadline, fee, pay, genre, eligibility, rights, policy), **`unsupported_scope`**(명시: 로그인 필요, JS 렌더링 필요, 접근 차단, 약관 금지 등 + 사유), **`permission`**(`allowed_public` / `allowed_conditional` / `manual_only` / `not_collectable`), `permission_basis`(robots 버전, ToS 확인일, 승인자), `refresh_cadence`(예: 24h/7d), `retention`(발췌 보관일, 원문 보관 여부=기본 없음), `rate_policy`(도메인 간격), `last_success_at`, `last_review_at`, `notes`.
`permission`이 `not_collectable`이면 단계 2는 실행하지 않고(V-13), 사유와 검토일을 남깁니다.

### 6.2 현재 seeds 기반 초안 (전체는 `source_registry_draft.csv`, 76개 도메인)
| 구분 | 수 | 제안 permission | 비고 |
| --- | --- | --- | --- |
| 공식 투고/가이드라인 페이지(영어 매체) | 68 | `allowed_public` | robots·도메인 간격 준수, 24h~7d 갱신(마감 가까운 회차는 단축). 필드: status, deadline, fee, pay, genre, eligibility, rights, simultaneous, ai_policy (실제 추출 성공 필드는 소스별로 레지스트리에 기록; 현재 claims는 genre/pay/category 위주, 나머지는 각 4건) |
| 접근 검토 중단(§6.3) | 7 | `not_collectable` | 아래 |
| Bluesky 공식 계정 게시물(Strange Horizons) | 1 | `allowed_conditional` | 공개 읽기 한정, 링크는 리드이며 공식 사이트로 확인 전엔 `unverified/source_claim` |
| Instagram | 0 (신규 등록) | `manual_only` | §6.4 |

주기·보관 기본값: 공식 페이지 24h(최소) ~ 7d, 발췌 보관 365일(정정 이력은 영구, 발췌 본문은 보관 만료 시 해시·위치만 남김), 원문 스냅샷 미보관(기존 정책). **원문 저장소를 따로 둘지는 §10 결정 항목.**

### 6.3 수집 불가로 기록하는 7개 도메인
`electricliterature.com`, `thedriftmag.com`, `bostonreview.net`, `lux-magazine.com`, `liberreview.com`, `thepointmag.com`, `bookforum.com` (각각 www 호스트 포함).
- 상태: `permission=not_collectable`, 사유 `access_review`(401/403/캡차 정지, 2026-10-27 기준), 우회(사람 위장·계정 교체·프록시) **금지**.
- 허용되는 대안(공개·공식 경로만): 해당 매체의 **공식 RSS/sitemap**, **공식 뉴스레터/공지의 공개 URL**, **공식 Submittable/투고 플랫폼의 공개 페이지**, 주최자 자체 사이트, 사람이 직접 보고 입력하는 수동 확인(`manual_clip`). 각 경로는 robots·약관 확인 후 **새 소스로 별도 등록**하고, 대체 경로에서 얻은 값도 원 매체 차단을 우회한 것이 아님을 `permission_basis`에 기록합니다.
- 후속 작업: 7곳 각각 대체 경로 탐색 결과(있음/없음)를 표로 레지스트리에 남깁니다(없으면 `none_found`로 명시, 비워 두지 않음).

### 6.4 Instagram — 수집 가능/불가 범위 (정직한 기록)
확인한 것: `https://www.instagram.com/robots.txt`는 일부 AI 크롤러를 명시 차단하고 `User-agent: *`에 **`Disallow: /`**, 상단 고지에 "명시적 서면 허가 없는 자동 수집 금지"가 있습니다(2026-10-27 확인). **페이지 자체는 직접 요청해 테스트하지 않았습니다**(robots 금지 때문). 아래 "비로그인 시 보이는 범위"는 일반적으로 알려진 동작에 근거한 추정이며 `unverified`로 기록합니다.

| 범위 | 판단 | 근거/조건 |
| --- | --- | --- |
| 자동 크롤링(프로필·게시물·릴스·해시태그) | **미지원(수집 안 함)** | robots `*` 전면 Disallow + 자동 수집 금지 고지. 사람 위장·계정·프록시 우회 금지(지시) |
| 로그인이 필요한 정보(비공개 계정, 팔로워 한정, 스토리, 전체 게시물 목록 등) | **미지원** | 로그인 자동화 범위 밖(기존 계약) |
| 비로그인 공개 화면 | **자동 수집 안 함**; 사람이 직접 보는 것만 가능 | 일반적으로 일부 게시물 미리보기 수준 + 로그인 유도 가능성(미검증) |
| 매체가 **자기 공식 사이트에 공개한 IG 링크/문구** | 지원(공식 사이트 수집 시 부수 수집) | 소스는 공식 사이트이며 IG는 `social_lead` 별칭으로만 기록 |
| 사용자가 **본인 브라우저에서 직접** 열어 보고 사용자 조작으로 보내는 수동 클립(확장 `manual_clip`) | 조건부 지원 | 사용자 개별 행위, 저빈도, 발췌 ≤280자, 개인정보 제거, **항상 `unverified` 리드**로만 저장(공식 사이트에서 확인되기 전 `source_claim` 승급 불가). 약관 해석은 준태님 판단 사항 |
| 공식 API(Meta Graph API: 본인/승인된 비즈니스 계정 한정, 앱 등록·심사 필요) | **보류(결정 필요)** | 계정 연결·앱 심사가 필요하므로 승인 전 진행 안 함. 다른 매체 계정 데이터 접근은 제한적일 수 있음(확인 필요) |

레지스트리 기록: `source_type=social_lead`, `permission=manual_only`, `collectable_fields=[announcement_text(발췌), posted_at, linked_url]`, `unsupported_scope=[자동 수집 전부, 로그인 필요 범위]`, `retention=발췌 90일 후 해시만`. 산출물 용도: **확인할 후보(리드) 찾기**. IG 단독 정보로 상태/마감/조건을 `verified_fact`로 만들지 않음.

---

## 7. 현재 스키마 재사용·마이그레이션 계획

### 7.1 매핑
| 현재 | 새 구조 | 변환 |
| --- | --- | --- |
| `entities`(79)+`entity_aliases` | `pb_outlet` + `pb_alias` | `out_<ULID>` 부여, 기존 UUID/사이트 numeric id는 alias로 보존. `target_entity_id` 연결 유지 |
| `sources`(139), `seeds`(103), `source_checks`, `policy_registry/receipts` | `source_registry` + 기존 정책 테이블 유지 | `authority` 값 그대로, 레지스트리 필드(§6.1) 추가 컬럼(NULL 허용 → 점진 채움) |
| `source_revisions`, `evidence`(≤280자) | 동일 + `char_start/end`, `content_hash`, `locator_json` 추가 | 기존 `heading`/`paragraph_index`는 유지(위치 정보 하위 호환) |
| `claims`(283): field, scope_key, value, authority | `pb_fact` | `field`→`predicate` 매핑표, `scope_key`→`scope_ref`, `authority=official_primary`+증거 있음 → **`source_claim`**(검증 단계 미통과이므로 `verified_fact`로 일괄 승급 금지), 그 외 `unverified`; `observed_at`→`collected_at`; `effective_from`=NULL(미상) |
| `conflicts`/`conflict_members`(4) | `pb_conflict_group` | 1:1, 미해결 상태 보존 |
| `review_decisions`(47)/`stage_events`(316) | 그대로 + `pb_status_event` 별도 | 파이프라인 단계 이벤트와 현실 상태 이벤트 분리 |
| `items`(call 4, outlet_guidelines 77, source_lead 58) | `items.stage` 유지(파이프라인 진행) + `pb_call_edition`(call 4건 → `status_unknown`/`candidate`로 시작) | 상태는 새 검증 단계 후에만 `open` 등으로 |
| `jobs`(fetch/normalize/recheck) | `jobs.kind` 확장(`detect/extract/resolve/verify/update/recompute/qc`), `stage_run`, `failure_queue` 추가 | `retry_wait`/`dead_letter` 의미 유지 |
| `daily_budget`, `domain_state`, `robots_cache`, `budget_reservations` | 그대로(공유 인프라) + 단계별 비용 항목 추가 | — |
| 사이트 `outlets.json`(96), `calls.json`(3) | **읽기 전용 import**(기존 `--import-data` 경로 재사용) → `unverified` Fact로, `last_checked`→`collected_at` | 문자열 `pays`/`word_count`는 `value_raw` 보존 + 정규화 시도(V-05), 사이트 JSON은 건드리지 않음 |

### 7.2 절차 (모두 가산적, 파괴 없음)
1. **백업**(`inbox.sqlite3` + WAL checkpoint 후 파일 복사, `var/.recovery` 마커는 백업 대상 제외 — 기존 규약 준수).
2. `schema_migrations`(체크섬 보유)에 `0004~` 마이그레이션 추가: 새 테이블/컬럼만 추가(DROP/ALTER 파괴 변경 없음). 서비스 DB는 **새 파일**(`pitchboard_kg.sqlite3`)에서 시작.
3. **섀도 모드**: 새 파이프라인은 기존 데몬과 병행해 *쓰기 없이* 계산만 → 기존 `claims`와 신규 `pb_fact` diff 리포트로 정합성 확인(건수, 필드 매핑 누락).
4. 승인된 후 백필 실행(멱등: 소스 digest 키, 재실행 안전).
5. 새 단계 러너를 **별도 진입점**으로 추가, 하루 하루 이관(단계 1~3 → 4~6 → 7~9). 기존 `daemon.sh`는 마지막에 교체 여부를 결정(이번 단계에서 수정하지 않음).
6. 롤백: 새 테이블 무시하고 기존 경로 계속 사용 가능(기존 테이블 미변경). 서비스 DB 파일은 폐기 가능.
- 시간 변환: `REAL` epoch → UTC ISO-8601 문자열. 변환 검증: 왕복 일치 테스트.
- **선행 조건(별도 승인 필요)**: ① 만료된 본문(전체 페이지) 수집 권한 갱신 여부 — 현재 링크 수집만 되고 본문 추출 불가라 Fact 생산이 막혀 있음, ② 오늘 크롤러에 허용한 네트워크 게이트웨이 예외(198.18.0.0/15) 유지 여부.

---

## 8. 구축 단계 (각 단계 = 작업 단위 = 별도 PR)

| 단계 | 내용 | 완료 기준(테스트) |
| --- | --- | --- |
| **P0 결정/선행** | §10 결정 항목 확정, 본문 수집 권한, 네트워크 예외 확인 | 준태님 답변 |
| **P1 질문·골든셋** | Q1~Q10을 **비식별 픽스처 20~30건**(가상 매체·마감·충돌·결측)으로 정답표 작성 | 정답표 리뷰 |
| **P2 스키마·검증기** | 서비스 DB DDL, V-01~V-15 검증기, 마이그레이션 0004, 상태 기계 | 규칙별 단위 테스트(위반 거부), 상태 전이표 전수 테스트 |
| **P3 백필·섀도** | 기존 claims/사이트 데이터 import(`source_claim/unverified`), diff 리포트 | 건수·필드 정합 리포트, 재실행 멱등 |
| **P4 단계 러너** | 단계 1~9 계약 구현(체크포인트·잠금·재시도·실패 큐·비용 상한), 실패 지점 재개 | 강제 중단 후 재개, 중복 실행 방지, 상한 도달 테스트 |
| **P5 변경 전파** | 변경 감지→Fact diff→상태 이벤트→투영 갱신, SLA 점검 | 픽스처 원문 수정(마감 연장/취소) → 투영 반영, 이전 값 이력 확인 |
| **P6 소스 확장** | 레지스트리 채움(76 도메인), 7곳 대체 경로 조사, IG 리드 경로(수동 클립), 공식 sitemap/RSS 확대 | 소스별 permission·cadence·unsupported 기록 100%, 빈 실행 경보 |
| **P7 품질·운영 문서** | 품질 지표, 런북, 장애 대응(§4), GitHub 정리 | 모의 장애 훈련, 문서 리뷰 |

권장 우선 구현 대상(디지털트윈 취지): 단순 매체 속성이 아니라 **`CallEdition`의 상태·마감·조건 변화 추적**부터 (P2~P5). 매체 프로필(Outlet)은 부가.

---

## 9. GitHub 저장소 제안 (아직 푸시 안 함)

- **제안: 새 비공개 repo `SUNBURN-Golden/pitchboard-data`** (사이트 repo `pitchboard-site`와 분리 — 사이트는 프론트·배포, 이쪽은 데이터 기반).
  - `schema/`(DDL, `migrations/0004_*.sql`), `pb/`(온톨로지 모델, 검증기 `rules/`, 상태 기계, repo 계층), `collectors/`(auto_discovery 등 이관), `stages/`(단계 러너), `common/`(공유 작업 인프라 — 이후 다른 서비스가 쓰면 별도 repo 분리), `registry/source_registry.example.csv`(**URL·권한 메타만**, 비식별), `tests/fixtures/`(**가상 도메인·가상 이름** 비식별 자료만, 실제 담당자 이메일/개인정보 금지), `docs/`(DESIGN, 런북, 소스별 권한 기록).
  - 작업 단위 = 브랜치 `p2-schema-validators` 식 + PR 1개. CI: 단위 테스트, 마이그레이션 체크섬 검증, 시크릿/개인정보 스캔.
- **repo 밖(커밋 금지, `.gitignore`)**: 서비스 DB(`pitchboard_kg.sqlite3`), 인프라 DB(`infra.sqlite3`), 원문 저장소(`rawstore/`, 기본 비어 있음), 백업, `var/.recovery`, 확장 페어링 토큰, 로그. 위치 제안: `/workspace/pitchboard/data-root/{service_db,infra_db,rawstore,backups}` 와 환경변수 `PB_DATA_ROOT`로 주입.
- 현재 `crawler/`는 git repo가 아님(확인) → 코드 이관 시 이력 보존 없이 새 repo로 복사하고 원본은 유지.
- 푸시 전 승인 항목: repo 생성(공개 범위 포함), 최초 커밋 내용 목록.

---

## 10. 준태님 결정이 필요한 항목

1. 새 repo `pitchboard-data`(비공개) 생성 승인 및 위치(§9).
2. **원문 보관**: 지금 정책(원문 미보관, 280자 발췌+해시)을 유지할지, 소스별로 제한 기간(예 30일) 원문 보관을 허용해 증거 재검증을 가능하게 할지.
3. 본문 수집 권한(만료분) 갱신 — 없으면 새 Fact가 거의 안 생김.
4. 오늘 추가한 네트워크 게이트웨이 예외(198.18.0.0/15) 유지.
5. Instagram: 수동 클립(사용자 조작)만 허용 / 공식 API(Meta 앱·계정 연결) 검토 여부.
6. 모델(AI) 추출 허용 및 일일 비용 상한(기본 0).
7. 한국 작가용 서비스 관점에서 우선할 조건(거주/국적/언어)의 기본 프로필.

---

## 11. 비목표 / 금지 (재확인)
- 프론트엔드·게시·배포·사이트 JSON 수정 없음. 자동 게시 없음.
- 차단 우회(사람 위장·계정 교체·프록시), 로그인 자동화, 캡차 우회, 유료벽 우회, robots 우회 없음.
- 원문에 없는 관계의 AI 확정 금지. 충돌 덮어쓰기 금지. 결측을 "제한 없음"으로 처리 금지.
