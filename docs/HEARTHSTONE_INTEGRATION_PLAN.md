# План углубления ParsesUnix для Hearthstone-источников

## Цель

Сделать embedded-интеграцию ParsesUnix безопасной для смешанного набора
источников `api.kolodahearthstone.com`: SSR HTML, JSON API, WordPress REST,
Firebase и клиентские оболочки. Успехом считается не `HTTP 200`, а доказанная
цепочка `transport -> content contract -> parser candidate -> publication gate`.

План не переносит специфичные парсеры Hearthstone в универсальное ядро. Ядро
задаёт строгий транспортный контракт, а прикладной репозиторий сохраняет
source-specific схемы, семантику и правила публикации.

## Исходные измерения

Zero-cost проверка 20 августа 2026 года дала следующую картину:

- ParsesUnix напрямую получил 10 из 10 HTML-страниц без платного провайдера;
- полный прикладной контракт прошёл только HSGuru Meta;
- HSReplay, Firestone, HearthArena, MetaStats, Hearthstone-Decks и Vicious
  возвращали страницу-оболочку, тогда как полезные данные живут во внутренних
  API;
- Firestone Standard и HSReplay Trinkets вернули корректный JSON и `HTTP 200`,
  но embedded-мост прикладного проекта передал в triage жёсткое ожидание HTML;
- HSGuru Streamer вернул строки, но без обязательных deck-кодов.

Следовательно, приоритет — не новый прокси, а интерфейс, который не позволяет
consumer-коду потерять тип ответа и доказательство содержимого.

## Архитектурные решения

1. **Тип ответа задаётся явно.** HTML, JSON и Text используют разные контракты;
   автоматическое принятие любого непустого `200` запрещено.
2. **Контракт требует доказательство.** Для HTML/Text нужен canary, для JSON —
   обязательный JSON path. Размер тела сам по себе недостаточен.
3. **Усечённый ответ всегда бракуется.** Prefix не является документом даже
   тогда, когда canary успел попасть в прочитанную часть.
4. **Телеметрия безопасна по умолчанию.** В ней нет тела, заголовков и query;
   остаются host, hash, размер, latency, status, content kind и verdict.
5. **Ядро не заявляет о публикации.** Оно подтверждает только транспорт и
   response contract. Candidate, completeness, regression и atomic publication
   остаются ответственностью прикладного parser pipeline.
6. **Платный fallback не лечит локальные ошибки.** JSON/HTML mismatch,
   отсутствующий path, сломанный extractor или source contract не разрешают
   Scrape.do/Bright Data.

## Зависимости

```text
Строгий ResponseContract
          |
          v
RawResponse validation + safe telemetry
          |
          v
Embedded fetch API
          |
          v
Hearthstone source recipes в API-репозитории
          |
          v
Shadow evidence -> bounded active rollout
```

## Фаза 1. Строгий embedded response contract

### Задача 1.1 — типизированный контракт

**Описание:** добавить публичный immutable-контракт для HTML, JSON и Text.

**Критерии приёмки:**

- HTML/Text без canary отклоняются до сети;
- JSON без required path отклоняется до сети;
- неподдерживаемый Binary/Unknown contract не создаётся;
- контракт компилируется в канонический `ContentRules` без второй логики triage.

**Проверка:** unit-тесты конструкторов и полученных rules.

**Зависимости:** нет.

**Предполагаемые файлы:** `src/web_scraper/embedded.py`,
`tests/test_embedded.py`.

**Размер:** S.

### Задача 1.2 — validation результата транспорта

**Описание:** принимать `RawResponse`, вызывать канонический triage и отдельно
фиксировать фактический `ContentKind`.

**Критерии приёмки:**

- корректные HSGuru-подобный HTML и HSReplay/Firestone-подобный JSON дают `OK`;
- JSON под HTML-контрактом даёт `PARSE_FAIL`;
- truncated body всегда даёт `PARSE_FAIL`;
- CSR shell с отсутствующим canary остаётся `CSR_REQUIRED`, а не становится
  доказательством блокировки.

**Проверка:** локальные fixtures без внешней сети.

**Зависимости:** 1.1.

**Предполагаемые файлы:** `src/web_scraper/embedded.py`,
`tests/test_embedded.py`.

**Размер:** S.

