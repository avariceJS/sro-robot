# Founder Desk — 2026-10-21


- **Теперь:** в истории загрузок сотрудники находят принятую партию по исходному имени файла; страница показывает точные совпадения и сохраняет границы партии для продолжения и recovery. Поиск и пагинация передаются POST и не попадают в URL.
- **TIFF:** добавлен просмотр страниц; при занятом декодере браузер один раз повторяет запрос после `Retry-After`.
- **Проверки:** Sol-6.1 High: GO, P1/P2/P3 нет. Full gate: 978 Python, 10 PostgreSQL case-package, 159 PostgreSQL integration tests, Chromium, миграции, restore/recovery и concurrency. Поиск 10 000 synthetic receipts: 63.57 мс за один service query, без HTTP/параллельной нагрузки. `external_sro_accessed=false`.
- **GitHub:** `develop`/`origin/develop` совпадают на `23cadd47b69aa43eb80f5acb843a0ee883a3d1ef`. Verify `39542741394` и deploy `39542740373` завершились до jobs/steps; hosted Actions код не запускали.
- **Production:** работает `2827cd232ff6cc4287a6b880c2d310e21756e073`, `/readyz` healthy. Релиза нет: свежие production backup/restore и rollback/migration репетиции не подтверждены.
- **Ограничения:** нет офисных документов и приёмки сотрудников. Production остаётся на прежней версии до прохождения каждого обязательного release gate. Продолжаю Stage 4 разработку.
