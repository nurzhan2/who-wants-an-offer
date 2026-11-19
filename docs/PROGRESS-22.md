# Промт 22 — журнал решений

Одна строка на решение: что решил · почему · чем проверил.

## Общее

- Работа в облачном контейнере, а не на машине владельца · сессия запущена в облаке, локальная папка `C:\Users\user\Documents\Who_wants_an_offer` отсюда недоступна · —
- Одна ветка `claude/bold-galileo-42d4xf` вместо ветки на фазу · среда разрешает пушить только в эту ветку; фазы разделены коммитами с префиксом области · `git log`
- `git push` в эту ветку делаю сам, `--force` и переписывание истории — нет · контейнер эфемерный, без пуша работа теряется; STOP 3 толкую как запрет на `main`, force и историю · —
- `CLAUDE.md` в репозитории нет, его роль играет `docs/ENGINEERING.md` (коммит 8194928) · правила взяты оттуда · `git log -- CLAUDE.md`
- Сеть контейнера закрыта политикой (прокси отвечает 403 на hh.ru, hh.kz, t.me, himalayas, greenhouse, habr) · живые замеры отсюда невозможны; фикстуры новых источников восстановлены по публичной документации и помечены `_comment` · `curl https://hh.ru/robots.txt` → `CONNECT tunnel failed, response 403`
- Для тестов поднят локальный PostgreSQL 16 + pgvector на 5436 · `make test` требует БД · исходный прогон: 3002 passed, 94%
- Коннекторы фазы D писали два параллельных агента в отдельных worktree, только новыми файлами; слиты в ветку вручную · коннекторы независимы (правило 5 ENGINEERING) · их тесты + полный прогон после слияния

## Фаза A — несколько профилей