### Задача 1.3 — безопасный embedded fetch

**Описание:** дать consumer-репозиторию одну функцию
`fetch_validated(transport, url, contract)`, не скрывая transport policy.

**Критерии приёмки:**

- transport инъецируется явно;
- функция возвращает raw response и triage evidence;
- сериализуемая телеметрия не содержит query, headers или body;
- транспортная ошибка не раскрывает исходное исключение в телеметрии.

**Проверка:** fake transport и URL с чувствительно выглядящим query.

**Зависимости:** 1.2.

**Предполагаемые файлы:** `src/web_scraper/embedded.py`,
`src/web_scraper/__init__.py`, `tests/test_embedded.py`.

**Размер:** S.

### Checkpoint A

- targeted-тесты embedded API зелёные;
- Ruff и mypy проходят;
- старый `ContentRules` API остаётся совместимым;
- ни одна новая ветка не разрешает paid escalation по `PARSE_FAIL`.

## Фаза 2. Source recipes в `api.kolodahearthstone.com`

Эта фаза выполняется после выпуска новой версии core в прикладном репозитории.

### Задача 2.1 — HSGuru

- Meta/Matchups: HTML contract с source-specific canary и текущими semantic /
  completeness gates;
- Streamer Decks: отдельная гидратация deck-кодов; до неё источник остаётся в
  shadow и не считается compatible.

### Задача 2.2 — HSReplay

- API endpoints получают JSON contract и обязательные paths;
- HTML-оболочки Arena/Trending не заменяют рабочие API routes;
- premium session остаётся прикладной ответственностью и не попадает в профиль
  или телеметрию.

### Задача 2.3 — Firestone

- ZeroToHeroes CDN и Firestone endpoints используют JSON contract;
- валидируется тип, schema path, минимальное число сущностей и upstream patch;
- browser shell остаётся только discovery-маршрутом.

### Задача 2.4 — остальные семейства

- Hearthstone-Decks: WordPress REST;
- Vicious: Firebase/официальные data endpoints;
- HearthArena и MetaStats: существующие API routes;
- HTML fallback допускается только при наличии полноценного parser candidate.

### Checkpoint B

- каждый source имеет явный content contract;
- HTML shell не может пройти candidate gate;
- transport/candidate/publication evidence записываются раздельно;
- paid provider вызывается только после `BLOCKED`/`SOFT_BLOCK`.

## Фаза 3. Shadow и продвижение

1. Запустить zero-cost shadow по одному источнику каждого семейства.
2. Накопить повторные наблюдения transport/candidate/publication compatibility.
3. Сравнить canonical hashes, entity counts, completeness и upstream version.
4. В active переводить небольшими allowlist-группами с отдельным rollback.
5. Scrape.do оставить ограниченным fallback; Bright Data residential не
   включать без отдельного решения оператора.

**Критерии продвижения:**

- transport validation = 100% на окне canary;
- candidate validation = 100%;
- publication compatibility = 100%;
- retrieval completeness соответствует source contract;
- paid cost известна и остаётся внутри дневного/per-refresh бюджета;
- ни LKG, ни provisional не учитываются как full-fresh.

## Риски и меры

| Риск | Влияние | Мера |
|---|---|---|
| Generic JSON принят без полезных данных | Высокое | required JSON paths обязательны |
| CSR shell принят как готовая страница | Высокое | обязательный canary и `CSR_REQUIRED` |
| Усечённый ответ выглядит валидным | Высокое | fail-closed truncation до triage успеха |
| Секрет появляется в telemetry | Высокое | host-only URL, без headers/body/query |
| Core дублирует прикладные contracts | Среднее | core проверяет response; API проверяет dataset |
| Новый маршрут увеличивает расходы | Среднее | paid escalation policy не меняется |
| Массовое включение портит fresh-only | Высокое | shadow -> bounded active -> rollback |

## Definition of Done

- новый public API покрыт тестами RED -> GREEN;
- полный unittest suite, Ruff и mypy зелёные;
- security scan не находит секретов или уязвимостей;
- README объясняет границу transport validation и publication success;
- изменения разбиты на логичные коммиты и отправлены в `main` ParsesUnix;
- подключение нового core в API-репозиторий выполняется отдельным rollout после
  выпуска immutable artifact с checksum.
