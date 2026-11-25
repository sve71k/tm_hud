import customtkinter as ctk
import psutil
import pynvml
import os
import json
import threading
import subprocess
from PIL import Image, ImageDraw
from pystray import Icon, Menu, MenuItem
import sys
import socket
import ctypes

# Защита потоков вывода для режима --noconsole
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Определение базовой директории для корректной работы в .exe и как скрипта
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

def load_config():
    default_config = {
        "cpu_template": "{cpu}%",
        "ram_template": "{used} / {total} GB",
        "gpu_template": "{util}% · {temp}°C",
        "font_size": 12,
        "color_text": "#FFFFFF",
        "x": 50,
        "y": 50,
        "move_mode": False
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**default_config, **json.load(f)}
        except Exception:
            return default_config
    return default_config

def save_config(config):
    if "color_label" in config:
        del config["color_label"]
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)

class HardwareMonitor:
    def __init__(self):
        self.gpu_util = 0
        self.gpu_temp = 0
        self.is_nvidia = False
        self.gpu_handle = None
        self.proc = None
        self.init_hardware()

    def init_hardware(self):
        try:
            pynvml.nvmlInit()
            self.gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.is_nvidia = True
        except Exception:
            self.is_nvidia = False
            self._run_typeperf()

    def _run_typeperf(self):
        # 0x08000000 = CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            ['typeperf', r'\GPU Engine(*engtype_3D)\Utilization Percentage', '-si', '1'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            creationflags=0x08000000
        )
        def parse_output():
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                parts = line.strip().split(',')
                if len(parts) >= 2:
                    try:
                        # Проверяем, что первая колонка - число (пропуск заголовков)
                        float(parts[0])
                        vals = []
                        for p in parts[1:]:
                            clean_p = p.strip(' "')
                            if clean_p:
                                vals.append(float(clean_p))
                        self.gpu_util = int(round(sum(vals)))
                    except ValueError:
                        pass
        threading.Thread(target=parse_output, daemon=True).start()

    def get_metrics(self):
        if self.is_nvidia and self.gpu_handle:
            try:
                util = pynvml.nvmlDeviceGetUtilizationRates(self.gpu_handle)
                temp = pynvml.nvmlDeviceGetTemperature(self.gpu_handle, pynvml.NVML_TEMPERATURE_GPU)
                return util.gpu, temp
            except:
                return 0, 0
        return self.gpu_util, None

    def shutdown(self):
        if self.proc:
            self.proc.terminate()