- Новые поля `slug` (unique), `track`, `priority` (default 100), `hh_resume_id` (char 38, nullable) · по ТЗ · миграция 0018, `test_migrations` (upgrade/downgrade/upgrade)
- Миграция: новейший активный профиль → `priority=0`, `slug=main`; остальные → `cv-<8 hex id>` · ничего не удаляется и не переименовывается руками · `test_migrations`
- Формат hh_resume_id проверяет Pydantic (`^[0-9a-f]{38}$`), а не CHECK в БД · все типизированные поля этой таблицы проверяются схемой; CHECK потребовал бы обойти naming convention · `test_a_profile_can_be_renamed_but_not_onto_another`
- `default_profile()` — активный с наименьшим priority, затем новейший; `get_active()` оставлен как алиас · ТЗ: явная функция вместо «первый попавшийся» · `test_the_default_profile_is_the_lowest_priority_not_the_newest`
- Правило выбора (DEFAULT_ORDER, best_profile_id) вынесено в `app/db/profile_order.py` · репозитории не должны импортировать `matching/`; первая версия дала циклический импорт · `python -c "import app.main"`
- `best_profile_id` — коррелированный подзапрос, не колонка `vacancy.best_profile_id` · колонка была бы второй копией факта из `match` и устаревала бы при выключении профиля · `test_two_vacancies_are_won_by_two_different_profiles`, `test_an_inactive_profile_never_wins`
- `match` уже уникален по `(profile_id, vacancy_id)` — миграция для скоринга не нужна · проверено в `models.py` и 0001 · `test_two_vacancies…`
- `score_corpus()` без профиля считает все активные; отчёт добавляет строку о профилях только при двух и больше · отчёты по одному профилю не меняются · `test_operations`, `test_autopilot`
- Загрузка больше не выключает остальные; `?replace=<slug>` выключает заменяемый только после успешного разбора нового · упавший разбор не должен оставить трек без резюме · `test_a_failed_replacement_leaves_the_replaced_profile_live`
- Эндпоинт остался `/resume/upload` (в ТЗ написано `POST /api/v1/resume`) · такого пути в коде нет, переименование сломало бы клиент; параметры добавлены к существующему · `test_replace_*`
- Наследование названий должностей — только от заменяемого профиля, не от «предыдущего активного» · новый трек не должен искать по названиям другого трека · `test_a_new_track_does_not_inherit_another_tracks_titles`
- Пакетная загрузка: все slug и размеры проверяются до записи первой строки; разбор — последовательно в одной фоновой задаче · «всё или ничего» на запросе; одна LLM за раз · `test_a_batch_with_one_bad_slug_writes_nothing`
- Письма пишутся от профиля-победителя; порог очереди применяется к оценке победителя · ТЗ, «не откликаться ниже порога» · `test_a_letter_is_written_from_the_profile_that_won`, `test_a_winner_below_the_floor_gets_no_letter_from_anyone`
- Карточка: `profile_slug`, `margin`, `runner_up_*` в `MatchSummary`; список: `profile_slug`, `?profile=<slug>` · ТЗ п. 4 и переключатель профилей · `test_two_vacancies…`
- Очередь агента: `profile_slug` и `hh_resume_id` в `QueueItem`; они входят в `card_digest`, т. е. подтверждение привязано к резюме · согласие на отклик одним резюме не покрывает другое · `test_the_queue_carries_the_winners_hh_resume_id`
- Без hh_resume_id → set-aside `no_hh_resume` (это `needs_manual` backend), а не запись в `application.agent_status` · очередь по дизайну ничего не пишет (`agent_queue.py`, «Why the queue writes nothing») · `test_a_winner_without_an_hh_resume_id_goes_to_a_person`
- Проверка hh_resume_id не применяется при `require_letter=False` · цепочка решает так, кому писать письма; письмо нужно и для ручного отклика · тот же тест
- Бюджет описаний — один проход reach-правила на каждый профиль, общие бюджеты · объединение топов без двойного кодирования · `test_every_ranking_profile_gets_its_own_walk_default_first`, `test_a_spent_budget_does_not_start_the_next_profiles_walk`
- Замер 6 против 1 — синтетический (227 → 1193, ×5,26), живой не сделан · базы владельца здесь нет · `scripts/measure_description_budget.py --synthetic 3000 --profiles 6`
- План поиска — `plan_for_profiles`, по кругу, потолок `max_queries_per_run × N` (≤ 200) · каждый трек должен попасть в план; темп hh и квоты не читают план · `test_the_plan_looks_for_every_track_default_first`
- `which_resume`/`pick_resume`: исходников в истории нет (`git log --all -S` находит только бриф d6e2c1f со списком удаляемых разовых скриптов) · значит, нужен живой замер — STOP 4 · `git log --all -S pick_resume`
- Агент: `QueueItem.hh_resume_id` разбирается и проверяется по форме; пока выбор резюме не замерен, такой пункт → `needs_manual` в `decide_before_opening` · «пока замера нет … needs_manual, а не отправка»; mandate/gate/human не тронуты · `test_an_item_needing_a_specific_resume_goes_to_a_person_until_the_picker_is_measured`
- Подпись `hh_resume_id` мандатом подготовлена диффом `docs/stop5-mandate-resume.diff`, не применена · STOP 5 · —
- Экран «Мои данные»: панель «Резюме по трекам» (переключатель, вкл/выкл, трек, приоритет, hh-id, «Заменить файл», загрузка нескольких файлов) · ТЗ п. 2 · `npm run typecheck && npm run lint && npm run build`
- Попутно: `wwao/tests/test_chain.py` патчил `cli.chain_command` — ошибка `mypy --strict` была и до фазы · DoD требует чистый mypy · `uv run mypy`

## Фаза B — hh.ru

