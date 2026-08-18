# План реализации Web Scraper Skill

## Цель

Создать переносимый скилл для Codex и Claude Code, который помогает проектировать и реализовывать надёжный парсинг одной страницы или группы страниц: сначала бесплатные и устойчивые маршруты, затем доказанная эскалация, обязательная валидация данных, фактический учёт стоимости и видимый итог по каждому URL.

«Любая страница» здесь означает расширяемую систему маршрутов и адаптеров, а не обещание универсального селектора. Страницы за авторизацией, paywall и другими контролями доступа остаются вне автоматического обхода без отдельного разрешённого интерфейса.

## Зафиксированные решения

- Семейство скиллов, каждый канонический в `.agents/skills/<name>`; Claude Code подключается ссылками из `.claude/skills/`, поэтому инструкции не дублируются. Правило разделения: **детерминированная логика живёт в пакете `web_scraper` и вызывается через `ws-*` CLI, скилл — это только когда и как её применять**. Скилл не описывает алгоритм, который можно выполнить кодом.
  - `web-scraper` — сбор данных (probe → профиль → уровни → прогон).
  - `scraper-regression` — что изменилось на сайте (`ws-regress`).
  - `scraper-debugger` — почему прогон недобирает и что с этим делать (`ws-diagnose`).
  - Обновлено 2026-08-19: раньше решение фиксировало один-единственный skill; расширено до семейства с непересекающимися описаниями, чтобы триггеры не конкурировали.
- Инженерные практики подключены как vendored-скиллы из MIT-репозиториев (`karpathy-coder`, `test-driven-development`, `systematic-debugging`, `verification-before-completion`, `pr-review-expert`, `python-lint`, `python-typing`, `python-ci`). Хранятся байт-в-байт как upstream, атрибуция и список правок — в `.agents/skills/THIRD_PARTY.md`. Там же зафиксировано, что конвенции проекта важнее их рекомендаций по инструментам: тесты остаются на stdlib `unittest`, `ruff`/`mypy` живут только в `dev`-extra и CI, рантайм пакета — без сторонних зависимостей.
- Ограничение frontmatter (`name`+`description`) действует для собственных скиллов; vendored сохраняют свой upstream-frontmatter. Обе инварианты проверяются `tests/test_skills.py` — это закрывает пункт «проверка формата скилла» из Этапа 0.
- Frontmatter ограничен `name` и `description`, что совместимо с общим Agent Skills форматом.
- Python + Scrapling — основной путь. Scrapy сохраняется для существующих проектов; Rust (`wreq`) — поздняя оптимизация стабильного L1.
- `triage` — единственный источник решений о retry/escalation.
- Paid fallback разрешён только для доказанных `BLOCKED`/`SOFT_BLOCK` и всегда проходит через бюджет.
- 404/410, origin failure, rate limit, auth/access denial и parse failure не эскалируются на деньги.
- Исходные материалы хранятся без редактирования в `docs/research`; рабочие references устраняют найденные противоречия.

## Статус (обновлено 2026-08-18)

Реализовано и в проде-готовности бесплатное ядро L0–L2:

- **Этапы 0–2 — готовы.** Контракты, Site Profile + валидатор, probe v2, браузерная разведка CSR, Fetch Gateway L0–L2 (строгая политика эскалации, circuit breaker, session/pacing/snapshots), checkpoint/resume через SQLite-очередь.
- **Этап 4 (частично) — готов бесплатный контур.** Очередь с дедупом/карантином/dead zones, phase-A HEAD sweep, раннер с окном/резюме, итоговый отчёт по каждому URL. Платный контур этапа 3 отложен (нет ключей).
- **Этап 5 — готов.** Цепочка экстракторов (JSON-LD→app_state→meta→CSS→heuristic) с провенансом и кворумом, staging→validate→atomic promote→LKG.
- **Этап 5.5 (freshness) — готов.** Условные запросы, content-hash, адаптивный интервал; на example.com подтверждён реальный 304.
- **Этап 6 (наблюдаемость) — базово готов.** Метрики, отчёт, alert-хук (log; Telegram — слот). ClickHouse/R2 отложены.
- **Инфраструктура:** CI (unittest 3.11–3.13), пакет `pip install -e .`, ws-* CLI, systemd-деплой на debian-151, 198 тестов.
- **Приёмка на реальных доменах** — см. `docs/acceptance/`: cost-safety и triage подтверждены; Cloudflare-домены требуют L2 (Scrapling/Playwright) на сервере.

Отложено: платные провайдеры L3–L4 (этап 3), ACR (этап 7), Rust L1 (этап 8), ClickHouse/R2.

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

