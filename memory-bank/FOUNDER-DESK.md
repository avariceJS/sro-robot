# Founder Desk — 2026-10-21

- **Теперь:** в карточке проверки можно листать многостраничный TIFF, менять масштаб и скачивать исходный файл; предпросмотр формируется отдельно для выбранной страницы.
- **Защита:** лимиты размера, кадров, пикселей, результата и времени; навигация по страницам не меняет проверяемые поля. Save/recovery остаются прежними.
- **Проверки:** Sol-6.1 High: GO, замечаний P1/P2/P3 нет. Полный локальный gate: 976 Python, 10 PostgreSQL case-package, 157 PostgreSQL integration tests, миграции, восстановление, concurrency и Chromium. TIFF проверен при обычном просмотре, конфликте и восстановлении; `external_sro_accessed=false`.
- **Доказательство:** `/tmp/sro-tiff-viewer-verify-final6.log`, SHA-256 `c4adeaa1602d81d3813148b4ab18471b5f77ec1276a2ed38f1f9cff980ed6b06`. Только synthetic data; офисной приёмки нет.
- **Ограничение:** на macOS нельзя применить Linux `RLIMIT_AS`; действуют остальные лимиты процесса и внешние ограничения времени. Это не production-приёмка.
- **Production:** релиза нет, версия не менялась. Обязательные свежие production backup/restore, миграционная и rollback репетиции, GitHub validation и точные release gates не подтверждены.
- **Сейчас:** selective публикация проверенного изменения в `develop`, затем проверка GitHub Actions и продолжение разработки.

- **Теперь:** локальный OCR обрабатывает многостраничные TIFF последовательно; сохраняет пустые страницы и точную нумерацию найденных полей.
- **Защита:** лимит кадров 80 по умолчанию, лимит пикселей на страницу и общий бюджет времени. Ошибка OCR любой страницы прекращает обработку целого документа; исходник сохраняется.
- **Публикация:** `develop` и `origin/develop` совпадают на `558ad6b23e9522fe4e083da13ff309014b3ae65c`.
- **Проверки:** Sol-6.1 High: GO, P1/P2/P3 нет. Полный локальный gate: 967 Python, 10 PostgreSQL case-package, 157 PostgreSQL integration tests, Chromium и restore/concurrency drills; TIFF прошёл сквозной UI сценарий; `external_sro_accessed=false`. Synthetic TIFF OCR: 2.185 с, peak RSS процесса 111.9 MiB.
- **GitHub:** verify run `39538057671` не запустил ни одного шага из-за проблем payments/spending limit; deploy run `39538056534` не создал jobs. Actions не проверил и не развёртывал кандидата.
- **Production:** работает `2827cd232ff6cc4287a6b880c2d310e21756e073`; `/readyz` healthy. Релиза нет: отсутствуют свежие production backup/restore proof и rollback rehearsal.
- **Ограничения:** нет репрезентативных офисных сканов и приёмки сотрудниками; timeout декодера TIFF кооперативный. Продолжаю разработку на `develop`, production удерживается до доказательств всех обязательных gates.