- Только хост `hh.ru`, без региональных поддоменов · ТЗ: поддомены — только после замера; замер невозможен (сеть) · `test_the_shipped_file_reads_hh_ru_for_remote_work_with_a_night_share`
- robots.txt hh.ru не переписывался в статические правила · транспорт и так читает robots каждого хоста живьём; статические правила hh.ru в `http.py` уже были и только сужают · чтение `http.py` (`RobotsCache`, `QUERYLESS_HOSTS`, `BLOCKED_PATHS`)
- Признак удалёнки — `vacancyView.workFormats`, `REMOTE` · найдено в фикстуре живой страницы `hh_vacancy_full.json` и в `FORMAT_TO_REMOTE`, не догадкой · `test_a_remote_only_host_keeps_remote_postings_and_counts_the_rest`
- `HYBRID` на hh.ru отбрасывается · «только удалёнка» · тот же тест
- Счётчик `not_remote` в `site_finished`, страниц на удалённую = `fetched/stored` · ТЗ «запиши честно»; живого числа нет · тот же тест (6 страниц → 2 вакансии на фикстуре)
- `city_fallback: false` для hh.ru · иначе удалённая вакансия без адреса получила бы город «Россия» и попала бы в fingerprint · `test_a_remote_posting_with_no_address_gets_no_city`
- Токенное ведро — на источник, а не на хост, оставлено как есть · это строже «0,25 на хост», а ускоряться ТЗ запрещает · `test_every_hh_host_shares_one_pace`
- В `night.cities` hh.ru получил `weight: 2`, последним · ночь сначала тратится на KZ; вес — решение, не замер · тест файла
- Дедуп hh.kz/hh.ru — общий `(hh, external_id)` и fingerprint · id у hh общие · `test_one_vacancy_on_both_hosts_is_one_vacancy`

## Фаза C — Telegram

- `access_mode = API` с `terms_url` core.telegram.org/api/terms и выжимкой условий в докстринге · это MTProto, а не страницы; robots.txt неприменим, правило реестра для API выполнено · `test_the_source_registers_with_its_terms`
- `telethon` — зависимость проекта, не extra · коннектор запускается и из процесса сервера (кнопки дашборда); импорт ленивый, без настроек источник неактивен · `test_without_configuration_the_source_is_inactive`
- Чтение за протоколом `ChannelReader`, Telethon — только в `_telegram_client.py`, два вызова (`get_entity`, `get_messages`) · узкая поверхность и тесты без сети/сессии · `test_sources_telegram.py`
- `flood_sleep_threshold=0`: FloodWait ждём сами ровно столько, сколько сказано, один раз, пишем в отчёт; второй — пропуск канала; > 15 мин — не ждём · ТЗ «без ретраев поверх» · `test_a_flood_wait_is_waited_exactly_once_and_reported`, `test_a_second_flood_wait_skips_the_channel_without_more_retries`
- `last_message_id` двигается только после `record_progress` · как у hh: позиция не должна обгонять записанное · `test_the_watermark_moves_only_once_the_postings_are_durable`
- Эвристика: слово вакансии И (контакт ИЛИ деньги); соискатели/дайджесты/реклама — сразу нет · дешёвый этап должен ошибаться в сторону пропуска поста к модели · `test_the_heuristic_keeps_job_posts_and_drops_the_rest`
- Между эвристикой и моделью — ключевые слова плана · Java-вакансия в общем IT-канале не стоит вызова модели для Python-профиля · `test_a_run_yields_vacancies_and_reports_what_the_filter_cut` (2 вызова на 6 постов)
- Нормализатор: в проекте нет LLM-нормализатора вакансий, есть `to_vacancy` · LLM-разбор поста — новый промпт `parse_vacancy_post.md` + `ParsedPost`, дальше обычный `to_vacancy`; фолбэк по правилам · `test_a_failing_model_falls_back_to_the_rules`
- Стартовые 16 каналов — по памяти, `verified: false` · сеть к Telegram закрыта, проверить нельзя; первый прогон помечает мёртвые/ненайденные, скрипт удаляет их из yaml с печатью · `test_dead_and_missing_channels_are_named`
- Мёртвые каналы удаляет скрипт, а не коннектор · коннектор не переписывает собственную конфигурацию во время прогона; ТЗ «не молча» выполняется печатью каждого · `scripts/prune_telegram_channels.py`
- Отклик: `ManualApply` в карточке — пост, контакт, текст для копирования (приветствие + письмо) · ТЗ п. 5; ничего не отправляется · `test_the_card_offers_the_post_the_contact_and_a_message_to_copy`
- Вход: `python -m app.sources.telegram_login`, телефон через `input`, код и пароль через `getpass`, ничего не логируется; путь сессии внутри репозитория — отказ; права 600 · STOP 1 · ручная проверка невозможна без номера
- **STOP 1 (Telegram)**: нужен номер владельца, код и `api_id/api_hash` с my.telegram.org. Всё остальное сделано.
