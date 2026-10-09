# ingest-parse

Скан ТН-2 или ТТН-1. Команды из папки проекта. Адреса серверов лежат в `scan.env`.

Полный текст накладной в Markdown:

```powershell
uv run ingest-parse накладная.pdf -o накладная.md --scan-layout --vision
```

Поля бланка в JSON и Markdown:

```powershell
uv run ingest-ttn накладная.pdf -o out_ttn
```
