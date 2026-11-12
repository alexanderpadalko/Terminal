# Исправление WebSocket в Windows EXE 0.2.26

Основа — актуальный GitHub head `be49895a8e8cbc621c35557b9c2d011432c8e661`.
Сохранены логотипы, упрощённая панель, исправление H1, индикатор канала и
отдельное разрешение HTTP. Номер версии и расчёты индикатора не меняются.

В локальном last_runtime_error.json зарегистрирован сбой от 12.11.2026
21:13:45 МСК: `TypeError: 'lazy_import' object is not callable`.
Стек: WebSocket → asyncio.create_connection → getaddrinfo → run_in_executor.
Исключение возникало до сетевого соединения при создании ThreadPoolExecutor
для DNS. В Python 3.15 публичный экспорт этого класса в concurrent.futures
использует lazy import; в затронутой frozen-сборке он оставался объектом,
который нельзя вызвать, вместо класса.

Исходные определения Python:

- https://github.com/python/cpython/blob/3.15/Lib/concurrent/futures/__init__.py
- https://github.com/python/cpython/blob/3.15/Lib/asyncio/base_events.py

flowdesk.asyncio_runtime явно импортирует конкретный класс из подмодуля thread
и связывает с ним публичный экспорт перед запуском ISS+. Существующий executor
не заменяется. Event-loop policy, адрес сервера, TLS и настройки сети не меняются.

`FlowDesk.exe --diagnose-stream <путь-json>` проверяет тот же IssPlusClient из
готового пакета: executor до/после подготовки, настоящий DNS, STOMP DEMO,
обязательные подписки SBER и не менее двух сообщений M1/котировок без рыночного
HTTP. Затем отменяет клиент, закрывает соединение и завершает процесс.
Обычный запуск приложения не меняется.

tools/check_exe_stream.py запускает эту проверку после упаковки в Windows CI
и build_windows.bat. EXE публикуется после успешной проверки, JSON сохраняется
отдельным Packaged-WebSocket-Evidence. Тесты воспроизводят невызваемый экспорт,
проверяют исправление DNS и сохранение существующего executor.

Галочка HTTP сохраняет прежние правила: без неё не запрашиваются архив,
MINSTEP, список и восстановление пропусков. При отсутствии полного кэша для
первой загрузки истории нужно отдельно включить HTTP. Короткий WebSocket-
снимок не заменяет историю индикатора. Поддержка потокового RFUD этим
исправлением не добавляется.
