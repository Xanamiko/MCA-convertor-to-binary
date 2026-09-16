# MCA Converter

Конвертирует Minecraft Anvil `.mca` в промежуточный формат для локального игрового сервера.

## Python

Нужен Python 3.13+.

```powershell
py -3.13 -m pip install nbtlib
```

Если `pip` сломан:

```powershell
py -3.13 -m pip install --upgrade pip
py -3.13 -m pip install nbtlib
```

## Запуск

Конвертировать всю папку:

```powershell
py -3.13 mca_converter.py "C:\Minecraft\world\region" -o "C:\STALZONE_LOCAL\data\maps\test"
```

Один регион:

```powershell
py -3.13 mca_converter.py "C:\Minecraft\world\region\r.0.0.mca" -o "C:\STALZONE_LOCAL\data\maps\test"
```

## Что получается

```text
test/
├── metadata.json
├── chunks/
│   ├── chunk_0_0.json
│   └── ...
├── blocks/
│   ├── chunk_0_0.bin
│   └── ...
└── heightmaps/
    ├── chunk_0_0.json
    └── ...
```

`blocks/*.bin` содержит только non-air блоки, поэтому пустые блоки не раздувают файл.

ВАЖНО: это промежуточный формат. Он не является автоматически совместимым форматом клиента STALZONE. Следующий слой должен читать эти файлы и превращать их в тот формат, который реально ожидает клиент.