class SystemMonitor(ctk.CTk):
    def __init__(self, instance_socket):
        super().__init__()

        self.instance_socket = instance_socket
        self.hw = HardwareMonitor()

        # Загрузка конфигурации
        self.config = load_config()

        # Проверка координат при запуске
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        if self.config['x'] < 0 or self.config['x'] > screen_w - 60 or \
           self.config['y'] < 0 or self.config['y'] > screen_h - 40:
            self.config['x'] = 50
            self.config['y'] = 50

        # 1. Настройки окна
        self.title("tm__hud")
        self.geometry(f"{250}x{125}+{self.config['x']}+{self.config['y']}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        
        self.configure(fg_color="#000001")
        self.attributes("-transparentcolor", "#000001")

        # Установка иконки для окон с overrideredirect(True)
        self._apply_win32_icon()

        # Режим перемещения
        self.move_mode = False
        self.config['move_mode'] = self.move_mode
        self.offset_x = 0
        self.offset_y = 0

        # 2. Создание элементов интерфейса
        self.hud_container = ctk.CTkFrame(self, fg_color="transparent")
        self.hud_container.pack(fill="both", expand=True, padx=10, pady=10)

        self.cpu_label, self.cpu_val = self._create_metric_row(0, "CPU")
        self.ram_label, self.ram_val = self._create_metric_row(1, "RAM")
        self.gpu_label, self.gpu_val = self._create_metric_row(2, "GPU")

        # Состояния для оптимизации
        self.last_cpu_int = -1
        self.last_ram_float = -1.0
        self.last_gpu_state = (-1, -1)

        # 3. Привязка событий перемещения
        widgets = [self, self.hud_container, self.cpu_label, self.cpu_val, 
                    self.ram_label, self.ram_val, self.gpu_label, self.gpu_val]
        for widget in widgets:
            widget.bind("<ButtonPress-1>", self.start_move)
            widget.bind("<B1-Motion>", self.do_move)
            widget.bind("<ButtonRelease-1>", self.save_coords)

        # Предварительный расчет общего объема ОЗУ
        self.total_ram_gb = f"{psutil.virtual_memory().total / (1024**3):.1f}"

        # Холостой вызов для корректного первого значения CPU
        psutil.cpu_percent(interval=None)

        self.update_stats()

        # Запуск иконки трея
        threading.Thread(target=self.run_tray_icon, daemon=True).start()

    def _apply_win32_icon(self):
        ico_path = resource_path("app.ico")
        if os.path.exists(ico_path):
            # Метод 1: Стандартный tkinter
            self.after(250, lambda: self.iconbitmap(ico_path))
            
            # Метод 2: Win32 API для корректного отображения в панели задач и Alt+Tab
            try:
                user32 = ctypes.windll.user32
                hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
                
                # Загружаем иконку (IMAGE_ICON = 1, LR_LOADFROMFILE = 0x00000010)
                h_icon = user32.LoadImageW(None, ico_path, 1, 0, 0, 0x00000010)
                
                if h_icon:
                    # WM_SETICON = 0x0080
                    # ICON_SMALL = 0, ICON_BIG = 1
                    user32.SendMessageW(hwnd, 0x0080, 0, h_icon)
                    user32.SendMessageW(hwnd, 0x0080, 1, h_icon)
            except Exception:
                pass

    def _create_metric_row(self, row_index, title):
        font_style = ("Consolas", self.config['font_size'], "bold")
        
        label = ctk.CTkLabel(self.hud_container, text=title, font=font_style, 
                              text_color=self.config['color_text'], anchor="w")
        label.grid(row=row_index, column=0, sticky="w", pady=2)
        
        val = ctk.CTkLabel(self.hud_container, text="", font=font_style, 
                            text_color=self.config['color_text'], anchor="w")
        val.grid(row=row_index, column=1, sticky="w", padx=5)
        
        return label, val

    def create_tray_image(self, color_code):
        image = Image.new('RGBA', (64, 64), color=(0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle([4, 4, 60, 60], radius=14, fill="#18181B", outline=color_code, width=3)
        draw.rectangle([16, 34, 23, 48], fill=color_code)
        draw.rectangle([28, 22, 35, 48], fill=color_code)
        draw.rectangle([40, 28, 47, 48], fill=color_code)
        return image

    def start_move(self, event):
        if not self.move_mode:
            return
        self.offset_x = event.x
        self.offset_y = event.y

    def do_move(self, event):
        if not self.move_mode:
            return
        x = self.winfo_x() + (event.x - self.offset_x)
        y = self.winfo_y() + (event.y - self.offset_y)
        self.geometry(f"+{x}+{y}")

    def save_coords(self, event):
        if not self.move_mode:
            return
        self.config['x'] = self.winfo_x()
        self.config['y'] = self.winfo_y()

    def reset_position(self):
        self.config['x'] = 50
        self.config['y'] = 50
        save_config(self.config)
        self.geometry(f"+{self.config['x']}+{self.config['y']}")

    def update_stats(self):
        # CPU
        cpu_usage = psutil.cpu_percent(interval=None)
        curr_cpu = int(cpu_usage)
        if curr_cpu != self.last_cpu_int:
            self.cpu_val.configure(text=self.config['cpu_template'].format(cpu=cpu_usage))
            self.last_cpu_int = curr_cpu

        # RAM
        mem = psutil.virtual_memory()
        used_gb = mem.used / (1024**3)
        curr_ram = round(used_gb, 1)
        if curr_ram != self.last_ram_float:
            self.ram_val.configure(text=self.config['ram_template'].format(used=f"{used_gb:.1f}", total=self.total_ram_gb))
            self.last_ram_float = curr_ram

        # GPU
        gpu_util, gpu_temp = self.hw.get_metrics()
        curr_gpu_state = (int(gpu_util), gpu_temp)
        if curr_gpu_state != self.last_gpu_state:
            if self.hw.is_nvidia:
                new_gpu_text = self.config['gpu_template'].format(util=gpu_util, temp=gpu_temp)
            else:
                new_gpu_text = f"{gpu_util}%"
            
            self.gpu_val.configure(text=new_gpu_text)
            self.last_gpu_state = curr_gpu_state

        self.after(1000, self.update_stats)

    def set_color(self, color_code):
        self.config['color_text'] = color_code
        save_config(self.config)
        
        # Обновляем иконку в трее напрямую (в потоке трея)
        if hasattr(self, 'tray_icon') and self.tray_icon:
            self.tray_icon.icon = self.create_tray_image(color_code)
        
        # Обновляем метки в основном потоке
        def update_ui():
            labels = [self.cpu_label, self.cpu_val, self.ram_label, self.ram_val, self.gpu_label, self.gpu_val]
            for label in labels:
                label.configure(text_color=color_code)
            
        self.after(0, update_ui)

    def toggle_move_mode(self, icon, item_id):
        self.move_mode = not self.move_mode
        self.config['move_mode'] = self.move_mode
        
        def update_ui():
            if self.move_mode:
                self.configure(fg_color="#1E1E1E")
            else:
                self.configure(fg_color="#000001")
                save_config(self.config)
        
        self.after(0, update_ui)

    def quit_app(self):
        try:
            pynvml.nvmlShutdown()
        except:
            pass
        self.hw.shutdown()
        self.instance_socket.close()
        self.destroy()
        sys.exit()

    def run_tray_icon(self):
        colors = [
            ("Белый", "#FFFFFF"),
            ("Серебристый", "#CBD5E1"),
            ("Зелёный (Матрица)", "#00FF66"),
            ("Изумрудный", "#10B981"),
            ("Лаймовый", "#A3E635"),
            ("Бирюзовый (Циан)", "#22D3EE"),
            ("Небесно-голубой", "#38BDF8"),
            ("Золотой", "#FBBF24"),
            ("Оранжевый", "#FB923C"),
            ("Кораллово-красный", "#F87171"),
            ("Неоновый розовый", "#F472B6"),
            ("Лавандовый", "#C084FC")
        ]

        def on_quit(icon, item_id):
            icon.stop()
            self.after(0, self.quit_app)

        def on_reset(icon, item_id):
            self.after(0, self.reset_position)

        def make_color_handler(code):
            return lambda icon, item: self.set_color(code)

        def make_color_checker(code):
            return lambda item: self.config["color_text"] == code

        color_items = [
            MenuItem(name, make_color_handler(code), checked=make_color_checker(code), radio=True)
            for name, code in colors
        ]

        menu_items = []
        menu_items.append(MenuItem("Цвет текста", Menu(*color_items)))
        menu_items.append(MenuItem("Режим перемещения", lambda icon, item: self.toggle_move_mode(icon, item), 
                                    checked=lambda item: self.move_mode))
        menu_items.append(MenuItem("Сбросить позицию", on_reset))
        menu_items.append(MenuItem("Выход", on_quit))

        self.tray_icon = Icon("tm__hud", self.create_tray_image(self.config['color_text']), 
                               title="tm__hud", menu=menu_items)
        self.tray_icon.run()

if __name__ == "__main__":
    try:
        _instance_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _instance_socket.bind(("127.0.0.1", 47291))
    except OSError:
        sys.exit(0)

    app = SystemMonitor(_instance_socket)
    app.mainloop()
