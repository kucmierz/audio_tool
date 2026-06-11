# AudioTool

Prywatne narzędzie desktopowe (Windows): edytor metadanych MP3 + dzielenie
audiobooków z audioteka.pl wg pliku `.cue`.

## Uruchomienie ze źródeł

```
pip install -r requirements.txt
python main.py
```

## ffmpeg

Przed buildem (i przed użyciem funkcji dzielenia) wrzuć `ffmpeg.exe`
i `ffprobe.exe` do `vendor/ffmpeg/`. Folder jest w `.gitignore` —
binarki nie trafiają do repo.
