# FPGA_board_project
учебный проект сайта дистанционного подключения к платам ПЛИС

## Сервер

Скопируйте `.env.example` в `.env` и задайте параметры БД. Для передачи
прошивок укажите `BOARD_AGENT_URL` (например, `http://192.168.1.50:9000`) и
`BOARD_AGENT_TOKEN`. Адрес должен быть доступен из контейнера backend.

```sh
docker compose up -d --build
```

PostgreSQL, миграции Alembic и backend входят в серверный Compose. Для production
используйте `docker compose -f docker-compose.yaml -f docker-compose.prod.yaml up -d --build`.
В production backend доступен в сети Compose. Для доступа снаружи нужен обратный
прокси в той же сети.

## Компьютер с платой

`integration_FGPA` можно развернуть отдельно. Агент принимает HTTP-запросы
от backend и запускает установленный на компьютере `quartus_pgm` для работы
с USB-Blaster. PostgreSQL и код backend агенту не требуются.

- **Windows.** Установите Quartus и драйвер USB-Blaster. В PowerShell из
  `integration_FGPA` выполните `py -3.12 -m pip install -r requirements.txt`,
  задайте `$env:QUARTUS_PGM` полным путём до `quartus_pgm.exe`, задайте
  `$env:AGENT_TOKEN` и запустите `py -3.12 main.py`.
- **Linux с Docker.** Из `integration_FGPA` скопируйте `.env.example`
  в `.env`. Укажите путь к каталогу Linux-версии Quartus, путь к
  `quartus_pgm` внутри этого каталога и токен. Запустите
  `docker compose up -d --build`. Compose публикует порт агента (по умолчанию
  `9000`) и передаёт доступ к USB.

Значение `AGENT_TOKEN` должно совпадать с `BOARD_AGENT_TOKEN` на сервере.
Откройте порт агента для сервера в локальной сети или VPN.

## Сессии платы и проверка доступности

Скопируйте `backend/board-config/hosts.example.json` в
`backend/board-config/hosts.json`. Для каждого стенда укажите код из БД, адрес
агента и его токен. `board-worker` опрашивает `GET /check` по расписанию
`board-beat`. Интервал и число последовательных неудач задаются через
`BOARD_HEALTH_INTERVAL` и `BOARD_HEALTH_FAILURE_LIMIT`. При достижении порога
плата становится недоступной, а её текущая сессия и задания удаляются. Успешная
проверка восстанавливает доступность. Redis находится во внутренней сети Compose
и не публикует порт.

`POST /board/sessions/{board_code}` открывает сессию, а
`DELETE /board/sessions/{board_code}` закрывает свою сессию. Оба запроса
используют `X-Session-Id`. Обращения к API платы с её `B-Session-Id` продлевают
сессию на час. Просроченные сессии удаляются фоновой задачей.

## Регистрация, вход и список плат

`POST /register` принимает JSON с полями `login` и `password`.
`POST /login` принимает те же поля и возвращает `access_token`.
Передайте его как `Authorization: Bearer <access_token>` при вызове `GET /boards`.

Без параметров `GET /boards` возвращает все платы, зарегистрированные в БД.
Поле `enabled` берётся из `is_enabled` в `hosts.json`. Для платы без записи в
файле оно равно `false`. Поле `is_available` берётся из БД. Для постраничного
запроса можно передать `offset` и `limit`.
