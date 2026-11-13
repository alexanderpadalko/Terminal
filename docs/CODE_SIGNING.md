# Подпись FlowDesk Windows — Microsoft Artifact Signing

**Статус:** на 13.11.2026 интеграция с подписанием в GitHub Actions подготовлена,
но сертификат издателя и Azure-профиль ещё **не подключены**.
Пока все сборки имеют имя `...-UNSIGNED`. Ни одну нельзя выдавать за подписанную.

## Для чего подпись

Windows Authenticode подтверждает издателя и целостность `FlowDesk.exe`.
Она помогает репутации SmartScreen, но **не гарантирует мгновенного исчезновения
предупреждения** у нового издателя. Самоподписанный сертификат или отключение
SmartScreen не подходят для публичного выпуска.

## Доступность Microsoft Artifact Signing

Сервис Public Trust доступен организациям из ЕС (включая Латвию) и других
поддерживаемых стран, а физическим лицам — только в США/Канаде.
Нужна платная подписка Azure, проверка личности издателя и профиль Public Trust.
**Физлицу вне США/Канады** следует рассматривать OV/EV-сертификат публичного
УЦ или иной публичный сервис подписи. Azure Private Trust не снимает предупреждения
публичного Windows EXE.

См. официальные документы:
- https://learn.microsoft.com/en-us/azure/artifact-signing/quickstart
- https://github.com/Azure/artifact-signing-action
- https://github.com/Azure/artifact-signing-action/blob/main/docs/OIDC.md

## Как включить Microsoft Artifact Signing

1. В https://portal.azure.com создать Artifact Signing account, пройти
   Identity Validation и создать **Public Trust** certificate profile.
   Записать региональный endpoint, account name и profile name.
2. В Microsoft Entra ID зарегистрировать App / Service Principal.
   Добавить Federated Credential с GitHub subject
   `repo:alexanderpadalko/Terminal:ref:refs/heads/main` (тип Branch, main).
   Дать сервисному принципалу только роль
   **Artifact Signing Certificate Profile Signer**.
3. В GitHub `alexanderpadalko/Terminal` → Settings →
   Secrets and variables → Actions → **Secrets** создать:
   `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_SUBSCRIPTION_ID`.
   Это идентификаторы. Не добавлять секрет приложения Finam или закрытый ключ.
4. Там же → **Variables** создать:
   - `AZURE_ARTIFACT_SIGNING_ENDPOINT` (например,
     `https://weu.codesigning.azure.net` — только если ресурс в этом регионе);
   - `AZURE_ARTIFACT_SIGNING_ACCOUNT` — имя account;
   - `AZURE_ARTIFACT_SIGNING_PROFILE` — имя Public Trust profile;
   - `FLOWDESK_SIGNING_ENABLED` — значение **`true`**, только после настройки.
5. Запустить GitHub Actions workflow **FlowDesk 0.2.26 — Finam Windows CI**
   на `main`. Выпуск `...-SIGNED` произойдёт только после того,
   как `azure/artifact-signing-action@v2` подпишет
   `dist/FlowDesk/FlowDesk.exe` с RFC3161 timestamp и Windows подтвердит
   `Get-AuthenticodeSignature.Status=Valid` и timestamp.

При ошибке авторизации, подписи или проверки подписи процесс падает, ZIP
с суффиксом SIGNED **не публикуется**. Подпись недоступна на pull requests.
Без флага `FLOWDESK_SIGNING_ENABLED=true` выпускается только ZIP
`FlowDesk-0.2.26-Finam-Windows-x64-UNSIGNED`.

## Как проверить результат на Windows

После полной распаковки ZIP открыть PowerShell в папке FlowDesk:

```powershell
Get-AuthenticodeSignature .\FlowDesk.exe |
  Format-List Status,SignerCertificate,TimeStamperCertificate
Get-FileHash .\FlowDesk.exe -Algorithm SHA256
Get-Content .\build-signature.json
```

Для подписанного EXE ожидается `Status : Valid`, установленный
`SignerCertificate` и `TimeStamperCertificate`. SHA256 должен совпасть
с `sha256` в `build-signature.json`, где статус будет `SIGNED`.
Для неактивного сервиса в JSON явно записано `UNSIGNED`.

## Безопасность CI

- Сертификат и закрытый ключ **не** хранятся в GitHub;
  используется Microsoft с OIDC и ограничением строго на `main`.
- Подпись стоит **после** тестов Windows EXE, перед загрузкой artifact.
- В ZIP всегда добавляется `build-signature.json`: версия, commit SHA,
  SHA256 EXE, статус подписи, проверенный издатель при подписании.
- Исходный код FlowDesk, Finam, VWAP и номер версии **0.2.26** не меняются.
