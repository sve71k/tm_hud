# tm__hud

Ультра-минималистичный прозрачный HUD-оверлей для Windows поверх всех окон, отображающий загрузку CPU, RAM и GPU (NVIDIA) в реальном времени.

## Запуск из исходников
```bash
pip install -r requirements.txt
python main.py
```

## Сборка в .exe
```bash
pyinstaller --clean --noconsole --onefile --name "tm__hud" --icon="app.ico" --collect-all customtkinter --hidden-import="pystray._win32" --hidden-import="PIL._tkinter_finder" main.py
```
