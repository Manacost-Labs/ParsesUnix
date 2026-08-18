# План реализации Web Scraper Skill

## Цель

Создать переносимый скилл для Codex и Claude Code, который помогает проектировать и реализовывать надёжный парсинг одной страницы или группы страниц: сначала бесплатные и устойчивые маршруты, затем доказанная эскалация, обязательная валидация данных, фактический учёт стоимости и видимый итог по каждому URL.

«Любая страница» здесь означает расширяемую систему маршрутов и адаптеров, а не обещание универсального селектора. Страницы за авторизацией, paywall и другими контролями доступа остаются вне автоматического обхода без отдельного разрешённого интерфейса.

## Зафиксированные решения

- Один канонический skill в `.agents/skills/web-scraper`; Claude Code подключается к нему ссылкой из `.claude/skills/web-scraper`, поэтому инструкции не дублируются.
- Frontmatter ограничен `name` и `description`, что совместимо с общим Agent Skills форматом.
- Python + Scrapling — основной путь. Scrapy сохраняется для существующих проектов; Rust (`wreq`) — поздняя оптимизация стабильного L1.
- `triage` — единственный источник решений о retry/escalation.
- Paid fallback разрешён только для доказанных `BLOCKED`/`SOFT_BLOCK` и всегда проходит через бюджет.
- 404/410, origin failure, rate limit, auth/access denial и parse failure не эскалируются на деньги.
- Исходные материалы хранятся без редактирования в `docs/research`; рабочие references устраняют найденные противоречия.

## Этапы

### Этап 0 — фундамент (в этом каркасе)

- Сохранить research-материалы и контрольные суммы.
- Создать portable SKILL.md, маршрутизацию references и Site Profile template.
- Реализовать детерминированный triage.
- Реализовать локальный дневной budget ledger без секретов.
- Реализовать безопасный статический probe с блокировкой private-network targets.
- Добавить unit-тесты и проверку формата скилла.

Граница: ещё нет production scheduler, браузерного XHR discovery, адаптеров платных провайдеров, ClickHouse/R2/Telegram и ACR.

### Этап 1 — probe и профили сайтов

- Проверка robots/sitemap/RSS/AMP/JSON-LD/embedded state.
- Браузерный перехват XHR для CSR-сайтов.
- Версионируемый Site Profile на каждый домен/URL class.
- Набор снапшотов: success, block, soft block, 404, rate limit, redesign.

Приёмка: три эталонных сайта (SSR, CSR, managed challenge) получают корректный стартовый маршрут без платного запроса на SSR.

### Этап 2 — Fetch Gateway L0–L2

- Единый Result/verdict contract.
- Scrapling sessions, warmup, concurrency, jitter, `Retry-After`.
- Alternative routes до эскалации.
- Snapshot store и redaction.
- Checkpoint/resume и идемпотентность.

Приёмка: прерывание и повторный запуск не создают дублей; empty-200/challenge не считается успехом.

### Этап 3 — адаптеры L3–L4 и стоимость

- Изолированные adapters для scrape.do, Firecrawl и Bright Data.
- Live-doc preflight и contract tests на сохранённых fixtures.
- Фактическая стоимость scrape.do из response header.
- Запрет cookie/auth forwarding; Bright Data custom headers выключены по умолчанию.
- Per-provider/day budget и circuit breaker.

Приёмка: 404/410 и origin failure не вызывают paid fallback; превышение бюджета останавливает только платные уровни и оставляет отчёт.

### Этап 4 — группы URL и расписание

- Queue, dedup, HEAD/light-GET sweep, quarantine и dead zones.
- Retry sweep внутри окна и перенос origin failures в следующий прогон.
- Проверка пагинации и ожидаемого объёма.
- Итоговый отчёт coverage/verdict/cost/fallback/unresolved.

Приёмка: каждый URL имеет запись или финальный вердикт; молчаливых пропусков нет.

### Этап 5 — Data Quality и атомарная публикация

- Цепочка extractors: JSON-LD → app state → meta → CSS/XPath → heuristic.
- Нормализация, schema validation и quorum критичных полей.
- Staging → validate → atomic promote; LKG сохраняется при браке.

Приёмка: сломанный частичный прогон не меняет чистый набор данных.

### Этап 5.5 — Freshness Scheduler

- ETag/Last-Modified, нормализованные hashes, index/sitemap watch.
- Адаптивные интервалы и полная периодическая ревизия.

Приёмка: на неизменном корпусе снимается не менее 90% полных запросов без нарушения freshness SLO.

### Этап 6 — наблюдаемость

- Метрики по домену, классу URL, route, level, extractor и provider.
- Data freshness отдельно от HTTP success.
- Дашборд и алерты без секретов.

### Этап 7 — Adaptive Cost Router

- EWMA, Wilson lower bound, expected cost и latency window.
- Бесплатные shadow probes вниз, гистерезис и детектор смены защиты.
- `saved_credits` относительно фиксированной политики.

Приёмка: origin outage не повышает уровень; ослабление защиты приводит к безопасному downgrade.

### Этап 8 — Rust L1 worker

Только после профилирования: порт canonical triage contract и высокообъёмного L1 на `wreq`/`wreq-util`/`scraper`/`tokio`. Не переносить L2 ради унификации языка.

## Ближайший практический шаг

Выбрать 3 разрешённых тестовых домена (SSR, CSR, challenge) и 20–50 URL на класс. На них создать первые Site Profiles и fixture-набор, после чего реализовывать Этап 1 без привязки к production-секретам.

