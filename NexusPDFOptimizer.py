#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nexus Optimizer 3.x

Ключевые изменения относительно 2.9.x:
- один корневой интерфейс (без рекурсивных mainloop и утечки Tcl-интерпретаторов);
- безопасное сохранение оригиналов: ни один путь кода не удаляет исходник,
  пока не подтверждено, что он сохранён;
- GPU-кодирование видео (Intel QSV / NVENC / AMF / Media Foundation)
  с автоопределением и автоматическим откатом на CPU;
- живой прогресс FFmpeg через -progress pipe:1 (без зависимости от \\r/\\n);
- Скан→PDF: правильный физический размер страницы (A4 / реальный DPI);
- ICC-профили сохраняются, HEIC/HEIF/AVIF поддержаны (pillow-heif);
- рекурсивная обработка папок, выбор папки результата, потоки в UI;
- атомарная запись настроек, лог в %APPDATA%\\NexusOptimizer\\nexus.log,
  глобальный excepthook с окном ошибки;
- отдельная временная папка на каждый запуск (несколько копий не мешают
  друг другу).
"""

from __future__ import annotations

import os
import sys
import json
import time
import re
import atexit
import ctypes
import functools
import socket
import traceback
import threading
import queue
import tempfile
import shutil
import subprocess
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import multiprocessing
from typing import List, Tuple, Optional
from datetime import datetime, timedelta

APP_NAME = "Nexus Optimizer"
APP_VERSION = "3.7.0"
APP_AUTHOR = "Artur Yurchuk"
APP_CREDIT = f"Crafted by {APP_AUTHOR}"

APPDATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "NexusOptimizer"
LOG_FILE = APPDATA_DIR / "nexus.log"

# Project addresses. Donation details are NOT stored in the program: payment
# methods change while an installed copy is updated rarely, and a wallet baked
# into it would one day belong to someone else. The source link is mandatory —
# the program is built with PyMuPDF under the AGPL-3.0.
PROJECT_SITE = "https://nexus.internetdeco.com/"
PROJECT_SUPPORT = "https://nexus.internetdeco.com/#support"
PROJECT_SOURCE = "https://github.com/skin4ik123/NexusOptimizer"


def _early_lang() -> str:
    """Language before the settings are loaded.

    The splash screen and the crash dialog appear before settings.json is read,
    so they used to speak Russian even to someone who had chosen English.
    Reading the language directly costs one small file.
    """
    try:
        with open(APPDATA_DIR / "settings.json", encoding="utf-8") as fh:
            return "en" if json.load(fh).get("lang") == "en" else "ru"
    except Exception:
        return "ru"


BOOT_LANG = _early_lang()


def _setup_logging():
    """Лог в файл всегда; в консоль — только если она есть (не --windowed)."""
    handlers = []
    try:
        APPDATA_DIR.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(
            LOG_FILE, maxBytes=512 * 1024, backupCount=1, encoding="utf-8"))
    except Exception:
        pass
    if getattr(sys, "stderr", None):
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        handlers=handlers or None,
        format="[%(asctime)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


_setup_logging()
log = logging.getLogger("nexus")


def _log_exc(where: str, e: BaseException = None):
    """Логирование исключений вместо молчаливого pass."""
    if e is None:
        log.exception("%s", where)
    else:
        log.warning("%s: %s: %s", where, type(e).__name__, e)


def open_url(url: str) -> None:
    """Opens an address in the default browser.

    No confirmation is asked for: these addresses are written into the program
    itself, and the action was started by the person clicking a button.
    """
    try:
        os.startfile(url)
    except Exception as e:
        _log_exc("open_url", e)


def win_msgbox(text: str, title: str = APP_NAME, flags: int = 0x10):
    """Нативное окно Windows — работает даже когда Tk ещё/уже мёртв."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, str(text), title, flags)
    except Exception:
        pass


def _excepthook(etype, value, tb):
    try:
        log.error("UNCAUGHT: %s", "".join(traceback.format_exception(etype, value, tb)))
    except Exception:
        pass
    try:
        stop = globals().get("_BOOT_SPLASH_STOP")
        if stop is not None:
            stop.set()  # не оставлять заставку висеть поверх ошибки
    except Exception:
        pass
    try:
        # LANG is defined after this handler, so use it only once the
        # dictionaries exist; an early crash falls back to BOOT_LANG.
        lang = globals().get("LANG", BOOT_LANG)
        details = ("\n\nПодробности в логе:\n" if lang == "ru"
                   else "\n\nDetails in the log:\n")
        win_msgbox(f"{etype.__name__}: {value}{details}{LOG_FILE}")
    except Exception:
        pass


sys.excepthook = _excepthook
try:
    threading.excepthook = lambda a: log.error(
        "THREAD UNCAUGHT: %s",
        "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
except Exception:
    pass


def is_frozen() -> bool:
    """PyInstaller, Nuitka и прочие frozen-сборки."""
    if getattr(sys, "frozen", False):
        return True
    if globals().get("__compiled__"):
        return True
    if getattr(sys, "__nuitka_binary_dir", None):
        return True
    return False


# ==================== интеграция с Проводником ====================
# Контекстное меню (только HKCU — без прав администратора) + один
# экземпляр приложения: повторный запуск передаёт пути в уже открытое
# окно через локальный сокет и сразу завершается. Так «выделил 20
# файлов → ПКМ → Сжать» превращается в одно окно со всеми файлами,
# а не в 20 копий программы.
#
# Windows 11: классические verb-пункты живут в «Показать дополнительные
# параметры» (Shift+F10). В новое компактное меню Microsoft пускает
# только подписанные MSIX-пакеты с COM-обработчиком IExplorerCommand,
# поэтому дополнительно регистрируемся в «Открыть с помощью» — этот
# раздел есть и в новом меню Win11.
#
# Блок расположен ДО тяжёлых импортов: процесс-пересыльщик завершается,
# не загружая customtkinter/PIL/fitz.

_SHELL_VERB = "NexusOptimizer.Compress"
_SHELL_PROGID = "NexusOptimizer.File"
_SHELL_APPKEY = "NexusOptimizer.exe"
_SHELL_EXTS_IMG = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp",
                   ".gif", ".jfif", ".jp2")
_SHELL_EXTS_VID = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv",
                   ".flv", ".mpeg", ".mpg", ".ts", ".mts", ".m2ts", ".3gp")
_INSTANCE_FILE = APPDATA_DIR / "instance.json"
_SINGLE_MUTEX = None          # держим handle mutex до конца процесса
_IPC_QUEUE = queue.Queue()    # пути от других запусков → главное окно


def _shell_exts() -> list:
    """Все расширения для пунктов меню (HEIF — если модуль доступен)."""
    exts = [".pdf", *_SHELL_EXTS_IMG, *_SHELL_EXTS_VID]
    try:
        import importlib.util
        if importlib.util.find_spec("pillow_heif") is not None:
            exts += [".heic", ".heif", ".avif"]
    except Exception:
        pass
    return exts


def _shell_label() -> str:
    """Подпись пункта меню: всегда английская — по требованию заказчика
    программа распространяется установщиком на англоязычные машины."""
    return "Compress with Nexus Optimizer"


def _shell_command() -> str:
    exe = os.path.abspath(sys.executable)
    if is_frozen():
        return f'"{exe}" "%1"'
    return f'"{exe}" "{os.path.abspath(__file__)}" "%1"'


def _shell_applies_to() -> str:
    """AQS-фильтр для verb-а на ветке `*`: виден только у наших типов."""
    return " OR ".join('System.FileName:"*%s"' % e for e in _shell_exts())


def _win_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def _hkcu_menu_invisible() -> bool:
    """Windows 11 24H2+ (сборка 26100+) не показывает в меню Проводника
    пользовательские (HKCU) статические пункты — проверено вживую на
    26200: SystemFileAssociations, `*\\shell`, добавки к ProgID и
    Applications игнорируются все. Работают только HKLM и папка SendTo."""
    return _win_build() >= 26100


def _sendto_lnk_names() -> list:
    # первый — актуальное имя; русский вариант оставлен для зачистки
    # ярлыков, созданных ранними версиями
    return ["Compress with Nexus Optimizer.lnk", "Сжать в Nexus Optimizer.lnk"]


def _sendto_dir() -> Path:
    return (Path(os.environ.get("APPDATA", str(Path.home())))
            / "Microsoft" / "Windows" / "SendTo")


def _make_sendto_shortcut() -> bool:
    """Ярлык в «Отправить»: единственный per-user способ, который Windows
    11 24H2+ не фильтрует. Выделенные файлы приходят одним процессом."""
    label = _shell_label()
    lnk = _sendto_dir() / (label + ".lnk")
    exe = os.path.abspath(sys.executable)
    args = "" if is_frozen() else f'"{os.path.abspath(__file__)}"'
    try:
        for old in _sendto_lnk_names():  # не плодить два языка сразу
            p = _sendto_dir() / old
            if p.name != lnk.name and p.exists():
                p.unlink()
    except Exception:
        pass
    try:
        import comtypes.client as cc
        ws = cc.CreateObject("WScript.Shell")
        sc = ws.CreateShortcut(str(lnk))
        sc.TargetPath = exe
        if args:
            sc.Arguments = args
        sc.IconLocation = exe + ",0"
        sc.WorkingDirectory = os.path.dirname(exe)
        sc.Save()
        return True
    except Exception as e:
        _log_exc("sendto comtypes", e)
    try:  # запасной путь — PowerShell (есть на любой Windows 10/11)
        ps = ("$w = New-Object -ComObject WScript.Shell; "
              f"$s = $w.CreateShortcut('{lnk}'); "
              f"$s.TargetPath = '{exe}'; "
              + (f"$s.Arguments = '{args}'; " if args else "")
              + f"$s.IconLocation = '{exe},0'; $s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       creationflags=CNW if "CNW" in globals() else 0,
                       timeout=30, check=True)
        return True
    except Exception as e:
        _log_exc("sendto powershell", e)
        return False


def _remove_sendto_shortcuts() -> None:
    for name in _sendto_lnk_names():
        try:
            p = _sendto_dir() / name
            if p.exists():
                p.unlink()
        except Exception:
            pass


def _reg_del_tree(root, sub) -> None:
    import winreg
    try:
        with winreg.OpenKey(root, sub, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _reg_del_tree(root, sub + "\\" + child)
    except FileNotFoundError:
        return
    try:
        winreg.DeleteKey(root, sub)
    except OSError:
        pass


def _shell_notify() -> None:
    """Попросить Проводник перечитать ассоциации (без перезапуска)."""
    try:
        import ctypes
        # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except Exception:
        pass


def _write_menu_keys(root) -> None:
    """Ключи пункта «Сжать…» под данным корнем (HKCU или HKLM):
    один verb на ветке `*` с AppliesTo-фильтром + verb на папках.
    (Directory\\Background не трогаем: «сжать всё в текущей папке»
    слишком легко нажать случайно.)"""
    import winreg
    label, cmd = _shell_label(), _shell_command()
    exe = os.path.abspath(sys.executable)

    def put(path, value=None, name=None):
        k = winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE)
        if value is not None:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
        winreg.CloseKey(k)

    for base in ("Software\\Classes\\*\\shell\\" + _SHELL_VERB,
                 "Software\\Classes\\Directory\\shell\\" + _SHELL_VERB):
        put(base, label)
        put(base, exe, "Icon")
        # Player поднимает лимит выделения с ~15 до 100 файлов
        put(base, "Player", "MultiSelectModel")
        put(base + "\\command", cmd)
    put("Software\\Classes\\*\\shell\\" + _SHELL_VERB,
        _shell_applies_to(), "AppliesTo")


def _delete_menu_keys(root) -> None:
    _reg_del_tree(root, "Software\\Classes\\*\\shell\\" + _SHELL_VERB)
    _reg_del_tree(root, "Software\\Classes\\Directory\\shell\\" + _SHELL_VERB)


def register_context_menu() -> bool:
    """Пер-пользовательская часть: классический пункт (виден на Win10 и
    Win11 до 24H2), ярлык в «Отправить» (виден ВЕЗДЕ) и «Открыть с
    помощью» (старые сборки). Системную HKLM-часть для Win11 24H2+
    добавляет register_context_menu_hklm() под UAC."""
    try:
        import winreg
        hkcu = winreg.HKEY_CURRENT_USER
        exe = os.path.abspath(sys.executable)
        _write_menu_keys(hkcu)
        _make_sendto_shortcut()

        # подчистить пер-расширенные ключи старых версий (3.6.0)
        for e in _shell_exts():
            _reg_del_tree(hkcu, "Software\\Classes\\SystemFileAssociations\\"
                          + e + "\\shell\\" + _SHELL_VERB)

        def put(path, value=None, name=None):
            k = winreg.CreateKeyEx(hkcu, path, 0, winreg.KEY_WRITE)
            if value is not None:
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
            winreg.CloseKey(k)

        # «Открыть с помощью» — только для фирменного exe: в dev-режиме
        # ключ назывался бы python.exe и перехватил бы чужие ассоциации.
        cmd = _shell_command()
        if is_frozen() and os.path.basename(exe).lower() == "nexusoptimizer.exe":
            app_k = "Software\\Classes\\Applications\\" + _SHELL_APPKEY
            put(app_k, APP_NAME, "FriendlyAppName")
            put(app_k + "\\DefaultIcon", exe)
            put(app_k + "\\shell\\open\\command", cmd)
            for e in _shell_exts():
                put(app_k + "\\SupportedTypes", "", e)
            pg = "Software\\Classes\\" + _SHELL_PROGID
            put(pg, APP_NAME)
            put(pg + "\\DefaultIcon", exe)
            put(pg + "\\shell\\open\\command", cmd)
            for e in _shell_exts():
                put("Software\\Classes\\" + e + "\\OpenWithProgids",
                    "", _SHELL_PROGID)

        _shell_notify()
        log.info("context menu registered -> %s", cmd)
        return True
    except Exception as e:
        _log_exc("register menu", e)
        return False


def register_context_menu_hklm() -> bool:
    """Системная часть (нужны права администратора): на Win11 24H2+
    только она видна в классическом меню. Вызов: --register-menu-hklm."""
    try:
        import winreg
        _write_menu_keys(winreg.HKEY_LOCAL_MACHINE)
        _shell_notify()
        log.info("context menu registered in HKLM")
        return True
    except Exception as e:
        _log_exc("register menu hklm", e)
        return False


def unregister_context_menu_hklm() -> bool:
    try:
        import winreg
        _delete_menu_keys(winreg.HKEY_LOCAL_MACHINE)
        _shell_notify()
        log.info("context menu removed from HKLM")
        return True
    except Exception as e:
        _log_exc("unregister menu hklm", e)
        return False


def _elevate_menu_flag(flag: str) -> None:
    """Запустить себя с повышением прав для HKLM-части (запрос UAC).
    Отказ пользователя не ошибка: остаются «Отправить» и HKCU-ключи."""
    try:
        import ctypes
        exe = os.path.abspath(sys.executable)
        params = flag if is_frozen() else f'"{os.path.abspath(__file__)}" {flag}'
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, None, 0)
        log.info("elevate %s rc=%s", flag, rc)
    except Exception as e:
        _log_exc("elevate menu", e)


def unregister_context_menu() -> bool:
    """Убрать пер-пользовательские ключи, ярлык «Отправить» и, если есть
    права, HKLM-часть; чужие OpenWithProgids не трогаем."""
    try:
        import winreg
        hkcu = winreg.HKEY_CURRENT_USER
        _delete_menu_keys(hkcu)
        _remove_sendto_shortcuts()
        for e in _shell_exts() + [".heic", ".heif", ".avif"]:
            _reg_del_tree(hkcu, "Software\\Classes\\SystemFileAssociations\\"
                          + e + "\\shell\\" + _SHELL_VERB)
            try:
                with winreg.OpenKey(hkcu, "Software\\Classes\\" + e
                                    + "\\OpenWithProgids", 0,
                                    winreg.KEY_WRITE) as k:
                    winreg.DeleteValue(k, _SHELL_PROGID)
            except OSError:
                pass
        _reg_del_tree(hkcu,
                      "Software\\Classes\\Applications\\" + _SHELL_APPKEY)
        _reg_del_tree(hkcu, "Software\\Classes\\" + _SHELL_PROGID)
        try:  # сработает, только если процесс уже с правами админа
            _delete_menu_keys(winreg.HKEY_LOCAL_MACHINE)
        except Exception:
            pass
        _shell_notify()
        log.info("context menu unregistered")
        return True
    except Exception as e:
        _log_exc("unregister menu", e)
        return False


def _menu_key_exists(root) -> bool:
    import winreg
    try:
        with winreg.OpenKey(root, "Software\\Classes\\*\\shell\\"
                            + _SHELL_VERB):
            return True
    except OSError:
        return False


def is_context_menu_registered() -> bool:
    try:
        import winreg
        return (_menu_key_exists(winreg.HKEY_CURRENT_USER)
                or _menu_key_exists(winreg.HKEY_LOCAL_MACHINE))
    except Exception:
        return False


def context_menu_health_check() -> None:
    """После обновления/переноса exe пер-пользовательская часть меню
    чинит сама себя (HKLM не трогаем — не выпрашивать UAC на старте)."""
    if not is_frozen():
        return
    try:
        import winreg
        if not _menu_key_exists(winreg.HKEY_CURRENT_USER):
            return
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            "Software\\Classes\\*\\shell\\" + _SHELL_VERB
                            + "\\command") as k:
            stored = winreg.QueryValueEx(k, None)[0]
        if str(stored).strip().lower() != _shell_command().strip().lower():
            register_context_menu()
    except Exception as e:
        _log_exc("menu health", e)


# ---- один экземпляр + передача путей через локальный сокет ----

def _single_instance_acquire() -> bool:
    """True — мы первый экземпляр; False — программа уже запущена."""
    global _SINGLE_MUTEX
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "Local\\NexusOptimizer.Single")
        if not h:
            return True
        if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _SINGLE_MUTEX = h
        return True
    except Exception:
        return True


def _forward_to_running(paths, tries=40) -> bool:
    """Отдать пути работающему экземпляру. Ретраи покрывают момент,
    когда главный процесс ещё стартует и сокет не поднят (до ~10 с)."""
    payload = (json.dumps({"paths": [os.path.abspath(p) for p in paths],
                           "activate": True}) + "\n").encode("utf-8")
    for _ in range(tries):
        try:
            with open(_INSTANCE_FILE, "r", encoding="utf-8") as f:
                port = int(json.load(f)["port"])
            with socket.create_connection(("127.0.0.1", port),
                                          timeout=1.0) as c:
                c.settimeout(3.0)
                c.sendall(payload)
                if c.recv(4)[:2] == b"OK":
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _start_instance_server() -> None:
    """Главный экземпляр: слушаем 127.0.0.1 и складываем пути в очередь."""
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)
        port = srv.getsockname()[1]
        tmp = str(_INSTANCE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"port": port, "pid": os.getpid()}, f)
        os.replace(tmp, _INSTANCE_FILE)
    except Exception as e:
        _log_exc("ipc server", e)
        return

    def _cleanup():
        try:
            srv.close()
        except Exception:
            pass
        try:  # файл удаляем только если он наш (и только закрыв его:
            # Windows не удаляет открытые файлы)
            with open(_INSTANCE_FILE, "r", encoding="utf-8") as f:
                mine = int(json.load(f).get("pid", -1)) == os.getpid()
            if mine:
                os.unlink(_INSTANCE_FILE)
        except Exception:
            pass

    atexit.register(_cleanup)

    def _serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # сокет закрыт при выходе
            try:
                conn.settimeout(3)
                buf = b""
                while b"\n" not in buf and len(buf) < (1 << 20):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                _IPC_QUEUE.put(json.loads(buf.decode("utf-8")))
                conn.sendall(b"OK")
            except Exception as e:
                _log_exc("ipc msg", e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    threading.Thread(target=_serve, daemon=True, name="ipc").start()
    log.info("ipc listening on port %s", port)


def _early_shell_cli() -> None:
    """Флаги регистрации меню (для установщика и деинсталлятора).
    Выполняется до одноэкземплярной логики и до тяжёлых импортов.
    *-hklm-варианты запускаются установщиком/настройками с повышением
    прав (Verb runas): на Win11 24H2+ виден только системный пункт."""
    args = sys.argv[1:]
    if "--register-menu-hklm" in args:
        os._exit(0 if register_context_menu_hklm() else 1)
    if "--unregister-menu-hklm" in args:
        os._exit(0 if unregister_context_menu_hklm() else 1)
    if "--register-menu" in args or "--unregister-menu" in args:
        ok = (register_context_menu() if "--register-menu" in args
              else unregister_context_menu())
        os._exit(0 if ok else 1)


_early_shell_cli()

# пути из командной строки (контекстное меню передаёт по одному на процесс)
_CLI_PATHS = [a for a in sys.argv[1:] if not a.startswith("-")]

# один экземпляр — только во frozen-сборке (dev-запуски и smoke-тесты
# не мешают друг другу). NEXUS_MULTI=1 принудительно разрешает копии.
if ((is_frozen() or os.environ.get("NEXUS_SINGLE") == "1")
        and os.environ.get("NEXUS_MULTI") != "1"
        and not _single_instance_acquire()):
    if _forward_to_running(_CLI_PATHS):
        os._exit(0)
    # главное окно не ответило (зависло?) — работаем отдельной копией


def _die(msg: str):
    log.error("FATAL: %s", msg)
    if getattr(sys, "stdout", None):
        print(msg)
        try:
            input("Press Enter...")
        except Exception:
            pass
    else:
        win_msgbox(msg)
    sys.exit(1)


# --- собственная заставка загрузки: чёткая (DPI-aware), с бегущей
# полосой прогресса снизу. Стартует в отдельном потоке сразу после
# распаковки onefile и сменяет статичную картинку загрузчика; гаснет,
# когда готово главное окно. Дизайн зеркалит make_splash.py. ---
_BOOT_SPLASH_STOP = threading.Event()


def _boot_splash_thread(ready: threading.Event):
    try:
        import tkinter as _tk
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        root = _tk.Tk()
        # ВАЖНО: не оставаться default root — иначе CTkImage главного окна
        # привяжутся к этому интерпретатору (TclError: image "pyimage1")
        _tk._default_root = None
        root.withdraw()
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        try:
            s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:
            s = 1.0

        def px(v):
            return int(v * s)

        W, H = px(460), px(240)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")
        cv = _tk.Canvas(root, width=W, height=H, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)

        # фон: вертикальный градиент графита с бирюзовым подтоном
        top_c, bot_c = (16, 32, 29), (13, 20, 19)
        for i in range(H):
            f = i / max(1, H - 1)
            r = int(top_c[0] + (bot_c[0] - top_c[0]) * f)
            g = int(top_c[1] + (bot_c[1] - top_c[1]) * f)
            b = int(top_c[2] + (bot_c[2] - top_c[2]) * f)
            cv.create_line(0, i, W, i, fill=f"#{r:02x}{g:02x}{b:02x}")

        def rrect(x1, y1, x2, y2, rad, fill):
            cv.create_rectangle(x1 + rad, y1, x2 - rad, y2, fill=fill,
                                outline=fill)
            cv.create_rectangle(x1, y1 + rad, x2, y2 - rad, fill=fill,
                                outline=fill)
            for cx_, cy_, st in ((x1, y1, 90), (x2 - 2 * rad, y1, 0),
                                 (x1, y2 - 2 * rad, 180),
                                 (x2 - 2 * rad, y2 - 2 * rad, 270)):
                cv.create_arc(cx_, cy_, cx_ + 2 * rad, cy_ + 2 * rad,
                              start=st, extent=90, fill=fill, outline=fill)

        cx = W // 2
        rrect(cx - px(28), px(52), cx + px(28), px(108), px(14), "#2fd4c2")
        cv.create_text(cx, px(80), text="N", fill="#082622",
                       font=("Segoe UI", -px(34), "bold"))
        cv.create_text(cx, px(148), text="Nexus Optimizer", fill="#e8eeec",
                       font=("Segoe UI", -px(30), "bold"))
        cv.create_text(cx, px(180),
                       text=("PDF · Фото · Видео · Сканер" if BOOT_LANG == "ru"
                             else "PDF · Photo · Video · Scanner"),
                       fill="#6f807b", font=("Segoe UI", -px(16)))
        # подпись автора: «Crafted by» приглушённо, имя — светлее
        import tkinter.font as _tkfont
        f_small = ("Segoe UI", -px(11))
        credit_w = _tkfont.Font(root=root, family="Segoe UI",
                                size=-px(11)).measure("Crafted by ")
        cv.create_text(px(12), H - px(15), text="Crafted by ",
                       fill="#44534f", font=f_small, anchor="w")
        cv.create_text(px(12) + credit_w, H - px(15), text=APP_AUTHOR,
                       fill="#7f948e", font=f_small, anchor="w")
        cv.create_text(W - px(12), H - px(15), text=f"v{APP_VERSION}",
                       fill="#44534f", font=f_small, anchor="e")

        # дорожка + бегущий сегмент прогресса
        track_h = px(5)
        cv.create_rectangle(0, H - track_h, W, H, fill="#1d2b28",
                            outline="#1d2b28")
        seg_w = px(120)
        seg = cv.create_rectangle(0, H - track_h, seg_w, H, fill="#2fd4c2",
                                  outline="#2fd4c2")
        pos = {"x": -seg_w}
        step = max(4, (W + seg_w) // 55)

        def tick():
            if _BOOT_SPLASH_STOP.is_set():
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            pos["x"] += step
            if pos["x"] > W:
                pos["x"] = -seg_w
            cv.coords(seg, pos["x"], H - track_h, pos["x"] + seg_w, H)
            root.after(16, tick)

        root.deiconify()
        root.update_idletasks()
        ready.set()
        tick()
        root.mainloop()
    except Exception as e:
        _log_exc("boot splash", e)
    finally:
        ready.set()


if getattr(sys, "frozen", False):
    _ready = threading.Event()
    threading.Thread(target=_boot_splash_thread, args=(_ready,),
                     daemon=True).start()
    _ready.wait(1.5)  # заставка на экране до начала тяжёлых импортов

try:
    import customtkinter as ctk
except ImportError:
    _die("Не установлен customtkinter:  pip install customtkinter")

try:
    from PIL import Image, ImageOps, ImageStat
    Image.MAX_IMAGE_PIXELS = 250_000_000
except ImportError:
    _die("Не установлен pillow:  pip install pillow")

# HEIC/HEIF/AVIF (фото со смартфонов) — опционально, но кладётся в сборку
HAS_HEIF = False
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HAS_HEIF = True
except Exception as e:
    _log_exc("pillow_heif", e)


class _LazyMod:
    """Импорт модуля только при первом обращении — быстрее старт UI."""

    def __init__(self, name: str):
        self._name = name
        self._mod = None
        self._failed = False

    def _load(self):
        if self._failed:
            return None
        if self._mod is None:
            try:
                self._mod = __import__(self._name)
            except ImportError as e:
                self._failed = True
                _log_exc(f"lazy import {self._name}", e)
                return None
        return self._mod

    def __bool__(self):
        return self._load() is not None

    def __getattr__(self, item):
        mod = self._load()
        if mod is None:
            raise AttributeError(self._name)
        return getattr(mod, item)


fitz = _LazyMod("fitz")
pikepdf = _LazyMod("pikepdf")

# Drag&Drop: tkinterdnd2 (нативный OLE, юникод-пути). windnd удалён —
# он подменял WndProc через ctypes с обрезкой LRESULT на x64 и вызывал
# Python синхронно изнутри WndProc: зависание и падение при перетаскивании.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_TKDND = True
except ImportError:
    HAS_TKDND = False

    class TkinterDnD:  # заглушка для наследования App
        class DnDWrapper:
            pass

from tkinter import filedialog, messagebox
import tkinter as tk

try:
    import winsound
    HAS_SOUND = True
except ImportError:
    HAS_SOUND = False

# -------------------- i18n --------------------
LANG = BOOT_LANG  # runtime, loaded from settings

TRANSLATIONS = {
    "ru": {
        "pdf": "PDF",
        "photo": "Фото",
        "video": "Видео",
        "scanner": "Сканер",
        "history": "История операций",
        "ffmpeg_ok": "FFmpeg: найден",
        "ffmpeg_no": "FFmpeg: не найден (нужен для видео)",
        "ready": "Готов",
        "theme": "Тема",
        "th_dark": "Тёмная",
        "th_light": "Светлая",
        "ctx_menu": "Пункт «Compress» в меню Проводника",
        "ctx_hint": "Win11: в «Показать дополнительные параметры»;\nна 24H2+ нужен один запрос UAC, плюс всегда\nдоступно через «Отправить»",
        "ctx_fail": "Не удалось изменить меню Проводника.\nПодробности в логе.",
        "add_files": "Добавить файлы",
        "remove": "Убрать из списка",
        "stop": "Остановить",
        "stopping": "Остановка...",
        "stopped": "Остановлено",
        "done": "Готово",
        "error": "Ошибка",
        "history_title": "История операций",
        "history_empty": "История пуста",
        "history_open": "Открыть папку",
        "history_clear": "Очистить историю",
        "scan_pages": "Отсканированные страницы",
        "scan_btn": "Сканировать…",
        "save_pdf": "Сохранить PDF",
        "save_jpg": "Сохранить JPG",
        "save_files": "Сохранить по файлам",
        "compress_doc": "Сжимать документ",
        "folder": "Папка сохранения",
        "refresh_dev": "Обновить список",
        "scan_hint": "Настройки DPI, цвета и качества —\nв окне драйвера сканера",
        "del_temp": "Удалить временные файлы",
        "remove_sel": "Убрать выбранные",
        "clear_list": "Очистить список",
        "comp_settings": "Сжатие при сохранении",
        "gray_pdf": "Серый режим (PDF)",
        "gray_scan_pdf": "Серый режим (для сканов PDF)",
        "all_cores": "Все ядра CPU",
        "save_original": "Сохранить оригинал в папку original",
        "compress_selected_pdf": "Сжать выбранные PDF",
        "compress_selected_photo": "Сжать выбранные фото",
        "compress_selected_video": "Сжать выбранные видео",
        "merge_pdf": "Объединить в один PDF",
        "preset": "Профиль",
        "codec": "Кодек",
        "container": "Контейнер",
        "encode_speed": "Скорость кодирования",
        "max_height": "Макс. высота (0=как есть)",
        "audio_kbps": "Аудио кбит/с (0=без звука)",
        "filename_template": "Имя сжатого файла",
        "name_hint": "пусто = как оригинал | {name} {date} {time}",
        "video_hint": "AVI→только H.264 | WebM→VP9/AV1 | CRF меньше = лучше качество",
        "smart_pdf": "Умный (рекомендуется)",
        "aggressive": "Агрессивный",
        "max_compress": "Максимальный",
        "balanced": "Сбалансированный",
        "quality": "Качество",
        "custom": "Свои",
        "waiting": "Ожидание...",
        "disk_low": "Мало места на диске",
        "clear_pages_q": "Очистить список страниц?",
        "structure_only": "Только структура PDF",
        "images_only": "Сильнее картинки",
        "workers": "Потоков",
        "pdf_files": "PDF-файлы и папки",
        "photo_files": "Фото и папки",
        "video_files": "Видеофайлы и папки",
        "quality_short": "Кач.",
        "photo_quality": "Качество (чем меньше — тем сильнее сжатие)",
        "format": "Формат",
        "max_side": "Макс. ширина/высота (пиксели)",
        "no_resize": "0 = не менять размер",
        "smart_photo": "Авто-режим (фото/скрин)",
        "ready_profile": "Готовый профиль",
        "no_files": "Нет файлов",
        "add_pdfs": "Добавьте PDF-файлы",
        "add_photos": "Добавьте фото",
        "add_videos": "Добавьте видео",
        "pdf_created": "PDF создан:",
        "ffmpeg_missing": "FFmpeg не найден.\n\nПересоберите приложение с imageio-ffmpeg\nили скачайте https://www.gyan.dev/ffmpeg/builds/\nи добавьте bin в PATH.",
        "no_pages": "Нет страниц.",
        "no_pages_scan": "Нет страниц. Нажмите «Сканировать…».",
        "win_dialog": "Системный диалог Windows…",
        "scanning": "Окно сканера...",
        "aggressive_scans": "Агрессивный (сканы)",
        "max_compression": "Максимальное сжатие",
        "high_quality": "Высокое качество",
        "custom_params": "Свои параметры",
        "balance": "Баланс",
        # --- новые ключи v3 ---
        "out_dir": "Папка результата",
        "out_same": "пусто = рядом с исходником",
        "recurse": "Включая подпапки",
        "auto": "Авто",
        "hw_accel": "GPU-ускорение (если доступно)",
        "hw_found": "GPU-кодировщик: ",
        "hw_none": "GPU-кодировщик не найден — кодирование на CPU",
        "hw_fallback": "GPU не справился — повтор на CPU…",
        "open_after": "Открывать папку после сохранения",
        "sound_done": "Звук по завершении",
        "outdir_note": "Результат в отдельную папку — оригиналы остаются на месте",
        "err_not_found": "Файл не найден",
        "err_empty": "пустой файл",
        "err_too_long": "путь слишком длинный",
        "err_no_space": "Мало места на диске",
        "err_no_ffmpeg": "FFmpeg не найден",
        "err_protected": "PDF защищён паролем — сжатие невозможно",
        "err_no_gain": "Почти без выигрыша — оригинал не тронут",
        "err_no_access": "Нет доступа к файлу",
        "err_pymupdf": "Нужен PyMuPDF",
        "err_no_write": "Нет прав на запись в папку",
        "err_dir": "Папка недоступна",
        "err_add_pdf": "Не удалось добавить PDF",
        "err_add_pages": "Не удалось добавить ни одной страницы",
        "single_pdf_note": "один файл → параллель по страницам",
        "encoding_one": "Кодирую: ",
        "run_parallel": "Параллельно: ",
        "no_avail_files": "Нет доступных файлов для обработки",
        "imgp_max": "Макс. сжатие (800px, q55)",
        "imgp_msg": "Для мессенджеров (1280px, q65)",
        "imgp_bal": "Баланс (1200px, q75)",
        "imgp_good": "Хорошее качество (1600px, q82)",
        "imgp_high": "Высокое качество (2048px, q88)",
        "imgp_keep": "Без изменения размера",
        "vidp_arch_max": "H.264 архив макс. (CRF 32, 720p)",
        "vidp_strong": "H.264 сильное сжатие (CRF 28, 720p)",
        "vidp_compact": "H.264 компактный (CRF 26)",
        "vidp_work": "H.264 рабочий (CRF 24)",
        "vidp_quality": "H.264 качество (CRF 20)",
        "vidp_h265_arch": "H.265 архив (CRF 30, 720p)",
        "vidp_h265_eff": "H.265 эффективный (CRF 26)",
        "vidp_av1": "AV1 макс. эффективность",
        "vidp_vp9": "VP9 WebM",
        # --- v3.1: сайдбар и настройки ---
        "settings": "Настройки",
        "language": "Язык",
        "more_settings": "Ещё настройки",
        "more_pdf": "имя файла, папка результата, оригиналы, потоки",
        "more_img": "качество, формат, размер, имя, папка",
        "more_vid": "кодек, CRF, звук, имя, папка",
        "empty_files_hint": "Здесь пока пусто\nПеретащите файлы или папку — или нажмите «Добавить файлы»",
        "drop_strip": "Перетащите файлы в любое место окна  ·  Ctrl+O — добавить",
        "hint_start": "Ctrl+Enter — старт",
        "stop_esc": "Стоп · Esc",
        "sort_added": "По добавлению",
        "sort_name": "По имени",
        "sort_size": "По размеру",
        "empty_pages_hint": "Нажмите «Сканировать…» —\nстраницы появятся здесь",
        "history_empty_hint": "Здесь появятся выполненные операции",
        # --- v3.2: строки-карточки, сводка, тосты ---
        "queued": "в очереди",
        "sum_idle": "Файлов: {n} · {size}",
        "sum_idle_est": "Файлов: {n} · {size} → ≈ {est}",
        "sum_run": "Готово {done}/{total} · −{size}",
        "sum_done": "Готово {ok}/{total} · сэкономлено {size}",
        "toast_done": "Готово · {ok}/{total} · −{size}",
        "total_saved": "сэкономлено за всё время",
        "files_done": "файлов сжато",
        "operations": "операций",
        "today": "Сегодня",
        "yesterday": "Вчера",
        "skipped_n": "пропущено {n}",
        # --- v3.7: поддержка проекта и исходный код ---
        "support": "Поддержать",
        "support_title": "Поддержать проект",
        "support_text": "Nexus Optimizer бесплатен и таким останется: "
                        "без подписки, без платной версии и без рекламы.\n\n"
                        "Если программа сэкономила вам время — кофе будет честным обменом.",
        "support_open": "Открыть страницу поддержки",
        "source_code": "Исходный код",
        "source_hint": "Программа свободная, под лицензией AGPL-3.0",
        "close": "Закрыть",
        "fallback_single": "→ в один поток",
    },
    "en": {
        "pdf": "PDF",
        "photo": "Photos",
        "video": "Video",
        "scanner": "Scanner",
        "history": "History",
        "ffmpeg_ok": "FFmpeg: found",
        "ffmpeg_no": "FFmpeg: not found (required for video)",
        "ready": "Ready",
        "theme": "Theme",
        "th_dark": "Dark",
        "th_light": "Light",
        "ctx_menu": "“Compress” in Explorer context menu",
        "ctx_hint": "Win11: under “Show more options”;\non 24H2+ one UAC prompt is needed, plus\nalways available via “Send to”",
        "ctx_fail": "Could not update the Explorer menu.\nSee log for details.",
        "add_files": "Add files",
        "remove": "Remove",
        "stop": "Stop",
        "stopping": "Stopping...",
        "stopped": "Stopped",
        "done": "Done",
        "error": "Error",
        "history_title": "Operation history",
        "history_empty": "History is empty",
        "history_open": "Open folder",
        "history_clear": "Clear history",
        "scan_pages": "Scanned pages",
        "scan_btn": "Scan…",
        "save_pdf": "Save PDF",
        "save_jpg": "Save JPG",
        "save_files": "Save as files",
        "compress_doc": "Compress document",
        "folder": "Output folder",
        "refresh_dev": "Refresh list",
        "scan_hint": "DPI, color and quality are set\nin the scanner driver window",
        "del_temp": "Delete temp files",
        "remove_sel": "Remove selected",
        "clear_list": "Clear list",
        "comp_settings": "Compression on save",
        "gray_pdf": "Grayscale (PDF)",
        "gray_scan_pdf": "Grayscale (for scanned PDFs)",
        "all_cores": "All CPU cores",
        "save_original": "Move originals to original/",
        "compress_selected_pdf": "Compress selected PDFs",
        "compress_selected_photo": "Compress selected photos",
        "compress_selected_video": "Compress selected videos",
        "merge_pdf": "Merge into one PDF",
        "preset": "Preset",
        "codec": "Codec",
        "container": "Container",
        "encode_speed": "Encode speed",
        "max_height": "Max height (0=keep)",
        "audio_kbps": "Audio kbps (0=mute)",
        "filename_template": "Output name template",
        "name_hint": "empty = original name | {name} {date} {time}",
        "video_hint": "AVI→H.264 only | WebM→VP9/AV1 | lower CRF = better quality",
        "smart_pdf": "Smart (recommended)",
        "aggressive": "Aggressive",
        "max_compress": "Maximum",
        "balanced": "Balanced",
        "quality": "Quality",
        "custom": "Custom",
        "waiting": "Waiting...",
        "disk_low": "Not enough disk space",
        "clear_pages_q": "Clear scanned pages list?",
        "structure_only": "PDF structure only",
        "images_only": "Compress images harder",
        "workers": "Workers",
        "pdf_files": "PDF files and folders",
        "photo_files": "Photos and folders",
        "video_files": "Video files and folders",
        "quality_short": "Qual.",
        "photo_quality": "Quality (lower = stronger compression)",
        "format": "Format",
        "max_side": "Max width/height (px)",
        "no_resize": "0 = do not resize",
        "smart_photo": "Auto mode (photo/screenshot)",
        "ready_profile": "Preset profile",
        "no_files": "No files",
        "add_pdfs": "Add PDF files",
        "add_photos": "Add photos",
        "add_videos": "Add videos",
        "pdf_created": "PDF created:",
        "ffmpeg_missing": "FFmpeg not found.\n\nRebuild the app with imageio-ffmpeg\nor download https://www.gyan.dev/ffmpeg/builds/\nand add bin to PATH.",
        "no_pages": "No pages.",
        "no_pages_scan": "No pages. Click Scan… first.",
        "win_dialog": "Windows scanner dialog…",
        "scanning": "Scanner window...",
        "aggressive_scans": "Aggressive (scans)",
        "max_compression": "Maximum compression",
        "high_quality": "High quality",
        "custom_params": "Custom settings",
        "balance": "Balance",
        # --- new v3 keys ---
        "out_dir": "Output folder",
        "out_same": "empty = next to the source file",
        "recurse": "Include subfolders",
        "auto": "Auto",
        "hw_accel": "GPU acceleration (if available)",
        "hw_found": "GPU encoder: ",
        "hw_none": "No GPU encoder found — using CPU",
        "hw_fallback": "GPU failed — retrying on CPU…",
        "open_after": "Open folder after saving",
        "sound_done": "Sound when done",
        "outdir_note": "Output goes to a separate folder — originals stay in place",
        "err_not_found": "File not found",
        "err_empty": "empty file",
        "err_too_long": "path too long",
        "err_no_space": "Not enough disk space",
        "err_no_ffmpeg": "FFmpeg not found",
        "err_protected": "PDF is password-protected",
        "err_no_gain": "No real gain — original kept",
        "err_no_access": "Access denied",
        "err_pymupdf": "PyMuPDF is required",
        "err_no_write": "No write permission for the folder",
        "err_dir": "Folder unavailable",
        "err_add_pdf": "Could not add PDF",
        "err_add_pages": "Could not add any pages",
        "single_pdf_note": "single file → parallel pages",
        "encoding_one": "Encoding: ",
        "run_parallel": "In parallel: ",
        "no_avail_files": "No processable files",
        "imgp_max": "Max compression (800px, q55)",
        "imgp_msg": "Messengers (1280px, q65)",
        "imgp_bal": "Balanced (1200px, q75)",
        "imgp_good": "Good quality (1600px, q82)",
        "imgp_high": "High quality (2048px, q88)",
        "imgp_keep": "Keep original size",
        "vidp_arch_max": "H.264 max archive (CRF 32, 720p)",
        "vidp_strong": "H.264 strong (CRF 28, 720p)",
        "vidp_compact": "H.264 compact (CRF 26)",
        "vidp_work": "H.264 standard (CRF 24)",
        "vidp_quality": "H.264 quality (CRF 20)",
        "vidp_h265_arch": "H.265 archive (CRF 30, 720p)",
        "vidp_h265_eff": "H.265 efficient (CRF 26)",
        "vidp_av1": "AV1 max efficiency",
        "vidp_vp9": "VP9 WebM",
        # --- v3.1: sidebar and settings ---
        "settings": "Settings",
        "language": "Language",
        "more_settings": "More settings",
        "more_pdf": "file name, output folder, originals, workers",
        "more_img": "quality, format, size, name, folder",
        "more_vid": "codec, CRF, audio, name, folder",
        "empty_files_hint": "Nothing here yet\nDrop files or a folder — or click Add files",
        "drop_strip": "Drop files anywhere in the window  ·  Ctrl+O — add",
        "hint_start": "Ctrl+Enter — start",
        "stop_esc": "Stop · Esc",
        "sort_added": "By added",
        "sort_name": "By name",
        "sort_size": "By size",
        "empty_pages_hint": "Click Scan… —\npages will appear here",
        "history_empty_hint": "Finished operations will appear here",
        # --- v3.2: file cards, summary, toasts ---
        "queued": "queued",
        "sum_idle": "Files: {n} · {size}",
        "sum_idle_est": "Files: {n} · {size} → ≈ {est}",
        "sum_run": "Done {done}/{total} · −{size}",
        "sum_done": "Done {ok}/{total} · saved {size}",
        "toast_done": "Done · {ok}/{total} · −{size}",
        "total_saved": "saved in total",
        "files_done": "files compressed",
        "operations": "operations",
        "today": "Today",
        "yesterday": "Yesterday",
        "skipped_n": "skipped {n}",
        # --- v3.7: support and source code ---
        "support": "Support",
        "support_title": "Support the work",
        "support_text": "Nexus Optimizer is free and stays free: "
                        "no subscription, no paid tier, no ads.\n\n"
                        "If it saved you an hour, a coffee is a fair trade.",
        "support_open": "Open the support page",
        "source_code": "Source code",
        "source_hint": "Free software, licensed under the AGPL-3.0",
        "close": "Close",
        "fallback_single": "→ single thread",
    },
}


def t(key: str) -> str:
    return TRANSLATIONS.get(LANG, TRANSLATIONS["ru"]).get(key, key)


def tr_msg(msg: str) -> str:
    """Сообщения из воркеров приходят кодами (err_*) — переводим при показе."""
    if not msg:
        return msg
    d = TRANSLATIONS.get(LANG, TRANSLATIONS["ru"])
    return d.get(msg, msg)


def _localize_mode(mode: str) -> str:
    """История могла сохраняться на русском — показываем на текущем языке."""
    if not mode:
        return "—"
    key = str(mode).strip().lower()
    mapping = {
        "pdf": "PDF",
        "pdf merge": "PDF merge" if LANG == "en" else "PDF объединение",
        "фото": "Photos" if LANG == "en" else "Фото",
        "photo": "Photos" if LANG == "en" else "Фото",
        "видео": "Video" if LANG == "en" else "Видео",
        "video": "Video" if LANG == "en" else "Видео",
        "скан": "Scan" if LANG == "en" else "Скан",
        "скан→pdf": "Scan→PDF" if LANG == "en" else "Скан→PDF",
        "scan→pdf": "Scan→PDF" if LANG == "en" else "Скан→PDF",
        "скан→файлы": "Scan→files" if LANG == "en" else "Скан→файлы",
        "scan→files": "Scan→files" if LANG == "en" else "Скан→файлы",
    }
    if key in mapping:
        return mapping[key]
    for k, v in mapping.items():
        if k in key:
            return v
    return mode


def set_lang(code: str):
    global LANG
    LANG = "en" if code == "en" else "ru"


# Профили фото: (ключ перевода, max_side, quality)
IMG_PRESETS = [
    ("imgp_max", 800, 55),
    ("imgp_msg", 1280, 65),
    ("imgp_bal", 1200, 75),
    ("imgp_good", 1600, 82),
    ("imgp_high", 2048, 88),
    ("imgp_keep", 0, 80),
]

# Профили видео: (ключ, кодек, CRF, preset, max_height, audio_kbps)
VID_PRESETS = [
    ("vidp_arch_max", "H.264", 32, "slow", 720, 64),
    ("vidp_strong", "H.264", 28, "medium", 720, 96),
    ("vidp_compact", "H.264", 26, "medium", 0, 96),
    ("vidp_work", "H.264", 24, "medium", 0, 128),
    ("vidp_quality", "H.264", 20, "slow", 0, 160),
    ("vidp_h265_arch", "H.265", 30, "medium", 720, 64),
    ("vidp_h265_eff", "H.265", 26, "medium", 0, 96),
    ("vidp_av1", "AV1", 34, "medium", 0, 64),
    ("vidp_vp9", "VP9", 34, "medium", 0, 96),
]


# -------------------- геометрия окна --------------------

def save_window_geo(key: str, win):
    """Запомнить положение и размер окна."""
    try:
        win.update_idletasks()
        geo = win.geometry()  # WxH+X+Y
        s = load_settings()
        geos = s.get("window_geo") or {}
        if not isinstance(geos, dict):
            geos = {}
        geos[key] = geo
        s["window_geo"] = geos
        save_settings(s)
    except Exception as e:
        _log_exc("save_window_geo", e)


def restore_window_geo(key: str, win, default_w: int, default_h: int):
    """Восстановить окно; если нет записи или уехало за экран — по центру."""
    try:
        s = load_settings()
        geos = s.get("window_geo") or {}
        geo = geos.get(key) if isinstance(geos, dict) else None
        if geo and isinstance(geo, str) and "x" in geo:
            m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo)
            if m:
                w, h, x, y = map(int, m.groups())
                sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
                w = max(300, min(w, sw))
                h = max(300, min(h, sh))
                if x + 80 > sw or y + 80 > sh or x < -50 or y < -50:
                    center_window(win, w, h)
                else:
                    win.geometry(f"{w}x{h}+{x}+{y}")
                return
        center_window(win, default_w, default_h)
    except Exception as e:
        _log_exc("restore_window_geo", e)
        try:
            center_window(win, default_w, default_h)
        except Exception:
            pass


def center_window(win, width: int = None, height: int = None):
    """Центрировать окно по рабочему столу."""
    try:
        win.update_idletasks()
        if width is None or height is None:
            geo = win.geometry().split("+")[0]
            w_s, h_s = geo.split("x")
            width = width or int(float(w_s))
            height = height or int(float(h_s))
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        max_w = int(sw * 0.92)
        max_h = int(sh * 0.88)
        width = min(width, max_w)
        height = min(height, max_h)
        x = max(0, (sw - width) // 2)
        y = max(0, (sh - height) // 2)
        if y + height > sh - 48:  # панель задач
            y = max(0, sh - height - 48)
        win.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
        win.update_idletasks()
    except Exception:
        pass

# -------------------- константы / пути --------------------

MAX_WORKERS = max(1, min(16, (os.cpu_count() or 4) - 1))
SETTINGS_FILE = APPDATA_DIR / "settings.json"
HISTORY_FILE = APPDATA_DIR / "history.json"

# Отдельная временная папка на каждый запуск: несколько копий программы
# не затирают файлы друг друга.
TEMP_BASE = Path(tempfile.gettempdir()) / "NexusOptimizer"
TEMP_DIR = TEMP_BASE / f"run_{os.getpid()}"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif",
              ".jfif", ".jp2"}
if HAS_HEIF:
    IMAGE_EXTS |= {".heic", ".heif", ".avif"}

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
              ".mpeg", ".mpg", ".ts", ".mts", ".m2ts", ".3gp"}

CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return si


stop_flag = threading.Event()
history_lock = threading.Lock()
_active_procs = []
_active_procs_lock = threading.Lock()


def _register_proc(proc):
    with _active_procs_lock:
        _active_procs.append(proc)


def _unregister_proc(proc):
    with _active_procs_lock:
        try:
            _active_procs.remove(proc)
        except ValueError:
            pass


def _terminate_proc(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
    except Exception:
        pass
    time.sleep(0.25)
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def kill_active_processes():
    """Принудительно остановить FFmpeg и др. дочерние процессы."""
    with _active_procs_lock:
        procs = list(_active_procs)
    for p in procs:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
    time.sleep(0.4)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    with _active_procs_lock:
        _active_procs.clear()


# -------------------- FFmpeg --------------------

@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """
    Поиск ffmpeg для dev и PyInstaller onefile/onedir.
    Стратегия: imageio-ffmpeg (вшитый бинарник) → _MEIPASS → рядом с exe → PATH.
    """
    candidates: list = []

    # 1. imageio-ffmpeg (предпочтительно в сборке)
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.isfile(p):
            return p
    except Exception as e:
        _log_exc("imageio_ffmpeg", e)

    # 2. PyInstaller unpack dir
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates += [
            Path(meipass) / "ffmpeg.exe",
            Path(meipass) / "ffmpeg",
            Path(meipass) / "imageio_ffmpeg" / "binaries",
        ]

    # 2b. Nuitka binary dir
    nd = getattr(sys, "__nuitka_binary_dir", None)
    if nd:
        candidates += [
            Path(nd) / "ffmpeg.exe",
            Path(nd) / "ffmpeg",
            Path(nd) / "imageio_ffmpeg" / "binaries",
        ]

    # 3. Рядом с исполняемым файлом
    try:
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [
            exe_dir / "ffmpeg.exe",
            exe_dir / "ffmpeg",
            exe_dir / "bin" / "ffmpeg.exe",
            exe_dir / "imageio_ffmpeg" / "binaries",
        ]
    except Exception as e:
        _log_exc("exe_dir", e)

    # 4. Известные пути Windows
    candidates += [
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path.home() / "ffmpeg" / "bin" / "ffmpeg.exe",
    ]

    for c in candidates:
        try:
            if c is None:
                continue
            if c.is_file() and c.name.lower().startswith("ffmpeg"):
                return str(c)
            if c.is_dir():
                for f in c.iterdir():
                    if f.is_file() and f.name.lower().startswith("ffmpeg"):
                        return str(f)
        except Exception as e:
            _log_exc(f"candidate {c}", e)

    # 5. PATH
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p and os.path.isfile(p):
            return p
    return None


@functools.lru_cache(maxsize=1)
def ffmpeg_encoders() -> frozenset:
    """Список кодировщиков в наличном ffmpeg (для автодетекта GPU)."""
    ff = find_ffmpeg()
    if not ff:
        return frozenset()
    try:
        r = subprocess.run([ff, "-hide_banner", "-encoders"],
                           capture_output=True, timeout=20,
                           creationflags=CNW, startupinfo=_startupinfo())
        txt = (r.stdout or b"").decode("utf-8", errors="ignore")
        names = set()
        for line in txt.splitlines():
            m = re.match(r"\s*[VAS][A-Z.]{5}\s+(\S+)", line)
            if m:
                names.add(m.group(1))
        return frozenset(names)
    except Exception as e:
        _log_exc("ffmpeg encoders", e)
        return frozenset()


# Приоритет для офисных ПК: Intel QSV → NVENC → AMD AMF → Media Foundation
HW_PRIORITY = {
    "H.264": ["h264_qsv", "h264_nvenc", "h264_amf", "h264_mf"],
    "H.265": ["hevc_qsv", "hevc_nvenc", "hevc_amf", "hevc_mf"],
    "AV1": ["av1_qsv", "av1_nvenc", "av1_amf"],
}


_HW_PROBE_LOCK = threading.Lock()
_HW_PROBE_TTL = 7 * 86400  # раз в неделю перепроверяем железо


def _ffmpeg_fingerprint(ff: str) -> str:
    """Стабильный отпечаток бинарника: в onefile путь меняется каждый
    запуск (_MEIxxxx), поэтому — имя + размер."""
    try:
        return f"{os.path.basename(ff)}:{os.path.getsize(ff)}"
    except Exception:
        return os.path.basename(str(ff))


@functools.lru_cache(maxsize=None)
def probe_hw_encoder(enc: str) -> bool:
    """
    Реальная проверка кодировщика: -encoders показывает вкомпилированные
    кодеки, а не наличие железа (qsv числится и без Intel). Кодируем
    3 кадра в null — если железа/драйвера нет, инициализация падает.
    Результат кэшируется на процесс и в настройках (TTL 7 дней) — первое
    видео сессии не ждёт повторных проб.
    """
    ff = find_ffmpeg()
    if not ff:
        return False
    fp = _ffmpeg_fingerprint(ff)
    with _HW_PROBE_LOCK:
        cache = load_settings().get("hw_probe") or {}
        if (cache.get("fp") == fp
                and time.time() - cache.get("ts", 0) < _HW_PROBE_TTL
                and enc in (cache.get("res") or {})):
            ok = bool(cache["res"][enc])
            log.info("hw probe %s: %s (cache)", enc, "OK" if ok else "unavailable")
            return ok
    try:
        r = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "color=black:s=128x128:d=0.2:r=15",
             "-frames:v", "3", "-pix_fmt", "nv12", "-c:v", enc,
             "-f", "null", "-"],
            capture_output=True, timeout=25,
            creationflags=CNW, startupinfo=_startupinfo())
        ok = r.returncode == 0
        log.info("hw probe %s: %s", enc, "OK" if ok else "unavailable")
    except Exception as e:
        _log_exc(f"hw probe {enc}", e)
        return False
    with _HW_PROBE_LOCK:
        s = load_settings()
        cache = s.get("hw_probe") or {}
        if cache.get("fp") != fp:
            cache = {"fp": fp, "res": {}}
        cache.setdefault("res", {})[enc] = ok
        cache["ts"] = time.time()
        s["hw_probe"] = cache
        save_settings(s)
    return ok


def working_hw_encoders(codec: str) -> List[str]:
    """Рабочие (проверенные пробой) аппаратные кодировщики для кодека."""
    encs = ffmpeg_encoders()
    return [e for e in HW_PRIORITY.get(codec, [])
            if e in encs and probe_hw_encoder(e)]


def hw_encoders_available() -> List[str]:
    """Для сообщения в UI: проверяем самый показательный ряд — H.264."""
    return working_hw_encoders("H.264")


def _resolve_video_out(out: str, codec: str, container: str) -> Tuple[str, str]:
    """Согласовать кодек и контейнер. Возвращает (путь_выхода, итоговый_кодек)."""
    codec = (codec or "H.264").upper().replace(" ", "")
    if codec in ("H264", "AVC"):
        codec = "H.264"
    if codec in ("H265", "HEVC"):
        codec = "H.265"
    cont = (container or "MP4").upper().lstrip(".")
    if cont not in ("MP4", "MKV", "WEBM", "AVI"):
        cont = "MP4"

    if cont == "AVI" and codec != "H.264":
        codec = "H.264"  # AVI живёт только с H.264 (см. подсказку в UI)
    if cont == "WEBM" and codec not in ("VP9", "AV1"):
        codec = "VP9"
    if cont == "MP4" and codec == "VP9":
        cont = "WEBM"

    ext = {"MP4": ".mp4", "MKV": ".mkv", "WEBM": ".webm", "AVI": ".avi"}[cont]
    out = os.path.splitext(out)[0] + ext
    return out, codec


def video_out_ext(codec: str, container: str) -> str:
    """Итоговое расширение с учётом согласования кодек/контейнер."""
    out, _ = _resolve_video_out("x.tmp", codec, container)
    return os.path.splitext(out)[1]


def _probe_duration(ff: str, inp: str) -> float:
    try:
        pi = subprocess.run([ff, "-i", inp], capture_output=True, timeout=30,
                            creationflags=CNW, startupinfo=_startupinfo())
        err = (pi.stderr or b"").decode("utf-8", errors="ignore")
        mm = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", err)
        if mm:
            return int(mm.group(1)) * 3600 + int(mm.group(2)) * 60 + float(mm.group(3))
    except Exception as e:
        _log_exc("probe duration", e)
    return 0.0


def _video_codec_args(codec: str, enc: Optional[str], crf: int, preset: str,
                      cont: str) -> List[str]:
    """
    Аргументы видеокодека.
    enc=None → программный кодек; enc="libaom" → запасной AV1;
    иначе — имя аппаратного кодировщика (h264_qsv и т.п.).
    """
    a: List[str] = []
    if enc and enc != "libaom":
        if enc.endswith("_qsv"):
            # Intel Quick Sync: ICQ-режим через global_quality
            a += ["-c:v", enc, "-global_quality", str(crf), "-preset", preset,
                  "-pix_fmt", "nv12"]
        elif enc.endswith("_nvenc"):
            a += ["-c:v", enc, "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
                  "-preset", "p5", "-pix_fmt", "yuv420p"]
        elif enc.endswith("_amf"):
            lvl = max(18, min(42, 52 - int(crf)))
            a += ["-c:v", enc, "-rc", "qvbr", "-qvbr_quality_level", str(lvl),
                  "-quality", "balanced", "-pix_fmt", "yuv420p"]
        elif enc.endswith("_mf"):
            q = max(25, min(95, 100 - 2 * int(crf)))
            a += ["-c:v", enc, "-rate_control", "quality", "-quality", str(q),
                  "-pix_fmt", "nv12"]
        else:
            a += ["-c:v", enc, "-pix_fmt", "yuv420p"]
        if codec == "H.265" and cont == ".mp4":
            a += ["-tag:v", "hvc1"]
        return a

    # Программные кодеки
    if enc == "libaom":
        a += ["-c:v", "libaom-av1", "-crf", str(crf), "-cpu-used", "6",
              "-pix_fmt", "yuv420p"]
    elif codec == "H.264":
        a += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
              "-pix_fmt", "yuv420p", "-profile:v", "high",
              "-x264-params", "ref=3:bframes=3:me=hex:subme=6:aq-mode=1"]
    elif codec == "H.265":
        a += ["-c:v", "libx265", "-crf", str(crf), "-preset", preset,
              "-pix_fmt", "yuv420p"]
        if cont == ".mp4":
            a += ["-tag:v", "hvc1"]
    elif codec == "VP9":
        a += ["-c:v", "libvpx-vp9", "-crf", str(crf), "-b:v", "0", "-row-mt", "1"]
    elif codec == "AV1":
        a += ["-c:v", "libsvtav1", "-crf", str(crf), "-preset", "6",
              "-pix_fmt", "yuv420p"]
    else:
        a += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
              "-pix_fmt", "yuv420p"]
    return a


def _run_ff(cmd: List[str], label: str, duration: float, progress_cb) -> Tuple[int, bytes]:
    """
    Запуск ffmpeg: прогресс через -progress pipe:1 (stdout, строки key=value),
    stderr — во временный файл (никаких дедлоков и проблем с \\r).
    """
    if stop_flag.is_set():
        return -9, b"stopped"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    errp = TEMP_DIR / f"fferr_{time.time_ns()}.log"
    proc = None
    tail = b""
    try:
        with open(errp, "w+b") as errf:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errf,
                stdin=subprocess.DEVNULL,
                startupinfo=_startupinfo(), creationflags=CNW)
            _register_proc(proc)
            while True:
                if stop_flag.is_set():
                    _terminate_proc(proc)
                    return -9, b"stopped"
                line = proc.stdout.readline() if proc.stdout else b""
                if not line:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                if progress_cb and duration > 0:
                    try:
                        s = line.decode("utf-8", errors="ignore").strip()
                        if s.startswith("out_time="):
                            hh, mm, ss = s.split("=", 1)[1].split(":")
                            cur = int(hh) * 3600 + int(mm) * 60 + float(ss)
                            frac = max(0.0, min(0.99, cur / duration))
                            progress_cb(frac, f"{label}{int(frac * 100)}%")
                    except Exception:
                        pass
            code = proc.wait()
            try:
                errf.flush()
                size = errf.tell()
                errf.seek(max(0, size - 4000))
                tail = errf.read()
            except Exception as e:
                _log_exc("ff stderr tail", e)
            return code or 0, tail
    except Exception as e:
        _log_exc("ff run", e)
        return -1, str(e).encode("utf-8", errors="ignore")
    finally:
        if proc is not None:
            _unregister_proc(proc)
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        try:
            errp.unlink(missing_ok=True)
        except Exception:
            pass


def process_video(inp: str, out: str, codec: str, crf: int, preset: str,
                  max_height: int, audio_kbps: int, container: str = "MP4",
                  use_hw: bool = True,
                  progress_cb=None) -> Tuple[str, bool, str, int, int]:
    """
    Сжатие видео. GPU (QSV/NVENC/AMF/MF) при наличии, откат на CPU при сбое.
    progress_cb(0..1, text).
    """
    orig = 0
    try:
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0
        orig = os.path.getsize(inp)
        if not ensure_disk_space(inp, int(orig * 1.3)):
            return inp, False, "err_no_space", orig, 0
        ff = find_ffmpeg()
        if not ff:
            return inp, False, "err_no_ffmpeg", orig, 0

        out, codec = _resolve_video_out(out, codec, container)
        crf = max(14, min(40, int(crf)))
        preset = preset or "medium"
        audio_kbps = max(0, min(320, int(audio_kbps)))
        cont = Path(out).suffix.lower()

        duration = _probe_duration(ff, inp)

        def build_cmd(enc: Optional[str]) -> List[str]:
            cmd = [ff, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
                   "-progress", "pipe:1", "-stats_period", "0.2",
                   "-i", inp, "-map_metadata", "-1"]
            if cont == ".mp4":
                cmd += ["-movflags", "+faststart"]
            cmd += _video_codec_args(codec, enc, crf, preset, cont)
            if max_height and int(max_height) > 0:
                cmd += ["-vf", f"scale=-2:{int(max_height)}"]
            if audio_kbps <= 0:
                cmd += ["-an"]
            elif cont == ".webm":
                cmd += ["-c:a", "libopus", "-b:a", f"{audio_kbps}k"]
            else:
                cmd += ["-c:a", "aac", "-b:a", f"{audio_kbps}k"]
            cmd.append(out)
            return cmd

        # Список попыток: проверенные аппаратные → программный (→ libaom).
        # Проба кэшируется, поэтому со второго файла список мгновенный;
        # даже прошедший пробу кодек может упасть на реальном видео —
        # тогда автоматически идёт следующая попытка.
        attempts: List[Optional[str]] = []
        if use_hw:
            attempts += working_hw_encoders(codec)
        attempts.append(None)
        if codec == "AV1":
            attempts.append("libaom")

        if progress_cb:
            progress_cb(0.01, "encode...")

        code, err_b = -1, b""
        for n, enc in enumerate(attempts):
            try:
                if os.path.isfile(out):
                    os.unlink(out)
            except Exception:
                pass
            label = f"{enc or 'cpu'} " if len(attempts) > 1 else ""
            code, err_b = _run_ff(build_cmd(enc), label, duration, progress_cb)
            if code == -9 or stop_flag.is_set():
                try:
                    if os.path.isfile(out):
                        os.unlink(out)
                except Exception:
                    pass
                return inp, False, "stopped", orig, 0
            if code == 0 and os.path.isfile(out) and os.path.getsize(out) > 1000:
                if enc and enc != "libaom":
                    log.info("video hw encoder used: %s", enc)
                break
            # неудача — лог и следующая попытка
            log.warning("encoder %s failed (code %s): %s", enc or "software",
                        code, (err_b or b"")[-300:].decode("utf-8", "ignore"))
            if progress_cb and n + 1 < len(attempts):
                progress_cb(0.01, t("hw_fallback") if enc else "retry...")
        else:
            pass

        if code != 0 or not os.path.isfile(out) or os.path.getsize(out) < 1000:
            err = (err_b or b"").decode("utf-8", errors="ignore")[-220:]
            return inp, False, f"FFmpeg: {err or 'error'}", orig, 0

        new = os.path.getsize(out)
        if new >= orig:
            # получилось больше исходника — не считаем успехом
            try:
                os.unlink(out)
            except Exception:
                pass
            return inp, False, "err_no_gain", orig, orig
        if progress_cb:
            progress_cb(1.0, "100%")
        return inp, True, "OK", orig, new
    except Exception as e:
        _log_exc("process_video", e)
        return inp, False, str(e)[:140], orig, 0


# -------------------- utils --------------------

def load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        _log_exc(f"load_json {path.name}", e)
    return default


def save_json(path: Path, data):
    """Атомарная запись: tmp-файл + os.replace (краш не портит файл)."""
    tmpf = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmpf = path.with_name(path.name + f".tmp{os.getpid()}")
        with open(tmpf, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmpf, path)
    except Exception as e:
        _log_exc("save_json", e)
        try:
            if tmpf and tmpf.exists():
                tmpf.unlink()
        except Exception:
            pass


def load_settings() -> dict:
    defaults = {
        "theme": "System", "lang": "ru", "last_dir": "", "window_geo": {},
        "sound": True, "last_section": "pdf",
        # PDF
        "pdf_preset_key": "smart",
        "pdf_dpi": 130, "pdf_quality": 55, "pdf_gray": False,
        "pdf_multi": True, "pdf_move": True,
        "pdf_outdir": "", "pdf_recurse": False, "pdf_workers": "auto",
        # Фото
        "img_preset_key": "imgp_bal",
        "img_quality": 78, "img_max_side": 0, "img_format": "JPEG",
        "img_multi": True, "img_move": True, "img_smart": True,
        "img_rename": "{name}_nexus",
        "img_outdir": "", "img_recurse": False, "img_workers": "auto",
        # Видео
        "vid_profile_key": "vidp_work",
        "vid_codec": "H.264", "vid_container": "MP4",
        "vid_crf": 24, "vid_preset": "medium", "vid_height": 0,
        "vid_audio": 128, "vid_multi": True, "vid_move": True,
        "vid_rename": "{name}_nexus", "vid_hw": True,
        "vid_outdir": "", "vid_recurse": False,
        # Сканер
        "scan_compress": True, "scan_pdf_preset": "",
        "scan_comp_dpi": 130, "scan_comp_q": 55, "scan_gray": False,
        "scan_folder": "", "scan_open_folder": True,
        "rename_template": "{name}_nexus",
    }
    data = load_json(SETTINGS_FILE, {})
    if isinstance(data, dict):
        defaults.update(data)
    try:
        defaults["pdf_dpi"] = safe_int(defaults.get("pdf_dpi"), 130, 50, 300)
        defaults["pdf_quality"] = safe_int(defaults.get("pdf_quality"), 55, 20, 95)
        defaults["img_quality"] = safe_int(defaults.get("img_quality"), 78, 20, 100)
        defaults["img_max_side"] = safe_int(defaults.get("img_max_side"), 0, 0, 20000)
        defaults["vid_crf"] = safe_int(defaults.get("vid_crf"), 24, 14, 40)
        defaults["vid_height"] = safe_int(defaults.get("vid_height"), 0, 0, 8700)
        defaults["vid_audio"] = safe_int(defaults.get("vid_audio"), 128, 0, 320)
        defaults["scan_comp_dpi"] = safe_int(defaults.get("scan_comp_dpi"), 130, 50, 300)
        defaults["scan_comp_q"] = safe_int(defaults.get("scan_comp_q"), 55, 20, 95)
        defaults["lang"] = "en" if defaults.get("lang") == "en" else "ru"
        if defaults.get("theme") not in ("Dark", "Light", "System"):
            defaults["theme"] = "System"
        for k in ("pdf_workers", "img_workers"):
            v = str(defaults.get(k, "auto"))
            defaults[k] = v if v == "auto" or v.isdigit() else "auto"
        for k in ("pdf_sort", "img_sort", "vid_sort"):
            if defaults.get(k) not in ("added", "name", "size"):
                defaults[k] = "added"
    except Exception as e:
        _log_exc("load_settings sanitize", e)
    return defaults


def save_settings(data: dict):
    save_json(SETTINGS_FILE, data)


def add_history(entry: dict):
    with history_lock:
        hist = load_json(HISTORY_FILE, [])
        if not isinstance(hist, list):
            hist = []
        if "names" not in entry:
            entry["names"] = []
        hist.insert(0, entry)
        save_json(HISTORY_FILE, hist[:80])


def cleanup_temp():
    """Удалить свою сессионную временную папку."""
    try:
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR, ignore_errors=True)
    except Exception:
        pass


def cleanup_stale_temp():
    """Убрать чужой старый мусор (>24 ч), не трогая живые сессии."""
    try:
        if not TEMP_BASE.exists():
            return
        cutoff = time.time() - 24 * 3600
        for item in TEMP_BASE.iterdir():
            try:
                if item == TEMP_DIR:
                    continue
                if item.stat().st_mtime < cutoff:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def cleanup_batch_temp():
    """Удалить рабочие файлы пакетной обработки, не трогая страницы сканера."""
    try:
        if not TEMP_DIR.exists():
            return
        for f in TEMP_DIR.iterdir():
            try:
                if not f.is_file():
                    continue
                n = f.name
                if n.startswith(("chunk_", "merged_", "fferr_", "doc_raw_", "p_")) or \
                        (n.startswith("tmp") and n.endswith(".pdf")):
                    f.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


atexit.register(cleanup_temp)


def disk_free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or path).free
    except Exception:
        return 10 ** 15


def ensure_disk_space(path: str, need_bytes: int) -> bool:
    """need_bytes — примерный запас (обычно 1.2 * размер исходника)."""
    free = disk_free_bytes(path)
    return free > need_bytes + 50 * 1024 * 1024


def safe_filename(name: str) -> str:
    """Убирает запрещённые символы Windows."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .")
    return name or "file"


def safe_int(val, default: int = 0, min_v: int = None, max_v: int = None) -> int:
    try:
        if isinstance(val, str):
            val = val.strip().replace(",", ".")
            if not val:
                n = default
            else:
                n = int(float(val))
        else:
            n = int(val)
    except Exception:
        n = default
    if min_v is not None:
        n = max(min_v, n)
    if max_v is not None:
        n = min(max_v, n)
    return n


def path_too_long(path: str) -> bool:
    try:
        return len(os.path.abspath(path)) > 240
    except Exception:
        return False


def filter_existing_files(paths: list) -> tuple:
    """Убрать несуществующие/пустые. Возвращает (ok_list, skipped_msgs)."""
    ok, skipped = [], []
    for p in paths:
        try:
            p = os.path.normpath(str(p))
            if not os.path.isfile(p):
                skipped.append(f"{os.path.basename(p)}: {t('err_not_found')}")
                continue
            if os.path.getsize(p) <= 0:
                skipped.append(f"{os.path.basename(p)}: {t('err_empty')}")
                continue
            if path_too_long(p):
                skipped.append(f"{os.path.basename(p)}: {t('err_too_long')}")
                continue
            ok.append(p)
        except PermissionError as e:
            skipped.append(f"{os.path.basename(p)}: {t('err_no_access')}")
            _log_exc("filter_existing", e)
        except Exception as e:
            skipped.append(f"{os.path.basename(p)}: {e}")
            _log_exc("filter_existing", e)
    return ok, skipped


def ensure_writable_dir(dirpath: str) -> tuple:
    """Проверка что в папку можно писать. (ok, error_msg)."""
    try:
        dirpath = os.path.normpath(dirpath)
        if not dirpath:
            return False, t("err_dir")
        Path(dirpath).mkdir(parents=True, exist_ok=True)
        test = Path(dirpath) / f".nexus_write_test_{os.getpid()}.tmp"
        test.write_text("ok", encoding="utf-8")
        test.unlink(missing_ok=True)
        return True, ""
    except PermissionError as e:
        _log_exc("ensure_writable", e)
        return False, t("err_no_write")
    except OSError as e:
        _log_exc("ensure_writable", e)
        return False, f"{t('err_dir')}: {e}"
    except Exception as e:
        _log_exc("ensure_writable", e)
        return False, str(e)[:120]


def unique_out_path(path: str) -> str:
    """Если файл существует — добавить _2, _3..."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for i in range(2, 1000):
        cand = f"{base}_{i}{ext}"
        if not os.path.exists(cand):
            return cand
    return f"{base}_{int(time.time())}{ext}"


def apply_rename(template: str, original_path: str) -> str:
    """Пустой шаблон = оставить исходное имя файла."""
    p = Path(original_path)
    name = p.stem
    template = (template or "").strip()
    if not template:
        return safe_filename(name)
    now = datetime.now()
    result = (template
              .replace("{name}", name)
              .replace("{date}", now.strftime("%Y%m%d"))
              .replace("{time}", now.strftime("%H%M%S")))
    result = safe_filename(result)
    if not result:
        result = name
    return result


def protect_original(input_path: str) -> bool:
    """Если не перемещаем в original/ — переименовать с префиксом _original_."""
    try:
        if not os.path.isfile(input_path):
            return False
        d = os.path.dirname(input_path)
        name = os.path.basename(input_path)
        if name.startswith("_original_"):
            return True
        new_name = "_original_" + name
        dest = os.path.join(d, new_name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(os.path.join(d, f"_original_{stem}_{i}{ext}")):
                i += 1
            dest = os.path.join(d, f"_original_{stem}_{i}{ext}")
        os.rename(input_path, dest)
        return True
    except Exception as e:
        _log_exc("protect_original", e)
        return False


def move_to_original(path: str) -> bool:
    try:
        d = os.path.dirname(path)
        name = os.path.basename(path)
        od = os.path.join(d, "original")
        os.makedirs(od, exist_ok=True)
        dest = os.path.join(od, name)
        if os.path.exists(dest):
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(os.path.join(od, f"{stem}_{i}{ext}")):
                i += 1
            dest = os.path.join(od, f"{stem}_{i}{ext}")
        shutil.move(path, dest)
        return True
    except Exception as e:
        _log_exc("move_to_original", e)
        return False


def finalize_output(inp: str, out: str, move: bool) -> bool:
    """
    Сохранить оригинал и убрать временный суффикс .__nexus_tmp__.

    ВАЖНО: если оригинал сохранить не удалось, финальное имя (= оригинал)
    НЕ трогаем — сжатый файл остаётся рядом с суффиксом. Потеря данных
    исключена по построению.
    """
    ok = False
    try:
        ok = move_to_original(inp) if move else protect_original(inp)
    except Exception as e:
        _log_exc("finalize_output move", e)
        ok = False
    try:
        if out and ".__nexus_tmp__" in out and os.path.isfile(out):
            final = out.replace(".__nexus_tmp__", "")
            if os.path.normcase(final) == os.path.normcase(out):
                return ok
            if os.path.exists(final):
                if not ok:
                    log.error("original was not preserved; keeping tmp result: %s", out)
                    return False
                try:
                    os.unlink(final)
                except Exception as e:
                    _log_exc("finalize unlink stale", e)
                    return False
            os.rename(out, final)
    except Exception as e:
        _log_exc("finalize rename", e)
    return ok


def merge_pdfs(paths: List[str], output_pdf: str,
               progress_cb=None) -> Tuple[bool, str, int, int]:
    """Объединить несколько PDF. Возвращает (ok, msg, added, skipped) —
    битые/зашифрованные файлы пропускаются, но не молча."""
    if not fitz:
        return False, "err_pymupdf", 0, len(paths)
    try:
        doc = fitz.open()
        added = 0
        total = max(1, len(paths))
        for i, p in enumerate(paths, 1):
            try:
                src = fitz.open(p)
                doc.insert_pdf(src)
                src.close()
                added += 1
            except Exception as e:
                _log_exc(f"merge {os.path.basename(p)}", e)
                continue
            finally:
                if progress_cb:
                    try:
                        progress_cb(i, total)
                    except Exception:
                        pass
        if added == 0:
            doc.close()
            return False, "err_add_pdf", 0, len(paths)
        doc.save(output_pdf, garbage=4, deflate=True)
        doc.close()
        return True, "OK", added, len(paths) - added
    except Exception as e:
        return False, str(e)[:120], 0, len(paths)


def get_executor(max_workers):
    """
    Параллелизм:
    - из исходников Python → процессы (реальные ядра);
    - в .exe → потоки (процессы в onefile снова поднимают GUI).
    """
    if is_frozen():
        return ThreadPoolExecutor(max_workers=max_workers)
    return ProcessPoolExecutor(max_workers=max_workers)


def play_sound():
    if HAS_SOUND:
        try:
            winsound.MessageBeep(winsound.MB_OK)
        except Exception:
            pass


def effective_workers(setting: str, cap: int, total: int) -> int:
    """'auto' → MAX_WORKERS; число → само число. Ограничение cap и total."""
    base = MAX_WORKERS if str(setting) == "auto" else safe_int(setting, MAX_WORKERS, 1, 32)
    return max(1, min(base, cap, max(1, total)))


def collect_files(path, exts: set, recurse: bool) -> List[str]:
    """Файл или папка → список файлов с нужными расширениями."""
    res: List[str] = []
    try:
        path = os.path.normpath(str(path).strip().strip("{}").strip('"'))
        if os.path.isfile(path):
            if Path(path).suffix.lower() in exts:
                res.append(path)
        elif os.path.isdir(path):
            if recurse:
                for root, dirs, files in os.walk(path):
                    # не подбирать уже сохранённые оригиналы
                    dirs[:] = [d for d in dirs if d.lower() != "original"]
                    for n in files:
                        if n.startswith("_original_"):
                            continue
                        if Path(n).suffix.lower() in exts:
                            res.append(os.path.join(root, n))
            else:
                for n in os.listdir(path):
                    f = os.path.join(path, n)
                    if os.path.isfile(f) and not n.startswith("_original_") \
                            and Path(n).suffix.lower() in exts:
                        res.append(f)
    except Exception as e:
        _log_exc("collect_files", e)
    return res


def _brace_parse(data: str) -> List[str]:
    """Запасной разбор строк DnD вида {path one} {path two}."""
    paths, cur, ins = [], "", False
    for ch in data:
        if ch == "{":
            ins, cur = True, ""
        elif ch == "}":
            ins = False
            if cur:
                paths.append(cur)
        elif ins:
            cur += ch
    if not paths and data.strip():
        paths = data.split()
    return paths

# -------------------- SCANNER (WIA + TWAIN) --------------------
# Maximum compatibility: the driver's own dialog plus a fallback chain.
#
# TWAIN goes through a first-party ctypes binding rather than the pytwain
# package. The reason is legal: pytwain is distributed strictly under GPL-2.0
# with no "or later", while this program is built with PyMuPDF under AGPL-3.0
# and ships FFmpeg under GPL-3.0. GPL-2.0-only is incompatible with both, so
# such a build could not be distributed at all. TWAIN itself is an open
# specification, and an own implementation inherits nobody's terms.

# ---------- TWAIN: first-party binding (ctypes) ----------
# Implements exactly what the application needs: list the sources, open the
# chosen one, set the resolution, show the driver window and take the image by
# native transfer (a DIB in memory). TWAIN structures on Windows are packed to
# two bytes — without _pack_ = 2 the fields shift and the driver reads rubbish.

_TWRC_SUCCESS, _TWRC_FAILURE, _TWRC_CANCEL = 0, 1, 2
_TWRC_NOTDSEVENT, _TWRC_XFERDONE, _TWRC_ENDOFLIST = 5, 6, 7

_DG_CONTROL, _DG_IMAGE = 0x0001, 0x0002
_DAT_CAPABILITY, _DAT_EVENT, _DAT_IDENTITY, _DAT_PARENT = 0x0001, 0x0002, 0x0003, 0x0004
_DAT_PENDINGXFERS, _DAT_USERINTERFACE = 0x0005, 0x0009
_DAT_IMAGENATIVEXFER = 0x0104

_MSG_GET, _MSG_SET, _MSG_RESET = 0x0001, 0x0006, 0x0007
_MSG_GETFIRST, _MSG_GETNEXT = 0x0004, 0x0005
_MSG_OPENDSM, _MSG_CLOSEDSM = 0x0301, 0x0302
_MSG_OPENDS, _MSG_CLOSEDS = 0x0401, 0x0402
_MSG_ENABLEDS, _MSG_DISABLEDS = 0x0501, 0x0502
_MSG_PROCESSEVENT = 0x0601
_MSG_ENDXFER = 0x0701
_MSG_XFERREADY, _MSG_CLOSEDSREQ, _MSG_CLOSEDSOK = 0x0101, 0x0102, 0x0103

_TWON_ONEVALUE = 0x0005
_TWTY_UINT16, _TWTY_FIX32 = 0x0005, 0x0007
_ICAP_UNITS, _ICAP_XRESOLUTION, _ICAP_YRESOLUTION = 0x0102, 0x1118, 0x1119
_TWUN_INCHES = 0
_DF_APP2 = 0x00000001  # SupportedGroups: приложение


class _TwFix32(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("Whole", ctypes.c_int16), ("Frac", ctypes.c_uint16)]


class _TwVersion(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("MajorNum", ctypes.c_uint16), ("MinorNum", ctypes.c_uint16),
                ("Language", ctypes.c_uint16), ("Country", ctypes.c_uint16),
                ("Info", ctypes.c_char * 34)]


class _TwIdentity(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("Id", ctypes.c_uint32), ("Version", _TwVersion),
                ("ProtocolMajor", ctypes.c_uint16),
                ("ProtocolMinor", ctypes.c_uint16),
                ("SupportedGroups", ctypes.c_uint32),
                ("Manufacturer", ctypes.c_char * 34),
                ("ProductFamily", ctypes.c_char * 34),
                ("ProductName", ctypes.c_char * 34)]


class _TwUserInterface(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("ShowUI", ctypes.c_uint16), ("ModalUI", ctypes.c_uint16),
                ("hParent", ctypes.c_void_p)]


class _TwEvent(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("pEvent", ctypes.c_void_p), ("TWMessage", ctypes.c_uint16)]


class _TwCapability(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("Cap", ctypes.c_uint16), ("ConType", ctypes.c_uint16),
                ("hContainer", ctypes.c_void_p)]


class _TwOneValue(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("ItemType", ctypes.c_uint32), ("Item", ctypes.c_uint32)]


class _TwPendingXfers(ctypes.Structure):
    _pack_ = 2
    _fields_ = [("Count", ctypes.c_uint16), ("Reserved", ctypes.c_uint32)]


class _WinMsg(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint32), ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long)]


_TWAIN_DSM = None
_TWAIN_DSM_TRIED = False


def _twain_dsm():
    """The TWAIN Data Source Manager.

    Looked for next to the program first, so a build can carry its own copy and
    not depend on whether a scanner driver installed one; then through the
    normal Windows search. The architecture must match the process: a 64-bit
    application cannot load a 32-bit twaindsm.dll, which is the usual reason
    for "TWAIN sees no scanners".
    """
    global _TWAIN_DSM, _TWAIN_DSM_TRIED
    if _TWAIN_DSM_TRIED:
        return _TWAIN_DSM
    _TWAIN_DSM_TRIED = True
    here = Path(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(
        sys.executable if is_frozen() else __file__))))
    for cand in (str(here / "twaindsm.dll"), "twaindsm.dll"):
        try:
            _TWAIN_DSM = ctypes.WinDLL(cand)
            log.info("TWAIN DSM: %s", cand)
            return _TWAIN_DSM
        except OSError:
            continue
    log.info("TWAIN DSM не найден (нужен twaindsm.dll той же разрядности)")
    return None


def _twain_app_identity() -> _TwIdentity:
    ident = _TwIdentity()
    ident.Id = 0
    ident.Version.MajorNum = 3
    ident.Version.MinorNum = 7
    ident.Version.Language = 0  # TWLG_USA
    ident.Version.Country = 1   # TWCY_USA
    ident.Version.Info = APP_VERSION.encode("ascii", "replace")[:33]
    # Declare TWAIN 1.9: containers can then be prepared with plain
    # GlobalAlloc, without exchanging the DAT_ENTRYPOINT function table.
    ident.ProtocolMajor = 1
    ident.ProtocolMinor = 9
    ident.SupportedGroups = _DG_CONTROL | _DG_IMAGE | _DF_APP2
    ident.Manufacturer = APP_AUTHOR.encode("ascii", "replace")[:33]
    ident.ProductFamily = b"Nexus"
    ident.ProductName = b"Nexus Optimizer"
    return ident


def _twain_call(dsm, origin, dest, dg, dat, msg, data):
    return dsm.DSM_Entry(ctypes.byref(origin),
                         ctypes.byref(dest) if dest is not None else None,
                         ctypes.c_uint32(dg), ctypes.c_uint16(dat),
                         ctypes.c_uint16(msg), data)


def twain_source_names() -> list:
    """Names of the installed TWAIN sources. An empty list is not an error."""
    dsm = _twain_dsm()
    if dsm is None:
        return []
    app = _twain_app_identity()
    names = []
    if _twain_call(dsm, app, None, _DG_CONTROL, _DAT_PARENT, _MSG_OPENDSM,
                   ctypes.byref(ctypes.c_void_p(None))) != _TWRC_SUCCESS:
        log.warning("TWAIN: DSM не открылся")
        return []
    try:
        src = _TwIdentity()
        rc = _twain_call(dsm, app, None, _DG_CONTROL, _DAT_IDENTITY,
                         _MSG_GETFIRST, ctypes.byref(src))
        while rc == _TWRC_SUCCESS:
            names.append(src.ProductName.decode("mbcs", "replace").strip())
            src = _TwIdentity()
            rc = _twain_call(dsm, app, None, _DG_CONTROL, _DAT_IDENTITY,
                             _MSG_GETNEXT, ctypes.byref(src))
        if rc not in (_TWRC_ENDOFLIST, _TWRC_SUCCESS):
            log.info("TWAIN: перечисление источников вернуло %s", rc)
    finally:
        _twain_call(dsm, app, None, _DG_CONTROL, _DAT_PARENT, _MSG_CLOSEDSM,
                    ctypes.byref(ctypes.c_void_p(None)))
    return names


def _twain_set_fix32(dsm, app, src, cap_id: int, value: float) -> bool:
    """One numeric capability of the source (X or Y resolution)."""
    k32 = ctypes.windll.kernel32
    whole = int(value)
    frac = int(round((value - whole) * 65536.0)) & 0xFFFF
    one = _TwOneValue(_TWTY_FIX32, (frac << 16) | (whole & 0xFFFF))
    h = k32.GlobalAlloc(0x0042, ctypes.sizeof(one))  # GHND
    if not h:
        return False
    try:
        p = k32.GlobalLock(ctypes.c_void_p(h))
        ctypes.memmove(p, ctypes.byref(one), ctypes.sizeof(one))
        k32.GlobalUnlock(ctypes.c_void_p(h))
        cap = _TwCapability(cap_id, _TWON_ONEVALUE, h)
        rc = _twain_call(dsm, app, src, _DG_CONTROL, _DAT_CAPABILITY,
                         _MSG_SET, ctypes.byref(cap))
        return rc == _TWRC_SUCCESS
    finally:
        k32.GlobalFree(ctypes.c_void_p(h))


def _twain_set_uint16(dsm, app, src, cap_id: int, value: int) -> bool:
    k32 = ctypes.windll.kernel32
    one = _TwOneValue(_TWTY_UINT16, value & 0xFFFF)
    h = k32.GlobalAlloc(0x0042, ctypes.sizeof(one))
    if not h:
        return False
    try:
        p = k32.GlobalLock(ctypes.c_void_p(h))
        ctypes.memmove(p, ctypes.byref(one), ctypes.sizeof(one))
        k32.GlobalUnlock(ctypes.c_void_p(h))
        cap = _TwCapability(cap_id, _TWON_ONEVALUE, h)
        return _twain_call(dsm, app, src, _DG_CONTROL, _DAT_CAPABILITY,
                           _MSG_SET, ctypes.byref(cap)) == _TWRC_SUCCESS
    finally:
        k32.GlobalFree(ctypes.c_void_p(h))


def _dib_handle_to_bmp(handle, bmp_path: str) -> bool:
    """An in-memory DIB to a BMP file.

    The driver hands over only the DIB body, without a file header: we add one
    ourselves, otherwise no viewer opens the image.
    """
    k32 = ctypes.windll.kernel32
    size = int(k32.GlobalSize(ctypes.c_void_p(handle)))
    if size <= 40:
        return False
    p = k32.GlobalLock(ctypes.c_void_p(handle))
    if not p:
        return False
    try:
        raw = ctypes.string_at(p, size)
    finally:
        k32.GlobalUnlock(ctypes.c_void_p(handle))

    header_size = int.from_bytes(raw[0:4], "little")
    bit_count = int.from_bytes(raw[14:16], "little")
    compression = int.from_bytes(raw[16:20], "little")
    clr_used = int.from_bytes(raw[32:36], "little")
    palette = clr_used * 4 if clr_used else ((1 << bit_count) * 4 if bit_count <= 8 else 0)
    if compression == 3 and header_size == 40:  # BI_BITFIELDS: три маски
        palette += 12
    offset = 14 + header_size + palette

    with open(bmp_path, "wb") as fh:
        fh.write(b"BM")
        fh.write((14 + size).to_bytes(4, "little"))
        fh.write(b"\0\0\0\0")
        fh.write(offset.to_bytes(4, "little"))
        fh.write(raw)
    return os.path.getsize(bmp_path) > 60


def _twain_pump(dsm, app, src, on_ready, deadline: float) -> str:
    """Message loop for as long as the driver window is up.

    TWAIN 1.x requires the application to hand every window message to the
    source: that is how the driver learns about clicks in its own window and
    reports that an image is ready.
    """
    u32 = ctypes.windll.user32
    msg = _WinMsg()
    while time.time() < deadline:
        if not u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            time.sleep(0.01)
            continue
        ev = _TwEvent(ctypes.cast(ctypes.byref(msg), ctypes.c_void_p), 0)
        rc = _twain_call(dsm, app, src, _DG_CONTROL, _DAT_EVENT,
                         _MSG_PROCESSEVENT, ctypes.byref(ev))
        if rc == _TWRC_NOTDSEVENT:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        if ev.TWMessage == _MSG_XFERREADY:
            return on_ready()
        if ev.TWMessage in (_MSG_CLOSEDSREQ, _MSG_CLOSEDSOK):
            return "cancel"
    return "timeout"


def twain_scan_to_file(source_name: str, out_path: str,
                       dpi: int = 200) -> Tuple[bool, str]:
    """Scans with the chosen TWAIN source through its own driver window."""
    dsm = _twain_dsm()
    if dsm is None:
        return False, "TWAIN: twaindsm.dll not found"

    u32 = ctypes.windll.user32
    app = _twain_app_identity()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    if _twain_call(dsm, app, None, _DG_CONTROL, _DAT_PARENT, _MSG_OPENDSM,
                   ctypes.byref(ctypes.c_void_p(None))) != _TWRC_SUCCESS:
        return False, "TWAIN: DSM open failed"

    # A hidden window of our own: scanning runs on a worker thread, and the
    # driver window must belong to the same thread as the message loop.
    hwnd = u32.CreateWindowExW(0, "STATIC", "Nexus TWAIN", 0, 0, 0, 0, 0,
                               None, None, None, None)
    src = None
    enabled = False
    result = (False, "TWAIN: no data")
    try:
        src = _TwIdentity()
        src.ProductName = source_name.encode("mbcs", "replace")[:33]
        if _twain_call(dsm, app, None, _DG_CONTROL, _DAT_IDENTITY,
                       _MSG_OPENDS, ctypes.byref(src)) != _TWRC_SUCCESS:
            return False, "TWAIN: source open failed"

        _twain_set_uint16(dsm, app, src, _ICAP_UNITS, _TWUN_INCHES)
        _twain_set_fix32(dsm, app, src, _ICAP_XRESOLUTION, float(dpi))
        _twain_set_fix32(dsm, app, src, _ICAP_YRESOLUTION, float(dpi))

        ui = _TwUserInterface(1, 0, ctypes.c_void_p(hwnd))
        if _twain_call(dsm, app, src, _DG_CONTROL, _DAT_USERINTERFACE,
                       _MSG_ENABLEDS, ctypes.byref(ui)) != _TWRC_SUCCESS:
            return False, "TWAIN: cannot show driver window"
        enabled = True

        def take_image():
            handle = ctypes.c_void_p(None)
            rc = _twain_call(dsm, app, src, _DG_IMAGE, _DAT_IMAGENATIVEXFER,
                             _MSG_GET, ctypes.byref(handle))
            if rc != _TWRC_XFERDONE or not handle.value:
                return "failed"
            bmp = os.path.splitext(out_path)[0] + ".bmp"
            ok = False
            try:
                ok = _dib_handle_to_bmp(handle.value, bmp)
            finally:
                ctypes.windll.kernel32.GlobalFree(handle)
            # End the transfer and reset the queue: without this the source
            # stays busy and the next scan never starts.
            pend = _TwPendingXfers()
            _twain_call(dsm, app, src, _DG_CONTROL, _DAT_PENDINGXFERS,
                        _MSG_ENDXFER, ctypes.byref(pend))
            if pend.Count != 0:
                _twain_call(dsm, app, src, _DG_CONTROL, _DAT_PENDINGXFERS,
                            _MSG_RESET, ctypes.byref(_TwPendingXfers()))
            return "ok" if ok else "failed"

        state = _twain_pump(dsm, app, src, take_image, time.time() + 600)
        if state == "ok":
            bmp = os.path.splitext(out_path)[0] + ".bmp"
            png = os.path.splitext(out_path)[0] + ".png"
            try:
                with Image.open(bmp) as im:
                    im.convert("RGB").save(png)
                try:
                    os.unlink(bmp)
                except Exception:
                    pass
                result = (True, png)
            except Exception as e:
                _log_exc("twain bmp→png", e)
                result = (True, bmp)
        elif state == "cancel":
            result = (False, "TWAIN: cancelled")
        elif state == "timeout":
            result = (False, "TWAIN: timeout")
        else:
            result = (False, "TWAIN: transfer failed")
        return result
    except Exception as e:
        _log_exc("twain scan", e)
        return False, f"TWAIN: {str(e)[:140]}"
    finally:
        try:
            if enabled:
                ui = _TwUserInterface(0, 0, ctypes.c_void_p(hwnd))
                _twain_call(dsm, app, src, _DG_CONTROL, _DAT_USERINTERFACE,
                            _MSG_DISABLEDS, ctypes.byref(ui))
            if src is not None and src.Id:
                _twain_call(dsm, app, None, _DG_CONTROL, _DAT_IDENTITY,
                            _MSG_CLOSEDS, ctypes.byref(src))
        except Exception as e:
            _log_exc("twain close ds", e)
        try:
            _twain_call(dsm, app, None, _DG_CONTROL, _DAT_PARENT,
                        _MSG_CLOSEDSM, ctypes.byref(ctypes.c_void_p(None)))
        except Exception as e:
            _log_exc("twain close dsm", e)
        if hwnd:
            u32.DestroyWindow(ctypes.c_void_p(hwnd))


WIA_FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_PNG_REAL = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_JPEG_REAL = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
WIA_FORMAT_TIFF = "{B96B3CB1-0728-11D3-9D7B-0000F81EF32E}"


def _com_prepare():
    """
    comtypes в onefile-сборке пытается генерировать обёртки в _MEIPASS
    (read-only) — отключаем дисковый кэш, генерация идёт в память.
    """
    try:
        import comtypes.client
        comtypes.client.gen_dir = None
        return comtypes.client
    except Exception as e:
        _log_exc("comtypes prepare", e)
        return None


def list_wia_scanners():
    """Список устройств WIA (сканеры + МФУ)."""
    devices = []
    cc = _com_prepare()
    if cc is None:
        return devices
    try:
        dm = cc.CreateObject("WIA.DeviceManager")
        for i in range(1, dm.DeviceInfos.Count + 1):
            info = dm.DeviceInfos(i)
            try:
                dtype = int(info.Type)
            except Exception:
                dtype = 1
            if dtype == 3:  # чисто видео-устройства пропускаем
                continue
            try:
                name = str(info.Properties("Name").Value)
            except Exception:
                try:
                    name = str(info.Properties(1).Value)
                except Exception:
                    name = f"WIA device {i}"
            tag = "WIA/Camera" if dtype == 2 else "WIA"
            devices.append((f"wia:{i}", f"{name}  [{tag}]", "wia", i))
    except Exception as e:
        _log_exc("WIA list", e)
    return devices


def list_twain_scanners():
    devices = []
    try:
        for i, name in enumerate(twain_source_names()):
            devices.append((f"twain:{i}:{name}", f"{name}  [TWAIN]", "twain", name))
    except Exception as e:
        _log_exc("TWAIN list", e)
    return devices


def list_all_scanners():
    items = list_wia_scanners() + list_twain_scanners()
    seen = set()
    uniq = []
    for it in items:
        k = it[1].lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    uniq.append(("dialog", t("win_dialog"), "dialog", None))
    return uniq


def _wia_save_image(image, out_path: str) -> bool:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    candidates = [out_path]
    base = os.path.splitext(out_path)[0]
    for ext in (".png", ".jpg", ".bmp", ".tif"):
        candidates.append(base + ext)
    for p in candidates:
        try:
            image.SaveFile(str(p))
            if os.path.isfile(p) and os.path.getsize(p) > 50:
                if p != out_path:
                    # нормализуем в PNG через Pillow если нужно
                    try:
                        target = base + ".png"
                        with Image.open(p) as im0:
                            im = ImageOps.exif_transpose(im0)
                            if im.mode in ("RGBA", "P"):
                                im.save(target)
                            else:
                                im.convert("RGB").save(target)
                        if p != target and os.path.isfile(p):
                            try:
                                os.unlink(p)
                            except Exception:
                                pass
                        return True
                    except Exception as e:
                        _log_exc("wia normalize", e)
                        if os.path.getsize(p) > 50:
                            return True
                return True
        except Exception as e:
            _log_exc("wia SaveFile", e)
    return False


def wia_scan_with_dialog(out_path: str) -> Tuple[bool, str]:
    """
    Самый совместимый путь: UI драйвера Windows.
    Перебираем форматы PNG → JPEG → BMP → TIFF → default.
    """
    cc = _com_prepare()
    if cc is None:
        return False, "WIA: comtypes error"
    try:
        dialog = cc.CreateObject("WIA.CommonDialog")
        formats = [
            WIA_FORMAT_PNG_REAL,
            WIA_FORMAT_JPEG_REAL,
            WIA_FORMAT_BMP,
            WIA_FORMAT_TIFF,
            None,
        ]
        last_err = "Cancelled"
        for fmt in formats:
            try:
                if fmt:
                    # DeviceType=1 Scanner; Intent=0; Bias=0 MaximizeQuality
                    image = dialog.ShowAcquireImage(1, 0, 0, fmt, False, True, False)
                else:
                    image = dialog.ShowAcquireImage()
                if image is None:
                    last_err = "Cancelled"
                    continue
                base = os.path.splitext(out_path)[0] + ".png"
                if _wia_save_image(image, base):
                    for ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"):
                        p = os.path.splitext(out_path)[0] + ext
                        if os.path.isfile(p) and os.path.getsize(p) > 50:
                            return True, p
                    if os.path.isfile(base) and os.path.getsize(base) > 50:
                        return True, base
            except Exception as e:
                last_err = str(e)[:120]
                continue
        return False, last_err
    except Exception as e:
        return False, f"WIA: {str(e)[:140]}"


def scan_acquire(device_key: str, devices_cache: list, out_path: str,
                 dpi: int = 200) -> Tuple[bool, str]:
    """
    Единая точка: выбранное устройство → системный диалог.
    Диалог показывается не более одного раза за вызов.
    """
    errors = []
    dialog_tried = False

    if device_key and device_key.startswith("twain:"):
        for item in devices_cache or []:
            if item[0] == device_key:
                ok, res = twain_scan_to_file(str(item[3]), out_path, dpi)
                if ok:
                    return ok, res
                errors.append(res)
                break
    elif device_key and (device_key.startswith("wia:") or device_key == "dialog"):
        # WIA-устройства сканируем через системный диалог: он сам показывает
        # список устройств, а прямой Connect+Transfer ломается на многих МФУ.
        ok, res = wia_scan_with_dialog(out_path)
        dialog_tried = True
        if ok:
            return ok, res
        errors.append(res)

    if not dialog_tried:
        ok, res = wia_scan_with_dialog(out_path)
        if ok:
            return ok, res
        errors.append(res)
    return False, " | ".join(str(e) for e in errors[-3:])


# -------------------- PDF --------------------

def analyze_pdf(path: str) -> dict:
    info = {"pages": 0, "images": 0, "text_len": 0, "is_scanned_like": False,
            "avg_image_dpi": 0.0, "encrypted": False, "error": ""}
    if not fitz:
        return info
    doc = None
    try:
        doc = fitz.open(path)
        if getattr(doc, "is_encrypted", False) or getattr(doc, "needs_pass", False):
            try:
                ok = doc.authenticate("")
            except Exception:
                ok = False
            if not ok:
                info["encrypted"] = True
                info["error"] = "protected"
                return info
        info["pages"] = doc.page_count
        total_dpi = dpi_samples = text_len = image_count = 0
        for i in range(min(info["pages"], 12)):
            page = doc[i]
            try:
                text_len += len(page.get_text("text") or "")
            except Exception:
                pass
            try:
                # get_image_info: размеры БЕЗ декодирования пикселей
                for im in page.get_image_info():
                    image_count += 1
                    if dpi_samples < 5:
                        try:
                            w = im.get("width", 0)
                            bbox = im.get("bbox")
                            bw = (bbox[2] - bbox[0]) if bbox else 0
                            if w > 0 and bw > 0:
                                total_dpi += w * 72.0 / bw
                                dpi_samples += 1
                        except Exception:
                            pass
            except Exception:
                pass
        info["images"] = image_count
        info["text_len"] = text_len
        if dpi_samples:
            info["avg_image_dpi"] = total_dpi / dpi_samples
        pages = max(1, info["pages"])
        info["is_scanned_like"] = image_count >= max(1, pages // 2) and text_len < 40 * pages
    except Exception as e:
        _log_exc("analyze_pdf", e)
    finally:
        if doc:
            try:
                doc.close()
            except Exception:
                pass
    return info


def resolve_pdf_params(preset: str, dpi: int, quality: int, info: dict) -> Tuple[int, int, bool]:
    """Пресет → конкретные (dpi, quality, structure_only)."""
    structure_only = False
    if preset == "smart":
        if info.get("is_scanned_like"):
            dpi, quality = (100, 42) if info.get("avg_image_dpi", 0) > 140 else (120, 48)
        else:
            dpi, quality = 135, 56
    elif preset == "aggressive":
        dpi, quality = 85, 35
    elif preset == "max":
        dpi, quality = 80, 34
    elif preset == "balanced":
        dpi, quality = 118, 50
    elif preset == "quality":
        dpi, quality = 165, 68
    elif preset == "structure":
        structure_only = True
        dpi, quality = 130, 60
    elif preset == "images":
        dpi, quality = 95, 40
    else:  # custom
        dpi = max(50, min(300, safe_int(dpi, 130)))
        quality = max(20, min(95, safe_int(quality, 55)))
    return dpi, quality, structure_only


def process_pdf(inp: str, out: str, preset: str, dpi: int, quality: int, gray: bool,
                _resolved: dict = None, progress_cb=None):
    """
    Сжатие одного PDF. progress_cb(0..1) — прогресс по стадиям.
    _resolved — заранее рассчитанные параметры (для кусков многопоточного
    режима): одинаковое качество на всех страницах и без повторного анализа.
    """
    def cb(frac):
        if progress_cb:
            try:
                progress_cb(frac)
            except Exception:
                pass

    if not fitz:
        return inp, False, "err_pymupdf", 0, 0
    orig = 0
    tmp = None
    try:
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0
        orig = os.path.getsize(inp)
        if not ensure_disk_space(inp, int(orig * 1.2)):
            return inp, False, "err_no_space", orig, 0
        cb(0.05)

        if _resolved:
            rdpi = _resolved["dpi"]
            rq = _resolved["quality"]
            structure_only = _resolved["structure_only"]
            use_gray_raster = _resolved["use_gray_raster"]
        else:
            info = analyze_pdf(inp)
            if info.get("encrypted"):
                return inp, False, "err_protected", orig, 0
            rdpi, rq, structure_only = resolve_pdf_params(preset, dpi, quality, info)
            use_gray_raster = bool(gray) and bool(info.get("is_scanned_like")) \
                and not structure_only
            if gray and not use_gray_raster and not structure_only:
                # Текстовый PDF: цвет не трогаем, только жмём чуть сильнее
                rq = max(28, rq - 8)

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(TEMP_DIR))
        os.close(fd)
        cb(0.12)

        doc = fitz.open(inp)
        try:
            if use_gray_raster:
                # Безопасный серый для сканов: страница → серая картинка →
                # новый PDF. DPI не ниже 110, иначе текст «плывёт».
                render_dpi = max(110, min(int(rdpi), 160))
                zoom = render_dpi / 72.0
                mat = fitz.Matrix(zoom, zoom)
                new_doc = fitz.open()
                n_pages = max(1, doc.page_count)
                for page_index in range(doc.page_count):
                    page = doc[page_index]
                    try:
                        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY,
                                              alpha=False)
                        rect = page.rect
                        new_page = new_doc.new_page(width=rect.width, height=rect.height)
                        img_bytes = pix.tobytes(
                            "jpeg", jpg_quality=max(35, min(85, rq + 5)))
                        new_page.insert_image(new_page.rect, stream=img_bytes)
                        pix = None
                    except Exception:
                        try:
                            new_doc.insert_pdf(doc, from_page=page_index,
                                               to_page=page_index)
                        except Exception:
                            pass
                    cb(0.12 + 0.66 * (page_index + 1) / n_pages)
                doc.close()
                doc = new_doc
            elif not structure_only:
                if hasattr(doc, "rewrite_images"):
                    try:
                        cb(0.2)
                        doc.rewrite_images(
                            dpi_threshold=max(rdpi + 8, 55),
                            dpi_target=rdpi,
                            quality=rq,
                            lossy=True,
                            lossless=True,
                            bitonal=True,
                            color=True,
                            gray=True,
                            set_to_gray=False,
                        )
                    except Exception as e:
                        _log_exc("rewrite_images", e)
                cb(0.78)

            kw = dict(garbage=4, deflate=True, deflate_images=True,
                      deflate_fonts=True, use_objstms=1)
            try:
                doc.save(tmp, clean=True, **kw)
            except Exception:
                doc.save(tmp, **kw)
            cb(0.88)
        finally:
            try:
                doc.close()
            except Exception:
                pass

        if pikepdf:
            try:
                with pikepdf.open(tmp) as pdf:
                    try:
                        if "/Metadata" in pdf.Root:
                            del pdf.Root.Metadata
                    except Exception:
                        pass
                    pdf.save(out, compress_streams=True,
                             object_stream_mode=pikepdf.ObjectStreamMode.generate,
                             recompress_flate=True)
            except Exception as e:
                _log_exc("pikepdf pass", e)
                shutil.copy2(tmp, out)
        else:
            shutil.copy2(tmp, out)
        cb(0.97)

        new = os.path.getsize(out)
        if new >= orig * 0.985:
            try:
                os.unlink(out)
            except Exception:
                pass
            return inp, False, "err_no_gain", orig, orig
        return inp, True, "OK", orig, new
    except PermissionError as e:
        _log_exc("process_pdf perm", e)
        return inp, False, "err_no_access", orig, 0
    except Exception as e:
        _log_exc("process_pdf", e)
        return inp, False, str(e)[:120], orig, 0
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def process_pdf_chunk(args):
    """
    Сжать кусок PDF (несколько страниц). Если сжатие куска не дало выигрыша
    или упало — кусок берётся как есть (страницы не теряются и вся работа
    не перезапускается).
    """
    raw_path, tmp_out, resolved = args
    try:
        _i, ok, msg, _o, _n = process_pdf(raw_path, tmp_out, "_resolved",
                                          0, 0, False, _resolved=resolved)
        if not ok:
            shutil.copy2(raw_path, tmp_out)
            ok = True
            msg = "copy"
        return tmp_out, ok, msg, 1
    except Exception as e:
        try:
            shutil.copy2(raw_path, tmp_out)
            return tmp_out, True, "copy", 1
        except Exception:
            return tmp_out, False, str(e)[:100], 0


def process_single_pdf_parallel(inp: str, out: str, preset: str, dpi: int,
                                quality: int, gray: bool, workers: int = 0,
                                progress_cb=None) -> Tuple[str, bool, str, int, int]:
    """
    Многостраничный PDF → параллель по кускам страниц:
    1) анализ и выбор параметров ОДИН раз (одинаковое качество всех страниц);
    2) нарезка; 3) куски параллельно; 4) склейка + pikepdf.
    """
    orig = 0
    raw_chunks: List[Tuple[int, str, int]] = []
    results: List[Tuple[int, str]] = []
    try:
        if not fitz:
            return process_pdf(inp, out, preset, dpi, quality, gray)
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0

        if progress_cb:
            progress_cb(0, 1, "…")

        orig = os.path.getsize(inp)
        if not ensure_disk_space(inp, int(orig * 1.5)):
            return inp, False, "err_no_space", orig, 0

        info = analyze_pdf(inp)
        if info.get("encrypted"):
            return inp, False, "err_protected", orig, 0
        rdpi, rq, structure_only = resolve_pdf_params(preset, dpi, quality, info)
        use_gray_raster = bool(gray) and bool(info.get("is_scanned_like")) \
            and not structure_only
        if gray and not use_gray_raster and not structure_only:
            rq = max(28, rq - 8)
        resolved = {"dpi": rdpi, "quality": rq, "structure_only": structure_only,
                    "use_gray_raster": use_gray_raster}

        doc = fitz.open(inp)
        n_pages = doc.page_count
        workers = workers or MAX_WORKERS
        workers = max(1, min(workers, 8))

        if n_pages < 4 or workers <= 1:
            doc.close()
            res = process_pdf(inp, out, preset, dpi, quality, gray)
            if progress_cb:
                progress_cb(n_pages, max(n_pages, 1),
                            t("done") if res[1] else t("error"))
            return res

        chunk_size = max(2, (n_pages + workers - 1) // workers)
        if n_pages >= 30:
            chunk_size = max(5, min(15, n_pages // max(workers, 1)))

        TEMP_DIR.mkdir(parents=True, exist_ok=True)

        for start in range(0, n_pages, chunk_size):
            if stop_flag.is_set():
                doc.close()
                return inp, False, "stopped", orig, 0
            end = min(n_pages - 1, start + chunk_size - 1)
            part = fitz.open()
            part.insert_pdf(doc, from_page=start, to_page=end)
            raw = str(TEMP_DIR / f"chunk_raw_{start}_{end}_{time.time_ns()}.pdf")
            part.save(raw)
            part.close()
            raw_chunks.append((start, raw, end - start + 1))
            if progress_cb:
                progress_cb(end + 1, n_pages, f"{end + 1}/{n_pages}")
        doc.close()

        if progress_cb:
            progress_cb(0, n_pages, f"⇉ {len(raw_chunks)} × {workers}")

        done_pages = 0
        jobs = []
        for start, raw, pc in raw_chunks:
            tmp_out = str(TEMP_DIR / f"chunk_out_{start}_{time.time_ns()}.pdf")
            jobs.append((start, raw, tmp_out, pc))

        failed = False
        with get_executor(min(workers, len(jobs))) as ex:
            futs = {ex.submit(process_pdf_chunk, (raw, tmp_out, resolved)): (start, raw, tmp_out, pc)
                    for start, raw, tmp_out, pc in jobs}
            for fut in as_completed(futs):
                start, raw, tmp_out, pc = futs[fut]
                try:
                    out_path, ok, msg, _ = fut.result()
                except Exception as e:
                    _log_exc("chunk future", e)
                    ok, out_path = False, tmp_out
                try:
                    if os.path.isfile(raw):
                        os.unlink(raw)
                except Exception:
                    pass
                if stop_flag.is_set():
                    continue
                if ok and os.path.isfile(out_path):
                    results.append((start, out_path))
                    done_pages += pc
                    if progress_cb:
                        progress_cb(done_pages, n_pages, f"{done_pages}/{n_pages}")
                else:
                    failed = True

        if stop_flag.is_set():
            return inp, False, "stopped", orig, 0
        if failed or not results:
            # редчайший случай (кусок даже скопировать не удалось)
            for _, p in results:
                try:
                    os.unlink(p)
                except Exception:
                    pass
            if progress_cb:
                progress_cb(0, n_pages, t("fallback_single"))
            return process_pdf(inp, out, preset, dpi, quality, gray)

        if progress_cb:
            progress_cb(n_pages, n_pages, "…PDF")

        results.sort(key=lambda x: x[0])
        merged = fitz.open()
        for _, p in results:
            try:
                part = fitz.open(p)
                merged.insert_pdf(part)
                part.close()
            except Exception as e:
                _log_exc("merge chunk", e)
            try:
                os.unlink(p)
            except Exception:
                pass
        results = []

        if merged.page_count == 0:
            merged.close()
            return process_pdf(inp, out, preset, dpi, quality, gray)

        raw_merged = str(TEMP_DIR / f"merged_{time.time_ns()}.pdf")
        merged.save(raw_merged, garbage=4, deflate=True)
        merged.close()

        if pikepdf:
            try:
                with pikepdf.open(raw_merged) as pdf:
                    pdf.save(out, compress_streams=True,
                             object_stream_mode=pikepdf.ObjectStreamMode.generate,
                             recompress_flate=True)
            except Exception as e:
                _log_exc("pikepdf merged", e)
                shutil.copy2(raw_merged, out)
        else:
            shutil.copy2(raw_merged, out)
        try:
            os.unlink(raw_merged)
        except Exception:
            pass

        new = os.path.getsize(out) if os.path.isfile(out) else 0
        if new < 100 or new >= orig * 0.985:
            try:
                if os.path.isfile(out):
                    os.unlink(out)
            except Exception:
                pass
            return inp, False, "err_no_gain", orig, orig

        if progress_cb:
            progress_cb(n_pages, n_pages, t("done"))
        return inp, True, "OK", orig, new
    except PermissionError as e:
        _log_exc("process_pdf_parallel perm", e)
        return inp, False, "err_no_access", orig, 0
    except Exception as e:
        _log_exc("process_pdf_parallel", e)
        return inp, False, str(e)[:120], orig, 0
    finally:
        # никаких сирот в TEMP
        for _s, raw, _pc in raw_chunks:
            try:
                if os.path.isfile(raw):
                    os.unlink(raw)
            except Exception:
                pass
        for _s, p in results:
            try:
                if os.path.isfile(p):
                    os.unlink(p)
            except Exception:
                pass


# -------------------- Image --------------------

def is_photo_like_path(path: str) -> bool:
    """Фото или графика/скрин? Отдельное открытие + draft → быстро."""
    try:
        with Image.open(path) as im:
            im.draft("RGB", (96, 96))  # JPEG декодируется сразу уменьшенным
            small = im.convert("RGB").resize((48, 48), Image.Resampling.BILINEAR)
        stat = ImageStat.Stat(small)
        if sum(stat.stddev) > 42:
            return True
        colors = small.getcolors(48 * 48)
        return (len(colors) if colors else 0) > 600
    except Exception:
        return True


_JPEG_EXTS = {".jpg", ".jpeg", ".jfif"}


def _same_format_family(inp: str, fmt: str) -> bool:
    ext = Path(inp).suffix.lower()
    fmt = fmt.upper()
    if fmt in ("JPEG", "JPG"):
        return ext in _JPEG_EXTS
    if fmt == "WEBP":
        return ext == ".webp"
    if fmt == "PNG":
        return ext == ".png"
    return False


def process_image(inp: str, out: str, quality: int, max_side: int, fmt: str, smart: bool):
    """Сжатие изображения: ICC сохраняется, PNG-графика квантуется,
    результат без выигрыша откатывается (оригинал не трогается)."""
    orig = 0
    try:
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0
        orig = os.path.getsize(inp)

        photo_like = is_photo_like_path(inp) if smart else True

        with Image.open(inp) as img0:
            icc = img0.info.get("icc_profile")
            img = ImageOps.exif_transpose(img0)
            img.load()

        fmt = fmt.upper()
        q = max(15, min(95, safe_int(quality, 75)))

        # Умный режим: для графики/скринов чуть выше качество JPEG
        if smart and not photo_like and fmt in ("JPEG", "JPG"):
            q = min(90, q + 8)

        # Приводим к RGB для JPEG
        if fmt in ("JPEG", "JPG") and img.mode in ("RGBA", "P", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            mask = img.split()[-1] if img.mode in ("RGBA", "LA") else None
            bg.paste(img, mask=mask)
            img = bg
        elif img.mode not in ("RGB", "L", "RGBA"):
            img = img.convert("RGB")

        # Ресайз по длинной стороне
        ms = safe_int(max_side, 0, 0, 20000)
        if ms > 0:
            w, h = img.size
            if max(w, h) > ms:
                if w >= h:
                    img = img.resize((ms, max(1, int(h * ms / w))),
                                     Image.Resampling.LANCZOS)
                else:
                    img = img.resize((max(1, int(w * ms / h)), ms),
                                     Image.Resampling.LANCZOS)

        if fmt in ("JPEG", "JPG"):
            kw = dict(format="JPEG", quality=q, optimize=True, progressive=True,
                      subsampling=0 if q >= 90 else 2)
            if not out.lower().endswith((".jpg", ".jpeg")):
                out = os.path.splitext(out)[0] + ".jpg"
        elif fmt == "WEBP":
            kw = dict(format="WEBP", quality=q, method=6)
            if not out.lower().endswith(".webp"):
                out = os.path.splitext(out)[0] + ".webp"
        else:
            kw = dict(format="PNG", optimize=True, compress_level=9)
            if not out.lower().endswith(".png"):
                out = os.path.splitext(out)[0] + ".png"
            # Скрины/графику квантуем в палитру — реальный выигрыш PNG
            if smart and not photo_like and img.mode in ("RGB", "RGBA"):
                try:
                    img = img.quantize(256, method=Image.Quantize.FASTOCTREE)
                except Exception as e:
                    _log_exc("png quantize", e)

        if icc:
            kw["icc_profile"] = icc

        img.save(out, **kw)

        new = os.path.getsize(out)
        if new >= orig * 0.98 and _same_format_family(inp, fmt):
            # тот же формат и почти без выигрыша (или больше) → откат
            try:
                os.unlink(out)
            except Exception:
                pass
            return inp, False, "err_no_gain", orig, orig
        return inp, True, "OK", orig, new
    except PermissionError as e:
        _log_exc("process_image perm", e)
        return inp, False, "err_no_access", orig, 0
    except Exception as e:
        _log_exc("process_image", e)
        return inp, False, str(e)[:110], orig, 0


def images_to_pdf(paths: List[str], output_pdf: str,
                  progress_cb=None) -> Tuple[bool, str, int, int]:
    """
    Фото/сканы → PDF с корректным физическим размером страницы:
    - если в файле есть DPI → точный размер (px / dpi * 72 pt);
    - иначе страница A4 (портрет/альбом по пропорции), картинка вписывается.
    JPEG вставляется как есть (без перекодирования). progress_cb(i, total).
    Возвращает (ok, msg, added, skipped).
    """
    if not fitz:
        return False, "err_pymupdf", 0, len(paths)
    A4_W, A4_H = 595.0, 842.0
    try:
        doc = fitz.open()
        added = 0
        total = max(1, len(paths))
        for pi, p in enumerate(paths, 1):
            try:
                with Image.open(p) as im0:
                    im = ImageOps.exif_transpose(im0)
                    w_px, h_px = im.size
                    dpi = 0.0
                    d = im.info.get("dpi")
                    try:
                        if d and float(d[0]) >= 50:
                            dpi = float(d[0])
                    except Exception:
                        dpi = 0.0
                if dpi:
                    w_pt = w_px * 72.0 / dpi
                    h_pt = h_px * 72.0 / dpi
                else:
                    w_pt, h_pt = (A4_H, A4_W) if w_px > h_px else (A4_W, A4_H)
                page = doc.new_page(width=w_pt, height=h_pt)
                page.insert_image(page.rect, filename=p, keep_proportion=True)
                added += 1
            except Exception as e:
                _log_exc(f"images_to_pdf {os.path.basename(p)}", e)
                continue
            finally:
                if progress_cb:
                    try:
                        progress_cb(pi, total)
                    except Exception:
                        pass
        if added == 0:
            doc.close()
            return False, "err_add_pages", 0, len(paths)
        doc.save(output_pdf, garbage=4, deflate=True)
        doc.close()
        return True, "OK", added, len(paths) - added
    except Exception as e:
        return False, str(e)[:120], 0, len(paths)

# -------------------- GUI --------------------

# Палитра v4: один акцент (бирюза), тихие нейтрали, семантика цвета.
# Кортеж = (светлая тема, тёмная тема) — customtkinter выбирает сам.
CLR_ACC = ("#0e9384", "#2fd4c2")          # акцент — главные действия
CLR_ACC_HOVER = ("#0c7f73", "#54dccd")
CLR_ACC_TEXT = ("#ffffff", "#082622")
CLR_GHOST = ("#e4eae8", "#2a3431")        # тихие второстепенные кнопки
CLR_GHOST_HOVER = ("#d6dedb", "#354240")
CLR_GHOST_TEXT = ("#1a2422", "#e7edeb")
CLR_MUTED = ("#5b6b67", "#9cada8")
CLR_DANGER_TEXT = ("#b91c1c", "#f87171")  # красный — только опасность
CLR_SIDEBAR = ("#e9edec", "#121817")
CLR_LIST_BG = ("#f6f8f7", "#1a211f")
CLR_LIST_FG = ("#182220", "#e6edeb")
CLR_ROW = ("#ffffff", "#222a28")          # карточка файла
CLR_ROW_BORDER = ("#e2e8e6", "#2c3634")
CLR_OK = ("#15803d", "#4ade80")           # статусы
CLR_OK_SOFT = ("#ddf2e4", "#12351f")
CLR_ERR = ("#b91c1c", "#f87171")
CLR_ERR_SOFT = ("#fbdedb", "#431514")
CLR_WARN = ("#b45309", "#fbbf24")
CLR_ACC_SOFT = ("#d9efec", "#12403a")     # мягкая подложка бейджей


def human_size(nbytes: float) -> str:
    """18,2 МБ / 18.2 MB — по текущему языку."""
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ") if LANG == "ru" else \
            ("B", "KB", "MB", "GB", "TB")
    v = float(max(0, nbytes))
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    s = f"{v:.1f}" if i >= 2 else f"{v:.0f}"
    if LANG == "ru":
        s = s.replace(".", ",")
    return f"{s} {units[i]}"


# ---- иконки: рисуются кодом, две темы, кэш ----

_ICON_CACHE: dict = {}


def _draw_icon(name: str, color) -> "Image.Image":
    """Простые линейные иконки 64×64 (масштабируются CTkImage)."""
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    from PIL import ImageDraw as _ID
    d = _ID.Draw(im)
    c = color
    w = 6
    if name == "doc":
        d.rounded_rectangle([16, 8, 48, 56], radius=6, outline=c, width=w)
        d.line([26, 24, 38, 24], fill=c, width=w)
        d.line([26, 34, 38, 34], fill=c, width=w)
        d.line([26, 44, 34, 44], fill=c, width=w)
    elif name == "img":
        d.rounded_rectangle([8, 12, 56, 52], radius=6, outline=c, width=w)
        d.ellipse([17, 20, 27, 30], outline=c, width=4)
        d.line([14, 45, 30, 32], fill=c, width=w)
        d.line([30, 32, 40, 40], fill=c, width=w)
        d.line([40, 40, 51, 31], fill=c, width=w)
    elif name == "vid":
        d.rounded_rectangle([8, 14, 56, 50], radius=6, outline=c, width=w)
        d.polygon([(27, 24), (27, 40), (41, 32)], fill=c)
    elif name == "scan":
        d.line([18, 22, 18, 10, 46, 10, 46, 22], fill=c, width=w, joint="curve")
        d.rounded_rectangle([8, 22, 56, 42], radius=5, outline=c, width=w)
        d.line([18, 42, 18, 54, 46, 54, 46, 42], fill=c, width=w, joint="curve")
    elif name == "hist":
        d.ellipse([10, 10, 54, 54], outline=c, width=w)
        d.line([32, 20, 32, 34], fill=c, width=w)
        d.line([32, 34, 42, 40], fill=c, width=w)
    elif name == "gear":  # «настройки» — ползунки
        d.line([12, 21, 52, 21], fill=c, width=w)
        d.ellipse([34, 13, 50, 29], fill=c)
        d.line([12, 43, 52, 43], fill=c, width=w)
        d.ellipse([14, 35, 30, 51], fill=c)
    elif name == "plus":
        d.line([32, 14, 32, 50], fill=c, width=w + 1)
        d.line([14, 32, 50, 32], fill=c, width=w + 1)
    elif name == "check":
        d.line([14, 34, 27, 46], fill=c, width=w + 2, joint="curve")
        d.line([27, 46, 50, 20], fill=c, width=w + 2, joint="curve")
    elif name == "zap":
        d.polygon([(36, 8), (18, 36), (30, 36), (26, 56), (46, 26), (34, 26)],
                  fill=c)
    elif name == "heart":  # supporting the project
        d.ellipse([10, 14, 34, 38], fill=c)
        d.ellipse([30, 14, 54, 38], fill=c)
        d.polygon([(12, 30), (52, 30), (32, 56)], fill=c)
    elif name == "code":  # source code
        d.line([24, 18, 10, 32, 24, 46], fill=c, width=w, joint="curve")
        d.line([40, 18, 54, 32, 40, 46], fill=c, width=w, joint="curve")
    elif name == "drop":  # стрелка в лоток — пустое состояние
        d.line([32, 8, 32, 34], fill=c, width=w)
        d.line([22, 26, 32, 36], fill=c, width=w, joint="curve")
        d.line([42, 26, 32, 36], fill=c, width=w, joint="curve")
        d.line([12, 42, 12, 54, 52, 54, 52, 42], fill=c, width=w,
               joint="curve")
    return im


def icon(name: str, size: int = 18):
    """Иконка в цвете muted (для тихих кнопок и навигации)."""
    key = (name, size, "n")
    if key not in _ICON_CACHE:
        _ICON_CACHE[key] = ctk.CTkImage(
            light_image=_draw_icon(name, (91, 107, 103, 255)),
            dark_image=_draw_icon(name, (156, 173, 168, 255)),
            size=(size, size))
    return _ICON_CACHE[key]


def icon_active(name: str, size: int = 18):
    """Иконка в цвете текста на акценте (для активных/главных кнопок)."""
    key = (name, size, "a")
    if key not in _ICON_CACHE:
        _ICON_CACHE[key] = ctk.CTkImage(
            light_image=_draw_icon(name, (255, 255, 255, 255)),
            dark_image=_draw_icon(name, (8, 38, 34, 255)),
            size=(size, size))
    return _ICON_CACHE[key]


def btn_primary(parent, **kw):
    kw.setdefault("fg_color", CLR_ACC)
    kw.setdefault("hover_color", CLR_ACC_HOVER)
    kw.setdefault("text_color", CLR_ACC_TEXT)
    return ctk.CTkButton(parent, **kw)


def btn_ghost(parent, **kw):
    kw.setdefault("fg_color", CLR_GHOST)
    kw.setdefault("hover_color", CLR_GHOST_HOVER)
    kw.setdefault("text_color", CLR_GHOST_TEXT)
    return ctk.CTkButton(parent, **kw)


def style_listbox(lb: tk.Listbox, select_bg: str = None):
    """Цвета tk.Listbox по текущей теме. Выделение — всегда акцентом."""
    try:
        if ctk.get_appearance_mode() == "Dark":
            lb.configure(bg=CLR_LIST_BG[1], fg=CLR_LIST_FG[1],
                         selectbackground=CLR_ACC[1], selectforeground=CLR_ACC_TEXT[1])
        else:
            lb.configure(bg=CLR_LIST_BG[0], fg=CLR_LIST_FG[0],
                         selectbackground=CLR_ACC[0], selectforeground=CLR_ACC_TEXT[0])
    except Exception:
        pass


class BaseSection(ctk.CTkFrame):
    """Общий каркас раздела: список файлов, лог, прогресс, пакетный движок."""

    EXTS: set = set()
    EMPTY_KEY = "no_files"
    FILETYPES = [("All", "*.*")]
    MODE = "?"
    THUMBS = False  # миниатюры в строках (раздел Фото)

    def __init__(self, app, title_key: str):
        super().__init__(app.content, fg_color="transparent")
        self.app = app
        self.settings = load_settings()
        self.files: List[str] = []
        self.is_running = False
        self.msg_queue: queue.Queue = queue.Queue()
        self._futures = []
        self._lbs: List[tk.Listbox] = []
        self._rows: dict = {}       # path -> виджеты строки-карточки
        self._selected: set = set()
        self._sizes: dict = {}      # path -> размер, для сводки
        self._toast = None
        self._busy = False          # неопределённый прогресс (сканер и т.п.)
        self._thumb_cache: dict = {}  # path -> CTkImage
        self._poll_id = None
        self._title_key = title_key
        self.btn = None
        self.btn_stop = None

        self._build()
        self._apply()
        self._poll_id = self.after(60, self._poll)

        app.bind("<Control-Return>", self._ev(self._start))
        app.bind("<Delete>", self._ev(self._remove))
        app.bind("<Control-o>", self._ev(self._add))
        app.bind("<Escape>", self._ev(self._stop))

    # ---- инфраструктура ----

    def _ev(self, fn):
        def handler(_e=None):
            try:
                if self.winfo_exists():
                    fn()
            except Exception as ex:
                _log_exc("hotkey", ex)
        return handler

    def on_leave(self):
        """Вызывается контейнером перед уничтожением раздела."""
        try:
            if self._poll_id:
                self.after_cancel(self._poll_id)
                self._poll_id = None
        except Exception:
            pass
        try:
            if hasattr(self, "_thumb_q"):
                self._thumb_q.put(None)  # завершить поток миниатюр
        except Exception:
            pass
        if self.is_running:
            stop_flag.set()
            try:
                kill_active_processes()
            except Exception:
                pass
        s = None
        try:
            s = self._collect()
        except Exception as e:
            _log_exc("collect on leave", e)
        if isinstance(s, dict):
            try:
                save_settings(s)
            except Exception as e:
                _log_exc("save on leave", e)

    def restyle(self):
        """Перекраска tk-виджетов после смены темы (вызывает App)."""
        for lb in self._lbs:
            style_listbox(lb)

    def _log(self, m: str):
        """Всегда в лог-файл; в текстовое поле — только если оно есть (сканер)."""
        log.info("[ui] %s", m)
        box = getattr(self, "log", None)
        if box is None:
            return
        try:
            box.configure(state="normal")
            box.insert("end", m + "\n")
            box.see("end")
            box.configure(state="disabled")
        except Exception:
            pass

    def _update_count(self):
        try:
            self.count_label.configure(text=str(len(self.files)))
        except Exception:
            pass
        hint = getattr(self, "empty_hint", None)
        if hint is not None:
            try:
                if self.files:
                    hint.place_forget()
                else:
                    hint.place(relx=0.5, rely=0.45, anchor="center")
            except Exception:
                pass
        self._update_summary()

    def _est_ratio(self):
        """Ожидаемая доля размера после сжатия (None = не показывать)."""
        return None

    def _update_summary(self):
        lbl = getattr(self, "summary_lbl", None)
        if lbl is None:
            return
        if self.is_running:
            return  # во время работы сводку ведёт run_summary/sum_done
        try:
            n = len(self.files)
            if not n:
                lbl.configure(text="")
                return
            total = sum(self._sizes.get(p, 0) for p in self.files)
            ratio = self._est_ratio()
            if ratio:
                text = t("sum_idle_est").format(
                    n=n, size=human_size(total), est=human_size(total * ratio))
            else:
                text = t("sum_idle").format(n=n, size=human_size(total))
            if not self.is_running:
                text += f"    ·    {t('hint_start')}"
            lbl.configure(text=text)
        except Exception:
            pass

    def _finish_run(self, status_key: str, color):
        self.is_running = False
        self._futures = []
        if self.btn:
            self.btn.configure(state="normal")
        if self.btn_stop:
            try:
                self.btn_stop.grid_remove()
            except Exception:
                pass
        try:
            self.status.configure(text=t(status_key), text_color=color)
        except Exception:
            pass

    def _poll(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()
                k = item[0]
                if k == "progress":
                    if self._busy:
                        self._busy = False
                        try:
                            self.progress.stop()
                        except Exception:
                            pass
                        self.progress.configure(mode="determinate")
                    self.progress.set(item[1])
                    self.status.configure(text=item[2])
                elif k == "busy":
                    if item[1] and not self._busy:
                        self._busy = True
                        self.progress.configure(mode="indeterminate")
                        self.progress.start()
                    elif not item[1] and self._busy:
                        self._busy = False
                        try:
                            self.progress.stop()
                        except Exception:
                            pass
                        self.progress.configure(mode="determinate")
                        self.progress.set(0)
                elif k == "log":
                    self._log(item[1])
                elif k == "scan_refresh":
                    if hasattr(self, "_refresh_pages_ui"):
                        self._refresh_pages_ui()
                elif k == "thumb":
                    self._apply_thumb(item[1], item[2], item[3])
                elif k == "badge":
                    self._set_badge(item[1])
                elif k == "toast":
                    folder = item[3]
                    action = None
                    if folder and os.path.isdir(folder):
                        action = (t("history_open"),
                                  lambda f=folder: os.startfile(f))
                    self.show_toast(item[1], item[2], action)
                elif k == "op_end":
                    self.is_running = False
                    if self.btn:
                        self.btn.configure(state="normal")
                    self.progress.set(1.0)
                    self.status.configure(text=t("done"), text_color=CLR_ACC)
                elif k == "file":
                    self._set_row_state(item[1], item[2], item[3])
                elif k == "file_all":
                    for p in item[1]:
                        self._set_row_state(p, "queued", None)
                elif k == "run_summary":
                    done, total, saved = item[1], item[2], item[3]
                    lbl = getattr(self, "summary_lbl", None)
                    if lbl is not None:
                        lbl.configure(text=t("sum_run").format(
                            done=done, total=total, size=human_size(saved)))
                elif k == "done":
                    p = item[1] or {}
                    self._finish_run("done", CLR_ACC)
                    self.progress.set(1.0)
                    lbl = getattr(self, "summary_lbl", None)
                    if lbl is not None:
                        lbl.configure(text=t("sum_done").format(
                            ok=p.get("ok", 0), total=p.get("total", 0),
                            size=human_size(p.get("saved", 0))))
                    if load_settings().get("sound", True):
                        play_sound()
                    folder = p.get("folder") or ""
                    action = None
                    if folder and os.path.isdir(folder):
                        action = (t("history_open"),
                                  lambda f=folder: os.startfile(f))
                    self.show_toast(t("toast_done").format(
                        ok=p.get("ok", 0), total=p.get("total", 0),
                        size=human_size(p.get("saved", 0))), "ok", action)
                elif k == "stopped":
                    self._finish_run("stopped", CLR_WARN)
                    self.show_toast(t("stopped"), "warn")
                elif k == "error":
                    self._finish_run("error", CLR_ERR)
                    if item[1]:
                        self.show_toast(str(item[1]), "err")
        except queue.Empty:
            pass
        except Exception as e:
            _log_exc("poll", e)
        self._poll_id = self.after(60, self._poll)

    def show_toast(self, text: str, kind: str = "ok", action=None,
                   timeout: int = 6000):
        """Ненавязчивое уведомление в углу вместо модального окна."""
        try:
            if self._toast is not None and self._toast.winfo_exists():
                self._toast.destroy()
        except Exception:
            pass
        try:
            stripe = {"ok": CLR_ACC, "warn": CLR_WARN, "err": CLR_ERR}.get(
                kind, CLR_ACC)
            tf = ctk.CTkFrame(self, corner_radius=10, border_width=1,
                              border_color=CLR_ROW_BORDER, fg_color=CLR_ROW)
            # height=12 — иначе CTkFrame растянет тост до дефолтных 200px
            ctk.CTkFrame(tf, width=4, height=12, corner_radius=3,
                         fg_color=stripe).pack(side="left", fill="y",
                                               padx=(8, 0), pady=8)
            ctk.CTkLabel(tf, text=text, justify="left", wraplength=380,
                         font=ctk.CTkFont(size=13)).pack(side="left",
                                                         padx=10, pady=9)
            if action:
                a_lbl, a_cb = action
                ctk.CTkButton(tf, text=a_lbl, height=28, width=10,
                              fg_color="transparent", hover_color=CLR_GHOST,
                              text_color=CLR_ACC,
                              font=ctk.CTkFont(size=13, weight="bold"),
                              command=lambda: (a_cb(), tf.destroy())).pack(
                    side="left", padx=(0, 2))
            ctk.CTkButton(tf, text="✕", width=28, height=28,
                          fg_color="transparent", hover_color=CLR_GHOST,
                          text_color=CLR_MUTED,
                          command=tf.destroy).pack(side="left", padx=(0, 6))
            # плавное появление снизу (ease-out), уход — вниз
            base_y = -92
            slide_in = (34, 22, 13, 6, 2, 0)
            slide_out = (0, 6, 14, 26, 40)
            tf.place(relx=1.0, rely=1.0, x=-24, y=base_y + slide_in[0],
                     anchor="se")
            self._toast = tf

            def anim_in(i=0):
                try:
                    if not tf.winfo_exists():
                        return
                    if i < len(slide_in):
                        tf.place_configure(y=base_y + slide_in[i])
                        self.after(16, lambda: anim_in(i + 1))
                except Exception:
                    pass

            def anim_out(i=0):
                try:
                    if not tf.winfo_exists():
                        return
                    if i < len(slide_out):
                        tf.place_configure(y=base_y + slide_out[i])
                        self.after(16, lambda: anim_out(i + 1))
                    else:
                        tf.destroy()
                except Exception:
                    pass

            anim_in()
            self.after(timeout, anim_out)
        except Exception as e:
            _log_exc("toast", e)

    def _stop(self):
        if not self.is_running:
            return
        stop_flag.set()
        self._log("⏹ " + t("stopping"))
        try:
            self.status.configure(text=t("stopping"), text_color=CLR_WARN)
        except Exception:
            pass
        try:
            kill_active_processes()
        except Exception:
            pass
        for f in self._futures:
            try:
                f.cancel()
            except Exception:
                pass

    # ---- сборка типовых блоков UI ----

    def _build_header(self, title: str):
        h = ctk.CTkFrame(self, fg_color="transparent")
        h.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(h, text=title,
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        self.status = ctk.CTkLabel(h, text=t("ready"), text_color=CLR_MUTED)
        self.status.pack(side="right", padx=8)
        # бейдж-фича («⚡ Intel QSV», «HEIC») — появляется по _set_badge
        self.badge = ctk.CTkLabel(h, text="", corner_radius=999,
                                  fg_color=CLR_ACC_SOFT, text_color=CLR_ACC,
                                  font=ctk.CTkFont(size=11, weight="bold"),
                                  height=24)
        self._badge_shown = False

    def _set_badge(self, text: str):
        try:
            if text:
                self.badge.configure(text=f"  {text}  ")
                if not self._badge_shown:
                    self.badge.pack(side="right", padx=(0, 10))
                    self._badge_shown = True
            elif self._badge_shown:
                self.badge.pack_forget()
                self._badge_shown = False
        except Exception:
            pass

    def _build_filecard(self, files_label: str, select_bg: str = None,
                        extra=None, sort_key: str = None):
        card = ctk.CTkFrame(self, corner_radius=12)
        card.pack(fill="both", expand=True, padx=16, pady=4)
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 2))
        ctk.CTkLabel(top, text=files_label,
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=CLR_MUTED).pack(side="left")
        self.count_label = ctk.CTkLabel(top, text="0", text_color=CLR_MUTED,
                                        font=ctk.CTkFont(size=13))
        self.count_label.pack(side="right")

        # сортировка списка — выбор запоминается для раздела
        self._sort_key = sort_key
        self._sort_labels = {t("sort_added"): "added", t("sort_name"): "name",
                             t("sort_size"): "size"}
        saved_sort = load_settings().get(sort_key, "added") if sort_key else "added"
        cur_label = next((lb for lb, v in self._sort_labels.items()
                          if v == saved_sort), t("sort_added"))
        self._sort_var = ctk.StringVar(value=cur_label)
        ctk.CTkOptionMenu(top, variable=self._sort_var,
                          values=list(self._sort_labels.keys()), width=150,
                          height=26, fg_color=CLR_GHOST,
                          button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_MUTED,
                          font=ctk.CTkFont(size=12),
                          command=self._on_sort).pack(side="right",
                                                      padx=(0, 10))
        self._added_seq: dict = {}
        self._seq_counter = 0

        # постоянная полоса-подсказка: перетаскивание работает всегда
        strip = ctk.CTkFrame(card, corner_radius=9, fg_color="transparent",
                             border_width=1, border_color=CLR_ROW_BORDER)
        strip.pack(fill="x", padx=12, pady=(4, 2))
        ctk.CTkLabel(strip, text=t("drop_strip"), image=icon("drop", 15),
                     compound="left", padx=8,
                     font=ctk.CTkFont(size=12),
                     text_color=CLR_MUTED).pack(pady=3)

        # Файлы — строки-карточки: имя, папка, размер, статус/прогресс
        self.rowsarea = ctk.CTkScrollableFrame(card, fg_color=CLR_LIST_BG,
                                               corner_radius=10)
        self.rowsarea.pack(fill="both", expand=True, padx=12, pady=2)

        # Пустое состояние: иконка + короткая подсказка.
        # Привязка к card (не к scroll-фрейму) — place(in_=CTkScrollableFrame)
        # позиционирует ненадёжно.
        self.empty_hint = ctk.CTkLabel(
            card, text=t("empty_files_hint"), justify="center",
            image=icon("drop", 44), compound="top", pady=10,
            font=ctk.CTkFont(size=14), text_color=CLR_MUTED,
            fg_color=CLR_LIST_BG)
        self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

        br = ctk.CTkFrame(card, fg_color="transparent")
        br.pack(fill="x", padx=12, pady=(4, 10))
        btn_ghost(br, text=t("add_files"), width=130, image=icon("plus", 13),
                  compound="left", command=self._add).pack(side="left",
                                                           padx=(0, 6))
        btn_ghost(br, text=t("remove"), width=110,
                  command=self._remove).pack(side="left", padx=(0, 6))
        btn_ghost(br, text=t("clear_list"), width=110,
                  command=self._clear).pack(side="left")
        if extra:
            text, _color, cmd = extra
            btn_ghost(br, text=text, width=160, command=cmd).pack(side="right")

    def _on_sort(self, _choice=None):
        if self._sort_key:
            s = load_settings()
            s[self._sort_key] = self._sort_labels.get(self._sort_var.get(),
                                                      "added")
            save_settings(s)
        self._apply_sort()

    def _apply_sort(self):
        """Пересортировать файлы и перепаковать строки в новом порядке."""
        mode = self._sort_labels.get(self._sort_var.get(), "added") \
            if hasattr(self, "_sort_var") else "added"
        try:
            if mode == "name":
                self.files.sort(key=lambda p: os.path.basename(p).lower())
            elif mode == "size":
                self.files.sort(key=lambda p: self._sizes.get(p, 0),
                                reverse=True)
            else:
                self.files.sort(key=lambda p: self._added_seq.get(p, 0))
            for p in self.files:
                rd = self._rows.get(p)
                if rd:
                    rd["frame"].pack_forget()
                    rd["frame"].pack(fill="x", padx=4, pady=3)
        except Exception as e:
            _log_exc("apply sort", e)

    def _add_file_row(self, path: str):
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        self._sizes[path] = size
        r = ctk.CTkFrame(self.rowsarea, corner_radius=9, fg_color=CLR_ROW,
                         border_width=1, border_color=CLR_ROW)
        r.pack(fill="x", padx=4, pady=3)
        r.grid_columnconfigure(1, weight=1)
        ext = Path(path).suffix.lstrip(".").upper()[:4] or "?"
        icon_lbl = ctk.CTkLabel(r, text=ext, width=56, height=42,
                                corner_radius=6, fg_color=CLR_GHOST,
                                text_color=CLR_MUTED,
                                font=ctk.CTkFont(size=11, weight="bold"))
        icon_lbl.grid(row=0, column=0, rowspan=2, padx=(8, 6), pady=6)
        name = ctk.CTkLabel(r, text=os.path.basename(path), anchor="w",
                            font=ctk.CTkFont(size=14, weight="bold"))
        name.grid(row=0, column=1, sticky="w", padx=(4, 6), pady=(6, 0))
        meta = ctk.CTkLabel(r, text=os.path.dirname(path), anchor="w",
                            font=ctk.CTkFont(size=11), text_color=CLR_MUTED)
        meta.grid(row=1, column=1, sticky="w", padx=(4, 6), pady=(0, 6))
        # фиксированная ширина — размеры выстраиваются в колонку
        right = ctk.CTkLabel(r, text=human_size(size), anchor="e", width=160,
                             font=ctk.CTkFont(size=13), text_color=CLR_MUTED)
        right.grid(row=0, column=2, rowspan=2, sticky="e", padx=6)
        chip = ctk.CTkLabel(r, text="", anchor="center", corner_radius=8,
                            font=ctk.CTkFont(size=12, weight="bold"),
                            fg_color="transparent", text_color=CLR_MUTED,
                            width=112, height=24)
        chip.grid(row=0, column=3, rowspan=2, padx=(4, 10))
        bar = ctk.CTkProgressBar(r, width=112, height=5, progress_color=CLR_ACC)
        bar.set(0)
        self._rows[path] = {"frame": r, "name": name, "meta": meta,
                            "right": right, "chip": chip, "bar": bar,
                            "icon": icon_lbl, "state": "new"}
        for w in (r, icon_lbl, name, meta, right, chip):
            try:
                w.bind("<Button-1>", lambda e, p=path: self._row_click(p))
            except Exception:
                pass
        # миниатюра для изображений — асинхронно, с кэшем
        if self.THUMBS and Path(path).suffix.lower() in IMAGE_EXTS:
            cached = self._thumb_cache.get(path)
            if cached is not None:
                icon_lbl.configure(image=cached, text="")
            else:
                self._thumb_request(path, (56, 42), ("row", path))

    # ---- асинхронные миниатюры ----

    def _thumb_request(self, path: str, size, key):
        if not hasattr(self, "_thumb_q"):
            self._thumb_q = queue.Queue()
            threading.Thread(target=self._thumb_worker, daemon=True).start()
        self._thumb_q.put((path, size, key))

    def _thumb_worker(self):
        while True:
            try:
                item = self._thumb_q.get()
                if item is None:
                    return
                path, size, key = item
                # рендер в 2× от логического размера: на масштабе 150–200%
                # CTkImage показывает миниатюру без мыла
                px2 = (size[0] * 2, size[1] * 2)
                with Image.open(path) as im:
                    im.draft("RGB", (px2[0] * 2, px2[1] * 2))
                    im2 = ImageOps.exif_transpose(im)
                    # точный размер (кадрирование по центру) —
                    # миниатюры образуют ровную колонку
                    pil = ImageOps.fit(im2.convert("RGB"), px2,
                                       Image.Resampling.LANCZOS)
                self.msg_queue.put(("thumb", key, pil, size))
            except Exception as e:
                _log_exc("thumb worker", e)

    def _apply_thumb(self, key, pil, disp):
        """Пришла миниатюра из фонового потока (переопределяется в сканере)."""
        try:
            kind, ident = key
            if kind != "row":
                return
            rd = self._rows.get(ident)
            img = ctk.CTkImage(light_image=pil, dark_image=pil, size=disp)
            self._thumb_cache[ident] = img
            if rd:
                rd["icon"].configure(image=img, text="")
        except Exception as e:
            _log_exc("apply thumb", e)

    def _row_click(self, path: str):
        rd = self._rows.get(path)
        if not rd:
            return
        try:
            if path in self._selected:
                self._selected.discard(path)
                rd["frame"].configure(border_color=CLR_ROW)
            else:
                self._selected.add(path)
                rd["frame"].configure(border_color=CLR_ACC)
        except Exception:
            pass

    def _set_row_state(self, path: str, state: str, data=None):
        rd = self._rows.get(path)
        if not rd:
            return
        try:
            chip, bar = rd["chip"], rd["bar"]
            if state == "queued":
                if rd["state"] in ("done", "fail"):
                    return
                bar.grid_remove()
                chip.grid()
                chip.configure(text=t("queued"), fg_color=CLR_GHOST,
                               text_color=CLR_MUTED)
            elif state == "run":
                chip.grid_remove()
                bar.grid(row=0, column=3, rowspan=2, padx=(4, 10))
                bar.configure(mode="indeterminate")
                bar.start()
            elif state == "prog":
                chip.grid_remove()
                bar.grid(row=0, column=3, rowspan=2, padx=(4, 10))
                try:
                    bar.stop()
                except Exception:
                    pass
                bar.configure(mode="determinate")
                bar.set(max(0.0, min(1.0, float(data or 0))))
            elif state == "done":
                orig, new = data
                try:
                    bar.stop()
                except Exception:
                    pass
                bar.grid_remove()
                chip.grid()
                pct = int(round((1 - new / orig) * 100)) if orig else 0
                rd["right"].configure(
                    text=f"{human_size(orig)} → {human_size(new)}",
                    text_color=CLR_ACC)
                chip.configure(text=f"✓ −{pct}%", fg_color=CLR_OK_SOFT,
                               text_color=CLR_OK)
            elif state == "fail":
                try:
                    bar.stop()
                except Exception:
                    pass
                bar.grid_remove()
                chip.grid()
                msg = tr_msg(str(data or ""))
                if len(msg) > 26:
                    msg = msg[:25] + "…"
                chip.configure(text=f"✗ {msg}", fg_color=CLR_ERR_SOFT,
                               text_color=CLR_ERR)
            rd["state"] = state
        except Exception as e:
            _log_exc("row state", e)

    def _build_bottom(self, start_text: str, color: str = None, hint: str = ""):
        """Панель-сводка: слева итог по файлам, справа одна главная кнопка.
        «Стоп» появляется только во время работы."""
        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(fill="x", padx=16, pady=(4, 12))
        self.progress = ctk.CTkProgressBar(bot, height=7, progress_color=CLR_ACC)
        self.progress.pack(fill="x", pady=(0, 8))
        self.progress.set(0)
        bf = ctk.CTkFrame(bot, fg_color="transparent")
        bf.pack(fill="x")
        bf.grid_columnconfigure(1, weight=1)
        self.summary_lbl = ctk.CTkLabel(bf, text="", text_color=CLR_MUTED,
                                        font=ctk.CTkFont(size=12))
        self.summary_lbl.grid(row=0, column=0, sticky="w", padx=(2, 14))
        self.btn = btn_primary(bf, text=start_text, height=42,
                               image=icon_active("check", 16), compound="left",
                               font=ctk.CTkFont(size=14, weight="bold"),
                               command=self._start)
        self.btn.grid(row=0, column=1, sticky="ew")
        self.btn_stop = btn_ghost(bf, text=t("stop_esc"), width=118, height=42,
                                  text_color=CLR_DANGER_TEXT, command=self._stop)
        self.btn_stop.grid(row=0, column=2, padx=(8, 0))
        self.btn_stop.grid_remove()

    # Свёртка «Ещё настройки»: редкие поля спрятаны, форма дышит
    def _build_more_block(self, parent, summary: str):
        self._more_open = False
        self._more_btn = ctk.CTkButton(
            parent, text=f"▸  {t('more_settings')} — {summary}",
            anchor="w", height=30, fg_color="transparent",
            hover_color=CLR_GHOST, text_color=CLR_MUTED,
            command=self._toggle_more)
        self._more_btn.pack(fill="x", padx=6, pady=(2, 4))
        self._more_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._more_summary = summary
        return self._more_frame

    def _toggle_more(self, open_=None):
        want = (not self._more_open) if open_ is None else bool(open_)
        if want == self._more_open:
            return
        self._more_open = want
        arrow = "▾" if want else "▸"
        self._more_btn.configure(
            text=f"{arrow}  {t('more_settings')} — {self._more_summary}")
        if want:
            self._more_frame.pack(fill="x", after=self._more_btn)
        else:
            self._more_frame.pack_forget()

    def _build_outdir_row(self, parent, setting_key: str):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(row, text=t("out_dir")).pack(side="left")
        self.outdir_var = ctk.StringVar(value=self.settings.get(setting_key, ""))
        ctk.CTkEntry(row, textvariable=self.outdir_var, width=260).pack(
            side="left", padx=6)
        btn_ghost(row, text="…", width=34,
                  command=self._browse_outdir).pack(side="left")
        btn_ghost(row, text="×", width=28,
                  command=lambda: self.outdir_var.set("")).pack(
            side="left", padx=(4, 6))
        ctk.CTkLabel(row, text=t("out_same"),
                     text_color=CLR_MUTED).pack(side="left")

    def _browse_outdir(self):
        d = filedialog.askdirectory(title=t("out_dir"))
        if d:
            self.outdir_var.set(os.path.normpath(d))

    def _build_workers_menu(self, parent, setting_key: str):
        ctk.CTkLabel(parent, text=t("workers")).pack(side="left", padx=(12, 4))
        cur = str(self.settings.get(setting_key, "auto"))
        label = t("auto") if cur == "auto" else cur
        self.workers_var = ctk.StringVar(value=label)
        ctk.CTkOptionMenu(parent, variable=self.workers_var,
                          values=[t("auto"), "2", "4", "6", "8", "12"],
                          width=80, fg_color=CLR_GHOST,
                          button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_GHOST_TEXT).pack(side="left")

    def _workers_setting(self) -> str:
        v = self.workers_var.get() if hasattr(self, "workers_var") else "auto"
        return "auto" if v == t("auto") else v

    # ---- файлы ----

    def _recurse(self) -> bool:
        v = getattr(self, "recurse_var", None)
        try:
            return bool(v.get()) if v else False
        except Exception:
            return False

    def _collect_paths(self, p) -> List[str]:
        return collect_files(p, self.EXTS, self._recurse())

    def add_paths(self, paths: List[str]):
        added = 0
        for p in paths:
            for f in self._collect_paths(p):
                f = os.path.normpath(f)
                if f not in self.files:
                    self.files.append(f)
                    self._seq_counter += 1
                    self._added_seq[f] = self._seq_counter
                    self._add_file_row(f)
                    added += 1
        if added:
            self._apply_sort()
            self._update_count()
            self._log(f"+ {added}")

    def _add(self):
        init = load_settings().get("last_dir") or ""
        paths = filedialog.askopenfilenames(initialdir=init or None,
                                            filetypes=self.FILETYPES)
        if paths:
            s = load_settings()
            s["last_dir"] = os.path.dirname(paths[0])
            save_settings(s)
            self.add_paths(list(paths))

    def _remove(self):
        targets = list(self._selected)
        if not targets:
            return
        for p in targets:
            rd = self._rows.pop(p, None)
            if rd:
                try:
                    rd["frame"].destroy()
                except Exception:
                    pass
            self._sizes.pop(p, None)
            self._added_seq.pop(p, None)
            self._selected.discard(p)
            if p in self.files:
                self.files.remove(p)
        self._update_count()

    def _clear(self):
        for rd in self._rows.values():
            try:
                rd["frame"].destroy()
            except Exception:
                pass
        self._rows.clear()
        self._selected.clear()
        self._sizes.clear()
        self._added_seq.clear()
        self.files.clear()
        self._update_count()

    def _outdir(self) -> str:
        v = getattr(self, "outdir_var", None)
        try:
            d = (v.get() or "").strip() if v else ""
        except Exception:
            d = ""
        return os.path.normpath(d) if d else ""

    def _preflight_files(self, snapshot: list) -> list:
        """Фильтр мёртвых файлов + проверка папки записи. Пусто = отмена."""
        ok, skipped = filter_existing_files(snapshot)
        for msg in skipped[:15]:
            self._log(f"⚠ {msg}")
        if skipped and len(skipped) > 15:
            self._log(f"⚠ … +{len(skipped) - 15}")
        if not ok:
            self.show_toast(t("no_avail_files"), "warn")
            return []
        folder = self._outdir() or os.path.dirname(ok[0])
        writable, err = ensure_writable_dir(folder)
        if not writable:
            self.show_toast(err or "Cannot write", "err")
            return []
        probe = os.path.join(folder, "probe")
        avg = sum(os.path.getsize(p) for p in ok[:20]) // max(1, min(20, len(ok)))
        if not ensure_disk_space(probe, avg + 10 * 1024 * 1024):
            self.show_toast(t("disk_low"), "err")
            return []
        return ok

    # ---- пакетный движок (общий для PDF/фото/видео) ----

    def _launch_batch(self, *, ext_fn, pool_fn, pool_args_fn, serial_fn,
                      workers: int, size_div: int, mode: str, single_fn=None):
        """Подготовка и запуск фонового пакета. Вызывать из _start."""
        save_settings(self._collect())
        self.settings = load_settings()
        stop_flag.clear()
        snapshot = self._preflight_files(list(self.files))
        if not snapshot:
            return
        outdir = self._outdir()
        if outdir:
            self._log("ℹ " + t("outdir_note"))
        self.is_running = True
        if self.btn:
            self.btn.configure(state="disabled")
        if self.btn_stop:
            try:
                self.btn_stop.grid()
            except Exception:
                pass
        self.progress.set(0)
        self._log(f"——— {mode} ———")
        threading.Thread(
            target=self._batch_worker,
            args=(snapshot, ext_fn, pool_fn, pool_args_fn, serial_fn,
                  workers, size_div, mode, single_fn, outdir),
            daemon=True).start()

    @staticmethod
    def _real_out(out: str) -> str:
        """Фактическое имя результата (контейнер/формат могли поменять ext)."""
        if os.path.isfile(out):
            return out
        base = os.path.splitext(out)[0]
        for ext in (".mp4", ".webm", ".mkv", ".avi", ".jpg", ".webp", ".png", ".pdf"):
            if os.path.isfile(base + ext):
                return base + ext
        return out

    def _batch_worker(self, snapshot, ext_fn, pool_fn, pool_args_fn, serial_fn,
                      workers, size_div, mode, single_fn, outdir):
        q = self.msg_queue
        total = len(snapshot)
        success = saved = moved = processed = 0
        ok_names: List[str] = []
        custom_out = bool(outdir)
        folder = outdir or os.path.dirname(snapshot[0])
        move = bool(self.move_var.get()) and not custom_out
        template = self.rename_var.get().strip() if hasattr(self, "rename_var") else ""

        tasks = []
        planned = set()  # защита от коллизий имён (одноимённые файлы из
        for inp in snapshot:  # разных папок при общей папке результата)
            d = outdir or os.path.dirname(inp)
            newname = apply_rename(template, inp)
            out = os.path.join(d, newname + ext_fn(inp))
            # tmp-суффикс — только при записи рядом с исходником: там его
            # снимает finalize_output. При custom-папке совпадение с
            # исходником разруливает unique_out_path (файл существует → _2).
            if not custom_out and os.path.normcase(out) == os.path.normcase(inp):
                base, ex = os.path.splitext(out)
                out = base + ".__nexus_tmp__" + ex
            out = unique_out_path(out)
            while os.path.normcase(out) in planned:
                base, ex = os.path.splitext(out)
                out = unique_out_path(f"{base}_2{ex}")
            planned.add(os.path.normcase(out))
            tasks.append((inp, out))

        q.put(("file_all", list(snapshot)))

        def handle(res, out_hint):
            nonlocal success, saved, moved, processed
            inp, ok, msg, orig, new = res
            name = os.path.basename(inp)
            processed += 1
            if ok:
                success += 1
                ok_names.append(name)
                saved += max(0, orig - new)
                pct = (1 - new / orig) * 100 if orig else 0
                q.put(("file", inp, "done", (orig, new)))
                q.put(("log", f"✓ {name}  {human_size(orig)}→{human_size(new)} (−{pct:.0f}%)"))
                if not custom_out and finalize_output(inp, out_hint, move):
                    moved += 1
            else:
                q.put(("file", inp, "fail", msg))
                q.put(("log", f"✗ {name} — {tr_msg(msg)}"))
            q.put(("run_summary", processed, total, saved))

        try:
            if single_fn and total == 1:
                inp, out = tasks[0]
                q.put(("file", inp, "run", None))

                def put_prog(frac, text):
                    q.put(("progress", min(0.99, max(0.0, frac)), text))
                    q.put(("file", inp, "prog", frac))
                res, out_hint = single_fn(inp, out, put_prog)
                handle(res, self._real_out(out_hint))
                q.put(("progress", 1.0, "1/1"))
            elif workers > 1 and total > 1:
                q.put(("log", f"{t('run_parallel')}{min(workers, total)}"))
                with get_executor(max_workers=min(workers, total)) as ex:
                    if is_frozen():
                        # В .exe параллелим потоками → можно вешать живой
                        # статус и процент на каждую строку.
                        def make_job(_i, _o):
                            def job():
                                q.put(("file", _i, "run", None))
                                return serial_fn(
                                    _i, _o,
                                    lambda f, txt, _p=_i:
                                        q.put(("file", _p, "prog", f)))
                            return job
                        fmap = {ex.submit(make_job(i, o)): (i, o)
                                for i, o in tasks}
                    else:
                        # dev-режим: процессы, события недоступны —
                        # строки идут «в очереди» → результат
                        fmap = {ex.submit(pool_fn, *pool_args_fn(i, o)): (i, o)
                                for i, o in tasks}
                    self._futures = list(fmap.keys())
                    done = 0
                    for fut in as_completed(fmap):
                        if stop_flag.is_set():
                            break
                        done += 1
                        i, o = fmap[fut]
                        try:
                            res = fut.result()
                        except Exception as e:
                            res = (i, False, str(e)[:80], 0, 0)
                        handle(res, self._real_out(o))
                        q.put(("progress", done / total, f"{done}/{total}"))
            else:
                for idx, (inp, out) in enumerate(tasks):
                    if stop_flag.is_set():
                        break
                    q.put(("file", inp, "run", None))

                    def put_prog(frac, text, _i=idx, _p=inp):
                        q.put(("progress", min(0.99, (_i + frac) / total), text))
                        q.put(("file", _p, "prog", frac))
                    res = serial_fn(inp, out, put_prog)
                    handle(res, self._real_out(out))
                    q.put(("progress", (idx + 1) / total, f"{idx + 1}/{total}"))

            add_history({
                "time": datetime.now().strftime("%d.%m %H:%M"),
                "mode": mode,
                "count": success,
                "saved_mb": round(saved / 1048576, 2),
                "folder": folder,
                "names": ok_names,
            })
            cleanup_batch_temp()
            if stop_flag.is_set():
                q.put(("stopped", None))
            else:
                q.put(("done", {"ok": success, "total": total, "saved": saved,
                                "moved": moved, "folder": folder}))
        except Exception as e:
            _log_exc("batch worker", e)
            q.put(("error", str(e)[:300]))

    # ---- переопределяемые ----

    def _build(self):
        raise NotImplementedError

    def _apply(self):
        pass

    def _collect(self):
        return None

    def _start(self):
        pass

class PdfSection(BaseSection):
    EXTS = {".pdf"}
    EMPTY_KEY = "add_pdfs"
    FILETYPES = [("PDF", "*.pdf")]
    MODE = "PDF"

    def __init__(self, app):
        super().__init__(app, "pdf")

    def _build(self):
        self._build_header(t("pdf"))
        self._build_filecard(t("pdf_files"),
                             extra=(t("merge_pdf"), None, self._merge_pdfs),
                             sort_key="pdf_sort")

        st = ctk.CTkFrame(self, corner_radius=12)
        st.pack(fill="x", padx=16, pady=4)
        r1 = ctk.CTkFrame(st, fg_color="transparent")
        r1.pack(fill="x", padx=10, pady=(10, 4))
        self.preset_map = {
            t("smart_pdf"): "smart",
            t("aggressive_scans"): "aggressive",
            t("max_compression"): "max",
            t("balance"): "balanced",
            t("high_quality"): "quality",
            t("structure_only"): "structure",
            t("images_only"): "images",
            t("custom_params"): "custom",
        }
        ctk.CTkLabel(r1, text=t("preset"),
                     font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 8))
        self.preset_var = ctk.StringVar(value=t("smart_pdf"))
        ctk.CTkOptionMenu(r1, variable=self.preset_var,
                          values=list(self.preset_map.keys()),
                          width=210, fg_color=CLR_GHOST,
                          button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_GHOST_TEXT,
                          command=self._preset_change).pack(side="left", padx=(0, 10))
        self.custom = ctk.CTkFrame(r1, fg_color="transparent")
        ctk.CTkLabel(self.custom, text="DPI").pack(side="left")
        self.dpi_var = ctk.IntVar(value=130)
        s1 = ctk.CTkSlider(self.custom, from_=50, to=220, variable=self.dpi_var,
                           width=90, progress_color=CLR_ACC, button_color=CLR_ACC,
                           button_hover_color=CLR_ACC_HOVER)
        s1.pack(side="left", padx=3)
        self.dpi_lbl = ctk.CTkLabel(self.custom, text="130", width=26)
        self.dpi_lbl.pack(side="left")
        s1.configure(command=lambda v: self.dpi_lbl.configure(text=str(int(float(v)))))
        ctk.CTkLabel(self.custom, text=t("quality_short")).pack(side="left", padx=(6, 0))
        self.q_var = ctk.IntVar(value=55)
        s2 = ctk.CTkSlider(self.custom, from_=25, to=85, variable=self.q_var,
                           width=80, progress_color=CLR_ACC, button_color=CLR_ACC,
                           button_hover_color=CLR_ACC_HOVER)
        s2.pack(side="left", padx=3)
        self.q_lbl = ctk.CTkLabel(self.custom, text="55", width=26)
        self.q_lbl.pack(side="left")
        s2.configure(command=lambda v: self.q_lbl.configure(text=str(int(float(v)))))
        self.gray_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(r1, text=t("gray_scan_pdf"), variable=self.gray_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="right")

        more = self._build_more_block(st, t("more_pdf"))
        m1 = ctk.CTkFrame(more, fg_color="transparent")
        m1.pack(fill="x", padx=10, pady=2)
        self.multi_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(m1, text=t("all_cores"), variable=self.multi_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left", padx=(0, 12))
        self.move_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(m1, text=t("save_original"), variable=self.move_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left", padx=(0, 12))
        self.recurse_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(m1, text=t("recurse"), variable=self.recurse_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left")
        self._build_workers_menu(m1, "pdf_workers")

        m2 = ctk.CTkFrame(more, fg_color="transparent")
        m2.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(m2, text=t("filename_template")).pack(side="left")
        self.rename_var = ctk.StringVar(value="{name}_nexus")
        ctk.CTkEntry(m2, textvariable=self.rename_var, width=160).pack(
            side="left", padx=6)
        ctk.CTkLabel(m2, text=t("name_hint"), text_color=CLR_MUTED).pack(side="left")

        self._build_outdir_row(more, "pdf_outdir")
        ctk.CTkFrame(st, fg_color="transparent", height=4).pack()

        self._build_bottom(t("compress_selected_pdf"))

    def _preset_change(self, choice: str):
        if self.preset_map.get(choice) == "custom":
            self.custom.pack(side="left")
        else:
            self.custom.pack_forget()
        self._update_summary()

    def _apply(self):
        s = self.settings
        key = s.get("pdf_preset_key") or "smart"
        label = next((lb for lb, k in self.preset_map.items() if k == key), None)
        self.preset_var.set(label or t("smart_pdf"))
        self.dpi_var.set(safe_int(s.get("pdf_dpi"), 130, 50, 300))
        self.dpi_lbl.configure(text=str(self.dpi_var.get()))
        self.q_var.set(safe_int(s.get("pdf_quality"), 55, 20, 95))
        self.q_lbl.configure(text=str(self.q_var.get()))
        self.gray_var.set(bool(s.get("pdf_gray", False)))
        self.multi_var.set(bool(s.get("pdf_multi", True)))
        self.move_var.set(bool(s.get("pdf_move", True)))
        self.recurse_var.set(bool(s.get("pdf_recurse", False)))
        self.rename_var.set(s.get("rename_template", "{name}_nexus"))
        self._preset_change(self.preset_var.get())

    def _collect(self) -> dict:
        s = load_settings()
        s.update({
            "pdf_preset_key": self.preset_map.get(self.preset_var.get(), "smart"),
            "pdf_dpi": self.dpi_var.get(),
            "pdf_quality": self.q_var.get(),
            "pdf_gray": self.gray_var.get(),
            "pdf_multi": self.multi_var.get(),
            "pdf_move": self.move_var.get(),
            "pdf_recurse": self.recurse_var.get(),
            "pdf_outdir": self._outdir(),
            "pdf_workers": self._workers_setting(),
            "rename_template": self.rename_var.get(),
            "theme": ctk.get_appearance_mode(),
        })
        return s

    def _merge_pdfs(self):
        if self.is_running:
            self.show_toast(t("waiting"), "warn")
            return
        if not self.files:
            self.show_toast(t("no_files"), "warn")
            return
        out = filedialog.asksaveasfilename(defaultextension=".pdf",
                                           filetypes=[("PDF", "*.pdf")],
                                           title=t("merge_pdf"))
        if not out:
            return
        self.is_running = True
        if self.btn:
            self.btn.configure(state="disabled")
        files = list(self.files)
        q = self.msg_queue
        self._log(t("merge_pdf") + "…")

        def work():
            try:
                ok, msg, added, skipped = merge_pdfs(
                    files, out,
                    progress_cb=lambda i, n: q.put(
                        ("progress", i / max(1, n), f"{i}/{n}")))
                if ok:
                    add_history({
                        "time": datetime.now().strftime("%d.%m %H:%M"),
                        "mode": "PDF merge",
                        "count": added,
                        "saved_mb": 0,
                        "folder": os.path.dirname(out),
                        "names": [os.path.basename(out)] +
                                 [os.path.basename(x) for x in files[:4]],
                    })
                    text = f"{t('pdf_created')} {os.path.basename(out)}"
                    if skipped:
                        text += f"  ·  ✗ {t('skipped_n').format(n=skipped)}"
                    q.put(("toast", text, "warn" if skipped else "ok",
                           os.path.dirname(out)))
                else:
                    q.put(("toast", tr_msg(msg), "err", None))
            finally:
                q.put(("op_end", None))

        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        if self.is_running:
            self._log(t("waiting"))
            return
        if not self.files:
            self.show_toast(t(self.EMPTY_KEY), "warn")
            return
        preset = self.preset_map.get(self.preset_var.get(), "smart")
        dpi = self.dpi_var.get()
        quality = self.q_var.get()
        gray = self.gray_var.get()
        multi = self.multi_var.get()
        workers = effective_workers(self._workers_setting(), 8,
                                    len(self.files)) if multi else 1
        pages_workers = effective_workers(self._workers_setting(), 8, 8) if multi else 1

        def single(inp, out, put_prog):
            self.msg_queue.put(("log", f"{os.path.basename(inp)} — {t('single_pdf_note')}"))
            res = process_single_pdf_parallel(
                inp, out, preset, dpi, quality, gray, pages_workers,
                progress_cb=lambda d, tot, txt: put_prog(
                    (d / tot) if tot else 0, txt))
            return res, out

        def serial(i, o, pcb):
            cb = (lambda f: pcb(f, f"{int(f * 100)}%")) if pcb else None
            return process_pdf(i, o, preset, dpi, quality, gray,
                               progress_cb=cb)

        self._launch_batch(
            ext_fn=lambda inp: ".pdf",
            pool_fn=process_pdf,
            pool_args_fn=lambda i, o: (i, o, preset, dpi, quality, gray),
            serial_fn=serial,
            workers=workers,
            size_div=1024,
            mode="PDF",
            single_fn=single,
        )

    def _est_ratio(self):
        key = self.preset_map.get(self.preset_var.get(), "smart")
        return {"smart": 0.4, "aggressive": 0.25, "max": 0.22,
                "balanced": 0.45, "quality": 0.6, "structure": 0.85,
                "images": 0.35, "custom": 0.4}.get(key)


class ImageSection(BaseSection):
    EXTS = IMAGE_EXTS
    EMPTY_KEY = "add_photos"
    MODE = "Photo"
    THUMBS = True

    def __init__(self, app):
        pattern = " ".join("*" + e for e in sorted(IMAGE_EXTS))
        self.FILETYPES = [("Images", pattern), ("All", "*.*")]
        super().__init__(app, "photo")

    def _build(self):
        self._build_header(t("photo"))
        self._build_filecard(t("photo_files"),
                             extra=(t("merge_pdf"), None, self._to_pdf),
                             sort_key="img_sort")

        st = ctk.CTkFrame(self, corner_radius=12)
        st.pack(fill="x", padx=16, pady=4)

        r0 = ctk.CTkFrame(st, fg_color="transparent")
        r0.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(r0, text=t("ready_profile"),
                     font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 8))
        self.img_presets = {t(k): (k, ms, q) for k, ms, q in IMG_PRESETS}
        self.img_presets[t("custom_params")] = ("custom", -1, -1)
        self.img_preset_var = ctk.StringVar(value=t("imgp_bal"))
        ctk.CTkOptionMenu(r0, variable=self.img_preset_var,
                          values=list(self.img_presets.keys()), width=270,
                          fg_color=CLR_GHOST, button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC, text_color=CLR_GHOST_TEXT,
                          command=self._on_img_preset).pack(side="left")
        self.smart_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r0, text=t("smart_photo"), variable=self.smart_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="right")

        more = self._build_more_block(st, t("more_img"))

        m1 = ctk.CTkFrame(more, fg_color="transparent")
        m1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(m1, text=t("photo_quality")).pack(side="left")
        self.q_var = ctk.IntVar(value=75)
        self.q_slider = ctk.CTkSlider(m1, from_=40, to=92, variable=self.q_var,
                                      width=140, progress_color=CLR_ACC,
                                      button_color=CLR_ACC,
                                      button_hover_color=CLR_ACC_HOVER)
        self.q_slider.pack(side="left", padx=6)
        self.q_lbl = ctk.CTkLabel(m1, text="75", width=28)
        self.q_lbl.pack(side="left")
        self.q_slider.configure(
            command=lambda v: self.q_lbl.configure(text=str(int(float(v)))))
        ctk.CTkLabel(m1, text=t("format")).pack(side="left", padx=(12, 4))
        self.fmt_var = ctk.StringVar(value="JPEG")
        ctk.CTkOptionMenu(m1, variable=self.fmt_var,
                          values=["JPEG", "WEBP", "PNG"], width=90,
                          fg_color=CLR_GHOST, button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_GHOST_TEXT).pack(side="left")
        ctk.CTkLabel(m1, text=t("max_side")).pack(side="left", padx=(12, 4))
        self.max_var = ctk.StringVar(value="1200")
        ctk.CTkEntry(m1, textvariable=self.max_var, width=70).pack(side="left", padx=4)
        ctk.CTkLabel(m1, text=t("no_resize"),
                     text_color=CLR_MUTED).pack(side="left", padx=(4, 0))

        m2 = ctk.CTkFrame(more, fg_color="transparent")
        m2.pack(fill="x", padx=10, pady=2)
        self.multi_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(m2, text=t("all_cores"), variable=self.multi_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left", padx=(0, 12))
        self.move_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(m2, text=t("save_original"), variable=self.move_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left", padx=(0, 12))
        self.recurse_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(m2, text=t("recurse"), variable=self.recurse_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left")
        self._build_workers_menu(m2, "img_workers")

        m3 = ctk.CTkFrame(more, fg_color="transparent")
        m3.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(m3, text=t("filename_template")).pack(side="left")
        self.rename_var = ctk.StringVar(value="{name}_nexus")
        ctk.CTkEntry(m3, textvariable=self.rename_var, width=160).pack(
            side="left", padx=6)
        ctk.CTkLabel(m3, text=t("name_hint"), text_color=CLR_MUTED).pack(side="left")

        self._build_outdir_row(more, "img_outdir")
        ctk.CTkFrame(st, fg_color="transparent", height=4).pack()

        self._build_bottom(t("compress_selected_photo"))

        # честный пресет: правка полей вручную → профиль «Свои»
        self._applying = True
        self.q_var.trace_add("write", self._mark_custom)
        self.max_var.trace_add("write", self._mark_custom)
        if HAS_HEIF:
            self._set_badge("HEIC / HEIF")

    def _est_ratio(self):
        q = safe_int(self.q_var.get(), 75, 20, 100)
        ms = safe_int(self.max_var.get(), 0, 0, 20000)
        r = 0.12 + q / 250.0          # качество → базовая доля
        if ms and ms <= 1600:
            r *= 0.55                  # даунскейл сильно уменьшает
        return max(0.06, min(0.9, r))

    def _mark_custom(self, *_):
        self._update_summary()
        if getattr(self, "_applying", False):
            return
        val = self.img_presets.get(self.img_preset_var.get())
        if not val or val[0] == "custom":
            return
        try:
            q = int(self.q_var.get())
        except Exception:
            return
        ms = safe_int(self.max_var.get(), -1)
        if val[1] == ms and val[2] == q:
            return
        self.img_preset_var.set(t("custom_params"))

    def _on_img_preset(self, choice: str):
        val = self.img_presets.get(choice)
        if not val or val[1] == -1:
            self._toggle_more(True)  # «Свои» — раскрыть настройки
            return
        _key, max_side, quality = val
        self._applying = True
        try:
            self.max_var.set(str(max_side))
            self.q_var.set(quality)
            self.q_lbl.configure(text=str(quality))
        finally:
            self._applying = False

    def _apply(self):
        s = self.settings
        self.q_var.set(safe_int(s.get("img_quality"), 78, 20, 100))
        self.q_lbl.configure(text=str(self.q_var.get()))
        self.max_var.set(str(safe_int(s.get("img_max_side"), 0, 0, 20000)))
        self.fmt_var.set(s.get("img_format", "JPEG"))
        self.multi_var.set(bool(s.get("img_multi", True)))
        self.move_var.set(bool(s.get("img_move", True)))
        self.smart_var.set(bool(s.get("img_smart", True)))
        self.recurse_var.set(bool(s.get("img_recurse", False)))
        self.rename_var.set(s.get("img_rename") or s.get("rename_template", "{name}_nexus"))
        key = s.get("img_preset_key") or ""
        label = next((lb for lb, v in self.img_presets.items() if v[0] == key), None)
        if label:
            self.img_preset_var.set(label)
        else:
            # подбор по (max_side, quality); иначе — «Свои параметры»
            q, ms = self.q_var.get(), safe_int(self.max_var.get(), 0)
            matched = next((lb for lb, v in self.img_presets.items()
                            if v[1] == ms and v[2] == q), None)
            self.img_preset_var.set(matched or t("custom_params"))
        self._applying = False

    def _collect(self) -> dict:
        s = load_settings()
        val = self.img_presets.get(self.img_preset_var.get())
        s.update({
            "img_preset_key": val[0] if val else "",
            "img_quality": self.q_var.get(),
            "img_max_side": safe_int(self.max_var.get(), 0, 0, 20000),
            "img_format": self.fmt_var.get(),
            "img_multi": self.multi_var.get(),
            "img_move": self.move_var.get(),
            "img_smart": self.smart_var.get(),
            "img_recurse": self.recurse_var.get(),
            "img_outdir": self._outdir(),
            "img_workers": self._workers_setting(),
            "img_rename": self.rename_var.get(),
            "rename_template": self.rename_var.get(),
            "theme": ctk.get_appearance_mode(),
        })
        return s

    def _to_pdf(self):
        if self.is_running:
            self.show_toast(t("waiting"), "warn")
            return
        if not self.files:
            self.show_toast(t("no_files"), "warn")
            return
        out = filedialog.asksaveasfilename(defaultextension=".pdf",
                                           filetypes=[("PDF", "*.pdf")])
        if not out:
            return
        self.is_running = True
        if self.btn:
            self.btn.configure(state="disabled")
        files = list(self.files)
        q = self.msg_queue
        self._log("→ PDF…")

        def work():
            try:
                ok, msg, added, skipped = images_to_pdf(
                    files, out,
                    progress_cb=lambda i, n: q.put(
                        ("progress", i / max(1, n), f"{i}/{n}")))
                if ok:
                    add_history({
                        "time": datetime.now().strftime("%d.%m %H:%M"),
                        "mode": "IMG→PDF",
                        "count": added,
                        "saved_mb": 0,
                        "folder": os.path.dirname(out),
                    })
                    text = f"{t('pdf_created')} {os.path.basename(out)}"
                    if skipped:
                        text += f"  ·  ✗ {t('skipped_n').format(n=skipped)}"
                    q.put(("toast", text, "warn" if skipped else "ok",
                           os.path.dirname(out)))
                else:
                    q.put(("toast", tr_msg(msg), "err", None))
            finally:
                q.put(("op_end", None))

        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        if self.is_running:
            self._log(t("waiting"))
            return
        if not self.files:
            self.show_toast(t(self.EMPTY_KEY), "warn")
            return
        quality = self.q_var.get()
        max_side = safe_int(self.max_var.get(), 0, 0, 20000)
        fmt = self.fmt_var.get()
        smart = self.smart_var.get()
        multi = self.multi_var.get()
        workers = effective_workers(self._workers_setting(), 12,
                                    len(self.files)) if multi else 1
        ext = ".jpg" if fmt.upper() == "JPEG" else f".{fmt.lower()}"

        self._launch_batch(
            ext_fn=lambda inp: ext,
            pool_fn=process_image,
            pool_args_fn=lambda i, o: (i, o, quality, max_side, fmt, smart),
            serial_fn=lambda i, o, pcb: process_image(i, o, quality, max_side,
                                                      fmt, smart),
            workers=workers,
            size_div=1024,
            mode="Photo",
        )


class VideoSection(BaseSection):
    EXTS = VIDEO_EXTS
    EMPTY_KEY = "add_videos"
    MODE = "Video"

    def __init__(self, app):
        pattern = " ".join("*" + e for e in sorted(VIDEO_EXTS))
        self.FILETYPES = [("Video", pattern), ("All", "*.*")]
        super().__init__(app, "video")

    def _build(self):
        self._build_header(t("video"))
        self._build_filecard(t("video_files"), sort_key="vid_sort")

        st = ctk.CTkFrame(self, corner_radius=12)
        st.pack(fill="x", padx=16, pady=4)

        r0 = ctk.CTkFrame(st, fg_color="transparent")
        r0.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(r0, text=t("ready_profile"),
                     font=ctk.CTkFont(weight="bold")).pack(side="left", padx=(0, 8))
        self.vid_presets = {t(k): (k, codec, crf, preset, h, a)
                            for k, codec, crf, preset, h, a in VID_PRESETS}
        self.vid_presets[t("custom_params")] = ("custom", None, None, None, None, None)
        self.vid_preset_var = ctk.StringVar(value=t("vidp_work"))
        ctk.CTkOptionMenu(r0, variable=self.vid_preset_var,
                          values=list(self.vid_presets.keys()), width=300,
                          fg_color=CLR_GHOST, button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC, text_color=CLR_GHOST_TEXT,
                          command=self._on_vid_preset).pack(side="left")
        self.hw_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(r0, text=t("hw_accel"), variable=self.hw_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="right")

        more = self._build_more_block(st, t("more_vid"))

        m1 = ctk.CTkFrame(more, fg_color="transparent")
        m1.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(m1, text=t("codec")).pack(side="left")
        self.codec_var = ctk.StringVar(value="H.264")
        ctk.CTkOptionMenu(m1, variable=self.codec_var,
                          values=["H.264", "H.265", "VP9", "AV1"], width=100,
                          fg_color=CLR_GHOST, button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_GHOST_TEXT).pack(side="left", padx=6)
        ctk.CTkLabel(m1, text=t("container")).pack(side="left", padx=(10, 4))
        self.container_var = ctk.StringVar(value="MP4")
        ctk.CTkOptionMenu(m1, variable=self.container_var,
                          values=["MP4", "MKV", "WebM", "AVI"], width=90,
                          fg_color=CLR_GHOST, button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_GHOST_TEXT).pack(side="left", padx=4)
        ctk.CTkLabel(m1, text="CRF").pack(side="left", padx=(10, 4))
        self.crf_var = ctk.IntVar(value=24)
        self.crf_slider = ctk.CTkSlider(m1, from_=16, to=34, variable=self.crf_var,
                                        width=120, progress_color=CLR_ACC,
                                        button_color=CLR_ACC,
                                        button_hover_color=CLR_ACC_HOVER)
        self.crf_slider.pack(side="left", padx=4)
        self.crf_lbl = ctk.CTkLabel(m1, text="24", width=28)
        self.crf_lbl.pack(side="left")
        self.crf_slider.configure(
            command=lambda v: self.crf_lbl.configure(text=str(int(float(v)))))

        m2 = ctk.CTkFrame(more, fg_color="transparent")
        m2.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(m2, text=t("encode_speed")).pack(side="left")
        self.preset_var = ctk.StringVar(value="medium")
        ctk.CTkOptionMenu(m2, variable=self.preset_var,
                          values=["ultrafast", "fast", "medium", "slow", "veryslow"],
                          width=110, fg_color=CLR_GHOST,
                          button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC,
                          text_color=CLR_GHOST_TEXT).pack(side="left", padx=6)
        ctk.CTkLabel(m2, text=t("max_height")).pack(side="left", padx=(12, 4))
        self.height_var = ctk.StringVar(value="0")
        ctk.CTkEntry(m2, textvariable=self.height_var, width=70).pack(side="left")
        ctk.CTkLabel(m2, text=t("audio_kbps")).pack(side="left", padx=(12, 4))
        self.audio_var = ctk.StringVar(value="128")
        ctk.CTkEntry(m2, textvariable=self.audio_var, width=70).pack(side="left")
        ctk.CTkLabel(m2, text=t("video_hint"), text_color=CLR_MUTED).pack(
            side="left", padx=(12, 0))

        m3 = ctk.CTkFrame(more, fg_color="transparent")
        m3.pack(fill="x", padx=10, pady=2)
        self.multi_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(m3, text=t("all_cores"), variable=self.multi_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left", padx=(0, 12))
        self.move_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(m3, text=t("save_original"), variable=self.move_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left", padx=(0, 12))
        self.recurse_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(m3, text=t("recurse"), variable=self.recurse_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            side="left")
        ctk.CTkLabel(m3, text=t("filename_template")).pack(side="left", padx=(14, 4))
        self.rename_var = ctk.StringVar(value="{name}_nexus")
        ctk.CTkEntry(m3, textvariable=self.rename_var, width=160).pack(side="left")

        self._build_outdir_row(more, "vid_outdir")
        ctk.CTkFrame(st, fg_color="transparent", height=4).pack()

        self._build_bottom(t("compress_selected_video"))
        threading.Thread(target=self._detect_ffmpeg, daemon=True).start()

        # честный пресет: правка полей вручную → профиль «Свои»
        self._applying = True
        for v in (self.codec_var, self.crf_var, self.preset_var,
                  self.height_var, self.audio_var):
            v.trace_add("write", self._mark_custom)

    def _mark_custom(self, *_):
        self._update_summary()
        if getattr(self, "_applying", False):
            return
        val = self.vid_presets.get(self.vid_preset_var.get())
        if not val or val[1] is None:
            return
        _k, codec, crf, preset, height, audio = val
        try:
            cur = (self.codec_var.get(), int(self.crf_var.get()),
                   self.preset_var.get(),
                   safe_int(self.height_var.get(), -1),
                   safe_int(self.audio_var.get(), -1))
        except Exception:
            return
        if (codec, crf, preset, height, audio) == cur:
            return
        self.vid_preset_var.set(t("custom_params"))

    _HW_NAMES = {"h264_qsv": "Intel QSV", "h264_nvenc": "NVIDIA NVENC",
                 "h264_amf": "AMD AMF", "h264_mf": "Windows MF"}

    def _detect_ffmpeg(self):
        ff = find_ffmpeg()
        if not ff:
            self.msg_queue.put(("log", "⚠ " + t("ffmpeg_no")))
            return
        self.msg_queue.put(("log", t("ffmpeg_ok") + f" — {os.path.basename(ff)}"))
        hw = hw_encoders_available()
        if hw:
            friendly = self._HW_NAMES.get(hw[0], hw[0])
            self.msg_queue.put(("badge", f"⚡ GPU: {friendly}"))
            self.msg_queue.put(("log", "⚡ " + t("hw_found") + ", ".join(hw)))
        else:
            self.msg_queue.put(("log", t("hw_none")))

    def _est_ratio(self):
        crf = safe_int(self.crf_var.get(), 24, 14, 40)
        pts = [(18, 0.75), (22, 0.55), (24, 0.45), (26, 0.35),
               (28, 0.28), (30, 0.22), (34, 0.15)]
        if crf <= pts[0][0]:
            return pts[0][1]
        if crf >= pts[-1][0]:
            return pts[-1][1]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if x1 <= crf <= x2:
                return y1 + (y2 - y1) * (crf - x1) / (x2 - x1)
        return 0.4

    def _on_vid_preset(self, choice: str):
        val = self.vid_presets.get(choice)
        if not val or val[1] is None:
            self._toggle_more(True)  # «Свои» — раскрыть настройки
            return
        _key, codec, crf, preset, height, audio = val
        self._applying = True
        try:
            self.codec_var.set(codec)
            self.crf_var.set(crf)
            self.crf_lbl.configure(text=str(crf))
            self.preset_var.set(preset)
            self.height_var.set(str(height))
            self.audio_var.set(str(audio))
        finally:
            self._applying = False

    def _apply(self):
        s = self.settings
        self.codec_var.set(s.get("vid_codec", "H.264"))
        self.container_var.set(s.get("vid_container", "MP4"))
        self.crf_var.set(safe_int(s.get("vid_crf"), 24, 14, 40))
        self.crf_lbl.configure(text=str(self.crf_var.get()))
        self.preset_var.set(s.get("vid_preset", "medium"))
        self.height_var.set(str(safe_int(s.get("vid_height"), 0, 0, 8700)))
        self.audio_var.set(str(safe_int(s.get("vid_audio"), 128, 0, 320)))
        self.multi_var.set(bool(s.get("vid_multi", True)))
        self.move_var.set(bool(s.get("vid_move", True)))
        self.hw_var.set(bool(s.get("vid_hw", True)))
        self.recurse_var.set(bool(s.get("vid_recurse", False)))
        self.rename_var.set(s.get("vid_rename", "{name}_nexus"))
        key = s.get("vid_profile_key") or ""
        label = next((lb for lb, v in self.vid_presets.items() if v[0] == key), None)
        if label:
            self.vid_preset_var.set(label)
        else:
            matched = next((lb for lb, v in self.vid_presets.items()
                            if v[1] == self.codec_var.get()
                            and v[2] == self.crf_var.get()), None)
            self.vid_preset_var.set(matched or t("custom_params"))
        self._applying = False

    def _collect(self) -> dict:
        s = load_settings()
        val = self.vid_presets.get(self.vid_preset_var.get())
        s.update({
            "vid_profile_key": val[0] if val else "",
            "vid_codec": self.codec_var.get(),
            "vid_container": self.container_var.get(),
            "vid_crf": self.crf_var.get(),
            "vid_preset": self.preset_var.get(),
            "vid_height": safe_int(self.height_var.get(), 0, 0, 8700),
            "vid_audio": safe_int(self.audio_var.get(), 128, 0, 320),
            "vid_multi": self.multi_var.get(),
            "vid_move": self.move_var.get(),
            "vid_hw": self.hw_var.get(),
            "vid_recurse": self.recurse_var.get(),
            "vid_outdir": self._outdir(),
            "vid_rename": self.rename_var.get(),
            "theme": ctk.get_appearance_mode(),
        })
        return s

    def _start(self):
        if self.is_running:
            self._log(t("waiting"))
            return
        if not self.files:
            self.show_toast(t(self.EMPTY_KEY), "warn")
            return
        if not find_ffmpeg():
            self.show_toast(t("ffmpeg_missing"), "err", timeout=10000)
            return
        codec = self.codec_var.get()
        container = self.container_var.get()
        crf = self.crf_var.get()
        preset = self.preset_var.get()
        height = safe_int(self.height_var.get(), 0, 0, 8700)
        audio = safe_int(self.audio_var.get(), 128, 0, 320)
        use_hw = self.hw_var.get()
        multi = self.multi_var.get()
        # видео: максимум 2 параллельно — каждое само грузит CPU/GPU
        workers = min(2, len(self.files)) if multi else 1
        ext = video_out_ext(codec, container)

        def single(inp, out, put_prog):
            self.msg_queue.put(("log", t("encoding_one") + os.path.basename(inp)))
            res = process_video(inp, out, codec, crf, preset, height, audio,
                                container, use_hw,
                                progress_cb=lambda f, txt: put_prog(f, txt))
            return res, out

        self._launch_batch(
            ext_fn=lambda inp: ext,
            pool_fn=process_video,
            pool_args_fn=lambda i, o: (i, o, codec, crf, preset, height, audio,
                                       container, use_hw, None),
            serial_fn=lambda i, o, pcb: process_video(
                i, o, codec, crf, preset, height, audio, container, use_hw,
                lambda f, txt: pcb(f, txt)),
            workers=workers,
            size_div=1048576,
            mode="Video",
            single_fn=single,
        )

class ScanSection(BaseSection):
    """
    Сканер: окно драйвера (WIA/TWAIN/системное) → страницы в списке →
    сохранить PDF / JPG. Сжатие можно включить или выключить.
    """
    EXTS = IMAGE_EXTS
    EMPTY_KEY = "no_pages_scan"
    MODE = "Scan"

    def __init__(self, app):
        self.devices_cache = []
        self.pages: List[str] = []
        super().__init__(app, "scanner")
        self._refresh_scanners()

    def _build(self):
        self._build_header(t("scanner"))

        body = ctk.CTkFrame(self, fg_color="transparent")
        # body упаковывается ПОСЛЕ нижних панелей — кнопки всегда видны

        # LEFT — прокручиваемая колонка: при любой высоте окна ни один
        # элемент не «исчезает», в крайнем случае появляется скролл
        left = ctk.CTkScrollableFrame(body, corner_radius=10, width=320)
        left.pack(side="left", fill="y", padx=(0, 6))

        ctk.CTkLabel(left, text=t("scanner"),
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10,
                                                           pady=(6, 4))
        self.dev_list = tk.Listbox(left, height=5, font=("Segoe UI", 12),
                                   relief="flat", highlightthickness=0,
                                   activestyle="none")
        style_listbox(self.dev_list)
        self._lbs.append(self.dev_list)
        self.dev_list.pack(fill="x", padx=10, pady=2)
        btn_ghost(left, text=t("refresh_dev"),
                  command=self._refresh_scanners).pack(fill="x", padx=10,
                                                       pady=(4, 6))
        ctk.CTkLabel(left, text=t("scan_hint"), text_color=CLR_MUTED,
                     justify="left",
                     font=ctk.CTkFont(size=12)).pack(anchor="w", padx=10,
                                                     pady=(0, 4))

        ctk.CTkLabel(left, text=t("folder"),
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10,
                                                           pady=(4, 2))
        fr = ctk.CTkFrame(left, fg_color="transparent")
        fr.pack(fill="x", padx=10, pady=2)
        self.folder_var = ctk.StringVar(
            value=str(Path.home() / "Documents" / "NexusScan"))
        ctk.CTkEntry(fr, textvariable=self.folder_var, width=220).pack(side="left")
        btn_ghost(fr, text="…", width=36,
                  command=self._browse_folder).pack(side="left", padx=4)

        self.open_after_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left, text=t("open_after"),
                        variable=self.open_after_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            anchor="w", padx=10, pady=(6, 2))

        # Compression block
        ctk.CTkLabel(left, text=t("comp_settings"),
                     font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=10,
                                                           pady=(10, 4))
        self.compress_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left, text=t("compress_doc"), variable=self.compress_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER,
                        command=self._toggle_comp_ui).pack(anchor="w", padx=10,
                                                           pady=2)

        self.comp_frame = ctk.CTkFrame(left, fg_color="transparent")
        self.comp_frame.pack(fill="x", padx=6, pady=2)
        r2 = ctk.CTkFrame(self.comp_frame, fg_color="transparent")
        r2.pack(fill="x", padx=4, pady=2)
        self.pdf_preset_var = ctk.StringVar(value=t("smart_pdf"))
        ctk.CTkOptionMenu(r2, variable=self.pdf_preset_var,
                          values=[t("smart_pdf"), t("aggressive"), t("max_compress"),
                                  t("balanced"), t("quality"), t("custom")],
                          width=200, fg_color=CLR_GHOST,
                          button_color=CLR_GHOST_HOVER,
                          button_hover_color=CLR_ACC, text_color=CLR_GHOST_TEXT,
                          command=self._on_pdf_preset).pack(side="left")
        r3 = ctk.CTkFrame(self.comp_frame, fg_color="transparent")
        r3.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(r3, text="DPI").pack(side="left")
        self.comp_dpi = ctk.StringVar(value="130")
        ctk.CTkEntry(r3, textvariable=self.comp_dpi, width=55).pack(side="left", padx=4)
        ctk.CTkLabel(r3, text=t("quality_short")).pack(side="left", padx=(6, 2))
        self.comp_q = ctk.StringVar(value="55")
        ctk.CTkEntry(r3, textvariable=self.comp_q, width=50).pack(side="left")
        self.gray_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.comp_frame, text=t("gray_pdf"),
                        variable=self.gray_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            anchor="w", padx=4, pady=4)

        btn_ghost(left, text=t("del_temp"),
                  command=self._cleanup_temps).pack(fill="x", padx=10,
                                                    pady=(8, 8))

        # RIGHT: pages
        right = ctk.CTkFrame(body, corner_radius=10)
        right.pack(side="left", fill="both", expand=True)
        top = ctk.CTkFrame(right, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(top, text=t("scan_pages"),
                     font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.pages_count = ctk.CTkLabel(top, text="0", text_color=CLR_MUTED)
        self.pages_count.pack(side="right")

        # Кнопки — снизу, ДО растягивающейся сетки: при сжатии окна
        # их не выдавливает за край
        br = ctk.CTkFrame(right, fg_color="transparent")
        br.pack(side="bottom", fill="x", padx=10, pady=(0, 8))
        btn_ghost(br, text=t("remove_sel"), width=130,
                  command=self._remove_pages).pack(side="left", padx=(0, 6))
        btn_ghost(br, text=t("clear_list"), width=130,
                  command=self._clear_pages).pack(side="left")

        # Страницы — сетка миниатюр: клик = выбор, перетаскивание = порядок
        self.pages_area = ctk.CTkScrollableFrame(right, fg_color=CLR_LIST_BG,
                                                 corner_radius=10)
        self.pages_area.pack(fill="both", expand=True, padx=10, pady=4)
        self._page_sel: set = set()
        self._page_cards: dict = {}
        self._drag = {"path": None, "moved": False, "x": 0, "y": 0}
        # колонки сетки подстраиваются под ширину области
        self._page_cols_cur = 5
        self._resize_job = None
        self.pages_area.bind("<Configure>", self._on_pages_resize)

        self.pages_hint = ctk.CTkLabel(
            right, text=t("empty_pages_hint"), justify="center",
            image=icon("scan", 40), compound="top", pady=10,
            font=ctk.CTkFont(size=14), text_color=CLR_MUTED,
            fg_color=CLR_LIST_BG)
        self.pages_hint.place(relx=0.5, rely=0.45, anchor="center")

        # Actions
        act = ctk.CTkFrame(self, corner_radius=10)
        act.pack(side="bottom", fill="x", padx=12, pady=4)
        af = ctk.CTkFrame(act, fg_color="transparent")
        af.pack(fill="x", padx=10, pady=10)
        btn_primary(af, text=t("scan_btn"), width=170, height=42,
                    image=icon_active("scan", 16), compound="left",
                    font=ctk.CTkFont(size=14, weight="bold"),
                    command=self._scan_page).pack(side="left", padx=(0, 10))
        btn_ghost(af, text=t("save_pdf"), width=140, height=42,
                  command=self._save_one_pdf).pack(side="left", padx=(0, 8))
        btn_ghost(af, text=t("save_jpg"), width=140, height=42,
                  command=self._save_jpg).pack(side="left", padx=(0, 8))
        btn_ghost(af, text=t("save_files"), width=150, height=42,
                  command=self._save_jpg).pack(side="left")

        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(side="bottom", fill="x", padx=12, pady=(2, 10))
        self.progress = ctk.CTkProgressBar(bot, height=7)
        self.progress.pack(fill="x", pady=(0, 4))
        self.progress.set(0)
        self.log = ctk.CTkTextbox(bot, height=84, font=ctk.CTkFont(size=13))
        self.log.pack(fill="x")
        body.pack(side="top", fill="both", expand=True, padx=12, pady=4)
        self.log.insert("1.0",
            "1) Выберите сканер\n"
            "2) «Сканировать…» — откроется окно драйвера (все настройки там)\n"
            "3) Можно сканировать несколько раз — страницы собираются в списке\n"
            "4) Сохраните: один PDF, JPG или по отдельным файлам\n"
            if LANG == "ru" else
            "1) Pick a scanner\n"
            "2) Scan… opens the driver window (all settings there)\n"
            "3) Scan multiple times — pages are collected in the list\n"
            "4) Save: single PDF, JPG or separate files\n")
        self.log.configure(state="disabled")
        self.count_label = self.pages_count
        self._toggle_comp_ui()

    def _toggle_comp_ui(self):
        if self.compress_var.get():
            self.comp_frame.pack(fill="x", padx=6, pady=2)
        else:
            self.comp_frame.pack_forget()

    def on_leave(self):
        self._cleanup_temps(silent=True)
        super().on_leave()

    def _browse_folder(self):
        d = filedialog.askdirectory(title=t("folder"))
        if d:
            self.folder_var.set(d)

    def _on_pdf_preset(self, choice: str):
        m = {
            t("smart_pdf"): (130, 55),
            t("aggressive"): (85, 35),
            t("max_compress"): (80, 34),
            t("balanced"): (118, 50),
            t("quality"): (165, 68),
        }
        if choice in m:
            d, q = m[choice]
            self.comp_dpi.set(str(d))
            self.comp_q.set(str(q))

    def _refresh_scanners(self):
        self._log("…")

        def work():
            try:
                devices = list_all_scanners()
            except Exception as e:
                _log_exc("refresh scanners", e)
                devices = []
            def apply_ui():
                try:
                    if not self.winfo_exists():
                        return
                    self.devices_cache = devices
                    self.dev_list.delete(0, "end")
                    for _key, name, _source, _ref in devices:
                        self.dev_list.insert("end", name)
                    n_wia = sum(1 for x in devices if x[2] == "wia")
                    n_tw = sum(1 for x in devices if x[2] == "twain")
                    self._log(f"WIA: {n_wia}, TWAIN: {n_tw}")
                    if devices:
                        self.dev_list.selection_set(0)
                except Exception as e:
                    _log_exc("scanners ui", e)
            try:
                self.after(0, apply_ui)
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _selected_device_key(self):
        sel = self.dev_list.curselection()
        if not sel or not self.devices_cache:
            return "dialog"
        idx = sel[0]
        if 0 <= idx < len(self.devices_cache):
            return self.devices_cache[idx][0]
        return "dialog"

    def _tmp_scan_path(self, ext=".png") -> str:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        return str(TEMP_DIR / f"scan_{time.time_ns()}{ext}")

    def _ingest_file(self, path: str) -> list:
        """Добавить файл; многостраничный TIFF разбить на страницы."""
        added = []
        path = str(path)
        if not os.path.isfile(path):
            return added
        ext = Path(path).suffix.lower()
        if ext in (".tif", ".tiff"):
            try:
                with Image.open(path) as im:
                    n = getattr(im, "n_frames", 1)
                    for i in range(n):
                        try:
                            im.seek(i)
                            frame = im.convert("RGB")
                            out = self._tmp_scan_path(".png")
                            frame.save(out)
                            self.pages.append(out)
                            added.append(out)
                        except Exception as e:
                            _log_exc("tiff frame", e)
                if str(TEMP_DIR) in path:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
                return added
            except Exception as e:
                _log_exc("tiff ingest", e)
        self.pages.append(path)
        added.append(path)
        return added

    def add_paths(self, paths: List[str]):
        """DnD: перетащенные картинки становятся страницами."""
        added = 0
        for p in paths:
            for f in collect_files(p, IMAGE_EXTS, False):
                added += len(self._ingest_file(f))
        if added:
            self._refresh_pages_ui()
            self._log(f"+ {added}")

    def _scan_page(self):
        if self.is_running:
            return
        self.is_running = True
        self.status.configure(text=t("scanning"), text_color=CLR_WARN)
        key = self._selected_device_key()
        devices = list(self.devices_cache)

        self.msg_queue.put(("busy", True))

        def work():
            try:
                out = self._tmp_scan_path(".png")
                ok, result = scan_acquire(key, devices, out, 200)
                if not ok:
                    self.msg_queue.put(("busy", False))
                    self.msg_queue.put(("log", f"✗ {result}"))
                    self.msg_queue.put(("progress", 0, t("ready")))
                    return
                added = self._ingest_file(result)
                self.msg_queue.put(("busy", False))
                self.msg_queue.put(("log", f"✓ +{len(added)} ({len(self.pages)})"))
                self.msg_queue.put(("scan_refresh", None))
                self.msg_queue.put(("progress", 1, t("ready")))
            finally:
                self.is_running = False

        threading.Thread(target=work, daemon=True).start()

    def _page_cols(self) -> int:
        try:
            w = self.pages_area.winfo_width()
            scale = float(self._get_widget_scaling())  # HiDPI: физ./лог. px
        except Exception:
            w, scale = 0, 1.0
        if w <= 1:
            return self._page_cols_cur
        cell = 118 * max(0.5, scale)  # карточка 92 + отступы, в физ. пикселях
        return max(2, int(w / cell))

    def _on_pages_resize(self, _e=None):
        # при ресайзе карточки НЕ пересоздаются — только переставляются
        # (re-grid): плавно, без фризов и мигания миниатюр
        cols = self._page_cols()
        if cols != self._page_cols_cur and self.pages:
            if self._resize_job:
                try:
                    self.after_cancel(self._resize_job)
                except Exception:
                    pass
            self._resize_job = self.after(60, self._relayout_pages)

    def _relayout_pages(self):
        """Переставить существующие карточки под новое число колонок."""
        self._resize_job = None
        self._page_cols_cur = self._page_cols()
        cols = self._page_cols_cur
        try:
            for idx, p in enumerate(self.pages):
                cd = self._page_cards.get(p)
                if not cd:
                    continue
                cd["card"].grid(row=idx // cols, column=idx % cols,
                                padx=5, pady=5)
                cd["num"].configure(text=str(idx + 1))
        except Exception as e:
            _log_exc("relayout pages", e)

    def _refresh_pages_ui(self):
        self._resize_job = None
        self._page_cols_cur = self._page_cols()
        for w in list(self.pages_area.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._page_cards.clear()
        self._page_sel = {p for p in self._page_sel if p in self.pages}
        for idx, p in enumerate(self.pages):
            self._make_page_card(idx, p)
        self.pages_count.configure(text=str(len(self.pages)))
        self.status.configure(text=t("ready"), text_color=CLR_MUTED)
        try:
            if self.pages:
                self.pages_hint.place_forget()
            else:
                self.pages_hint.place(relx=0.5, rely=0.45, anchor="center")
        except Exception:
            pass

    def _make_page_card(self, idx: int, path: str):
        sel = path in self._page_sel
        card = ctk.CTkFrame(self.pages_area, corner_radius=8, fg_color=CLR_ROW,
                            border_width=2,
                            border_color=(CLR_ACC if sel else CLR_ROW))
        cols = self._page_cols_cur
        card.grid(row=idx // cols, column=idx % cols, padx=5, pady=5)
        img = ctk.CTkLabel(card, text="…", width=92, height=118,
                           corner_radius=4, fg_color=CLR_GHOST,
                           text_color=CLR_MUTED)
        img.pack(padx=5, pady=(5, 2))
        num = ctk.CTkLabel(card, text=str(idx + 1), text_color=CLR_MUTED,
                           font=ctk.CTkFont(size=12, weight="bold"))
        num.pack(pady=(0, 4))
        card._nexus_page = path  # для поиска цели при перетаскивании
        self._page_cards[path] = {"card": card, "img": img, "num": num}
        cached = self._thumb_cache.get(path)
        if cached is not None:
            img.configure(image=cached, text="")
        else:
            self._thumb_request(path, (92, 118), ("page", path))
        for w in [card] + list(card.winfo_children()):
            try:
                w.bind("<ButtonPress-1>",
                       lambda e, p=path: self._page_press(e, p))
                w.bind("<B1-Motion>", self._page_motion)
                w.bind("<ButtonRelease-1>",
                       lambda e, p=path: self._page_release(e, p))
            except Exception:
                pass

    def _apply_thumb(self, key, pil, disp):
        kind, ident = key
        if kind == "page":
            try:
                img = ctk.CTkImage(light_image=pil, dark_image=pil, size=disp)
                self._thumb_cache[ident] = img
                cd = self._page_cards.get(ident)
                if cd:
                    cd["img"].configure(image=img, text="")
            except Exception as e:
                _log_exc("page thumb", e)
            return
        super()._apply_thumb(key, pil, disp)

    # выбор кликом / порядок перетаскиванием
    def _page_press(self, e, path):
        self._drag = {"path": path, "moved": False,
                      "x": e.x_root, "y": e.y_root}

    def _page_motion(self, e):
        d = self._drag
        if d["path"] and (abs(e.x_root - d["x"]) > 14 or
                          abs(e.y_root - d["y"]) > 14):
            d["moved"] = True

    def _page_release(self, e, path):
        d = self._drag
        self._drag = {"path": None, "moved": False, "x": 0, "y": 0}
        if not d["path"]:
            return
        if not d["moved"]:
            # клик — переключить выбор
            if path in self._page_sel:
                self._page_sel.discard(path)
            else:
                self._page_sel.add(path)
            cd = self._page_cards.get(path)
            if cd:
                cd["card"].configure(
                    border_color=(CLR_ACC if path in self._page_sel
                                  else CLR_ROW))
            return
        # перетаскивание — найти карточку под курсором
        target = None
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
            while w is not None:
                tp = getattr(w, "_nexus_page", None)
                if tp is not None:
                    target = tp
                    break
                w = w.master
        except Exception:
            pass
        if target and target != d["path"]:
            self._reorder_pages(d["path"], target)

    def _reorder_pages(self, src: str, target: str):
        try:
            self.pages.remove(src)
            self.pages.insert(self.pages.index(target), src)
        except ValueError:
            return
        self._relayout_pages()  # без пересоздания — мгновенно

    def _remove(self):
        # Del в сканере работает со страницами, а не с self.files
        self._remove_pages()

    def _clear(self):
        self._clear_pages()

    def _remove_pages(self):
        if not self._page_sel:
            return
        for p in list(self._page_sel):
            try:
                if str(TEMP_DIR) in os.path.abspath(p):
                    os.unlink(p)
            except Exception:
                pass
            if p in self.pages:
                self.pages.remove(p)
        self._page_sel.clear()
        self._refresh_pages_ui()

    def _clear_pages(self):
        if self.pages and not messagebox.askyesno(APP_NAME, t("clear_pages_q")):
            return
        for p in self.pages:
            try:
                if os.path.isfile(p) and str(TEMP_DIR) in os.path.abspath(p):
                    os.unlink(p)
            except Exception:
                pass
        self.pages.clear()
        self._page_sel.clear()
        self._refresh_pages_ui()

    def _cleanup_temps(self, silent=False):
        removed = 0
        for p in list(self.pages):
            try:
                if os.path.isfile(p) and str(TEMP_DIR) in os.path.abspath(p):
                    os.unlink(p)
                    removed += 1
            except Exception:
                pass
        try:
            if TEMP_DIR.is_dir():
                for f in TEMP_DIR.glob("scan_*"):
                    try:
                        f.unlink()
                        removed += 1
                    except Exception:
                        pass
        except Exception:
            pass
        if not silent:
            self.pages.clear()
            self._refresh_pages_ui()
            self._log(f"🗑 {removed}")

    def _preset_key(self) -> str:
        m = {
            t("smart_pdf"): "smart",
            t("aggressive"): "aggressive",
            t("max_compress"): "max",
            t("balanced"): "balanced",
            t("quality"): "quality",
            t("custom"): "custom",
        }
        return m.get(self.pdf_preset_var.get(), "smart")

    def _ensure_folder(self) -> str:
        folder = self.folder_var.get().strip() or \
            str(Path.home() / "Documents" / "NexusScan")
        ok, err = ensure_writable_dir(folder)
        if not ok:
            folder = str(Path.home() / "Documents" / "NexusScan")
            ok2, err2 = ensure_writable_dir(folder)
            if not ok2:
                raise PermissionError(err or err2 or "no write")
            self.folder_var.set(folder)
            self._log(f"⚠ {err} → {folder}")
        return folder

    def _maybe_open(self, folder: str):
        if self.open_after_var.get():
            try:
                os.startfile(folder)
            except Exception:
                pass

    def _save_one_pdf(self):
        if not self.pages:
            self.show_toast(t("no_pages_scan"), "warn")
            return
        if self.is_running:
            return
        try:
            folder = self._ensure_folder()
        except Exception as e:
            self.show_toast(str(e), "err")
            return
        self.is_running = True
        now = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_pdf = str(Path(folder) / f"document_{now}.pdf")
        raw_pdf = str(TEMP_DIR / f"doc_raw_{now}.pdf")
        compress = self.compress_var.get()
        preset = self._preset_key()
        dpi = safe_int(self.comp_dpi.get(), 130, 50, 300)
        quality = safe_int(self.comp_q.get(), 55, 20, 95)
        gray = self.gray_var.get()
        pages = list(self.pages)

        def work():
            try:
                TEMP_DIR.mkdir(parents=True, exist_ok=True)
                self.msg_queue.put(("log", f"→ PDF ({len(pages)})…"))
                span = 0.5 if compress else 1.0
                ok, msg, _added, _skipped = images_to_pdf(
                    pages, raw_pdf if compress else out_pdf,
                    progress_cb=lambda i, n: self.msg_queue.put(
                        ("progress", span * i / max(1, n), f"{i}/{n}")))
                if not ok:
                    self.msg_queue.put(("log", f"✗ {tr_msg(msg)}"))
                    return
                if compress:
                    _i, ok2, msg2, orig, new = process_pdf(
                        raw_pdf, out_pdf, preset, dpi, quality, gray,
                        progress_cb=lambda f: self.msg_queue.put(
                            ("progress", 0.5 + 0.5 * f, f"{int(f * 100)}%")))
                    if ok2:
                        self.msg_queue.put(("log", f"✓ PDF: {out_pdf} ({new / 1024:.0f} KB)"))
                    else:
                        # сжатие не удалось/без выигрыша → исходник ещё на
                        # месте, копируем его (документ не теряется)
                        try:
                            shutil.copy2(raw_pdf, out_pdf)
                            self.msg_queue.put(("log", f"✓ PDF: {out_pdf}"))
                        except Exception as e:
                            _log_exc("scan pdf copy", e)
                            self.msg_queue.put(("log", f"✗ {tr_msg(msg2)}"))
                            return
                else:
                    self.msg_queue.put(("log", f"✓ PDF: {out_pdf}"))
                add_history({
                    "time": datetime.now().strftime("%d.%m %H:%M"),
                    "mode": "Scan→PDF",
                    "count": len(pages),
                    "saved_mb": 0,
                    "folder": folder,
                    "names": [os.path.basename(out_pdf)],
                })
                self._maybe_open(folder)
                self.msg_queue.put(("progress", 1, t("done")))
            finally:
                try:
                    if os.path.isfile(raw_pdf):
                        os.unlink(raw_pdf)
                except Exception:
                    pass
                self.is_running = False

        threading.Thread(target=work, daemon=True).start()

    def _save_jpg(self):
        """Все страницы → JPG в папку (с опциональным сжатием)."""
        if not self.pages:
            self.show_toast(t("no_pages"), "warn")
            return
        if self.is_running:
            return
        try:
            folder = self._ensure_folder()
        except Exception as e:
            self.show_toast(str(e), "err")
            return
        self.is_running = True
        pages = list(self.pages)
        compress = self.compress_var.get()
        base_q = safe_int(self.comp_q.get(), 55, 20, 95)
        q = max(40, min(92, base_q + (0 if compress else 25)))

        def work():
            try:
                ok_n = 0
                stamp = datetime.now().strftime("%H%M%S")
                for i, p in enumerate(pages, 1):
                    out = str(Path(folder) / f"scan_{i:02d}_{stamp}.jpg")
                    out = unique_out_path(out)
                    ok = False
                    msg = ""
                    if compress:
                        _a, ok, msg, _o, _n = process_image(p, out, q, 0, "JPEG", True)
                        if not ok and msg == "err_no_gain":
                            # без выигрыша — просто копируем страницу как JPG
                            ok = self._plain_jpg(p, out)
                    else:
                        ok = self._plain_jpg(p, out)
                    if ok:
                        ok_n += 1
                        self.msg_queue.put(("log", f"✓ {os.path.basename(out)}"))
                    else:
                        self.msg_queue.put(("log", f"✗ {i}: {tr_msg(msg)}"))
                    self.msg_queue.put(("progress", i / len(pages),
                                        f"{i}/{len(pages)}"))
                self.msg_queue.put(("log", f"JPG: {ok_n}/{len(pages)} → {folder}"))
                add_history({
                    "time": datetime.now().strftime("%d.%m %H:%M"),
                    "mode": "Scan→files",
                    "count": ok_n,
                    "saved_mb": 0,
                    "folder": folder,
                })
                self._maybe_open(folder)
            finally:
                self.is_running = False

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _plain_jpg(src: str, out: str) -> bool:
        try:
            with Image.open(src) as im0:
                im = ImageOps.exif_transpose(im0)
                if im.mode in ("RGBA", "P", "LA"):
                    im = im.convert("RGB")
                im.save(out, "JPEG", quality=92, optimize=True)
            return True
        except Exception as e:
            _log_exc("plain jpg", e)
            return False

    def _apply(self):
        s = self.settings
        self.compress_var.set(bool(s.get("scan_compress", True)))
        self.pdf_preset_var.set(s.get("scan_pdf_preset") or t("smart_pdf"))
        self.comp_dpi.set(str(safe_int(s.get("scan_comp_dpi"), 130, 50, 300)))
        self.comp_q.set(str(safe_int(s.get("scan_comp_q"), 55, 20, 95)))
        self.gray_var.set(bool(s.get("scan_gray", False)))
        self.open_after_var.set(bool(s.get("scan_open_folder", True)))
        if s.get("scan_folder"):
            self.folder_var.set(s["scan_folder"])
        self._toggle_comp_ui()

    def _collect(self) -> dict:
        s = load_settings()
        s.update({
            "scan_compress": self.compress_var.get(),
            "scan_pdf_preset": self.pdf_preset_var.get(),
            "scan_comp_dpi": safe_int(self.comp_dpi.get(), 130, 50, 300),
            "scan_comp_q": safe_int(self.comp_q.get(), 55, 20, 95),
            "scan_gray": self.gray_var.get(),
            "scan_folder": self.folder_var.get(),
            "scan_open_folder": self.open_after_var.get(),
            "theme": ctk.get_appearance_mode(),
        })
        return s

    def _start(self):
        self._scan_page()


class HistorySection(ctk.CTkFrame):
    """История: итоговая экономия сверху + операции по дням, клик — папка."""

    def __init__(self, app):
        super().__init__(app.content, fg_color="transparent")
        self.app = app

        h = ctk.CTkFrame(self, fg_color="transparent")
        h.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(h, text=t("history_title"),
                     font=ctk.CTkFont(size=20, weight="bold")).pack(side="left")
        btn_ghost(h, text=t("history_clear"), width=150,
                  text_color=CLR_DANGER_TEXT,
                  command=self._clear).pack(side="right")

        hero = ctk.CTkFrame(self, fg_color="transparent")
        hero.pack(fill="x", padx=16, pady=(4, 6))
        self._stats = []
        for i, cap in enumerate((t("total_saved"), t("files_done"),
                                 t("operations"))):
            card = ctk.CTkFrame(hero, corner_radius=12)
            card.pack(side="left", expand=True, fill="x",
                      padx=(0 if i == 0 else 10, 0))
            kw = {"text_color": CLR_ACC} if i == 0 else {}
            v = ctk.CTkLabel(card, text="0",
                             font=ctk.CTkFont(size=24, weight="bold"), **kw)
            v.pack(anchor="w", padx=16, pady=(12, 0))
            ctk.CTkLabel(card, text=cap, font=ctk.CTkFont(size=11),
                         text_color=CLR_MUTED).pack(anchor="w", padx=16,
                                                    pady=(0, 12))
            self._stats.append(v)

        self.area = ctk.CTkScrollableFrame(self, fg_color=CLR_LIST_BG,
                                           corner_radius=12)
        self.area.pack(fill="both", expand=True, padx=16, pady=(4, 16))
        self._hint = ctk.CTkLabel(self, text=t("history_empty_hint"),
                                  font=ctk.CTkFont(size=13),
                                  text_color=CLR_MUTED, fg_color=CLR_LIST_BG)
        self._reload()

    def restyle(self):
        pass

    def on_leave(self):
        pass

    def _reload(self):
        for w in list(self.area.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        hist = load_json(HISTORY_FILE, [])
        if not isinstance(hist, list):
            hist = []
        rows = [x for x in hist[:200] if isinstance(x, dict)]

        saved_mb = sum(float(x.get("saved_mb") or 0) for x in rows)
        files = sum(safe_int(x.get("count"), 0) for x in rows)
        self._stats[0].configure(text=human_size(saved_mb * 1048576))
        self._stats[1].configure(text=str(files))
        self._stats[2].configure(text=str(len(rows)))

        today = datetime.now().strftime("%d.%m")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%d.%m")
        last_day = None
        for item in rows:
            time_s = str(item.get("time", ""))
            day = time_s.split(" ")[0] if " " in time_s else time_s
            if day != last_day:
                last_day = day
                cap = t("today") if day == today else \
                    (t("yesterday") if day == yesterday else day)
                ctk.CTkLabel(self.area, text=str(cap).upper(),
                             font=ctk.CTkFont(size=10, weight="bold"),
                             text_color=CLR_MUTED).pack(anchor="w", padx=10,
                                                        pady=(8, 2))
            self._make_row(item, time_s)
        try:
            if rows:
                self._hint.place_forget()
            else:
                self._hint.place(relx=0.5, rely=0.58, anchor="center")
        except Exception:
            pass

    def _make_row(self, item, time_s):
        folder = item.get("folder") or ""
        r = ctk.CTkFrame(self.area, corner_radius=9, fg_color=CLR_ROW)
        r.pack(fill="x", padx=4, pady=3)
        r.grid_columnconfigure(3, weight=1)
        tm = time_s.split(" ")[1] if " " in time_s else time_s
        ctk.CTkLabel(r, text=tm, width=52, anchor="w", text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0,
                                                     padx=(12, 6), pady=9)
        ctk.CTkLabel(r, text=_localize_mode(item.get("mode", "—")), width=120,
                     anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=1, padx=6)
        ctk.CTkLabel(r, text=f"×{item.get('count', 0)}", width=48, anchor="w",
                     text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=2, padx=6)
        ctk.CTkLabel(r, text=folder, anchor="w", text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=3,
                                                     sticky="ew", padx=6)
        saved = float(item.get("saved_mb") or 0)
        s_txt = f"−{human_size(saved * 1048576)}" if saved > 0 else "—"
        ctk.CTkLabel(r, text=s_txt, anchor="e", text_color=CLR_ACC,
                     font=ctk.CTkFont(size=13, weight="bold")).grid(
            row=0, column=4, padx=(6, 12))
        if folder:
            widgets = [r] + list(r.winfo_children())
            for w in widgets:
                try:
                    w.bind("<Button-1>", lambda e, f=folder: self._open(f))
                except Exception:
                    pass

    def _open(self, folder):
        if folder and os.path.isdir(folder):
            try:
                os.startfile(folder)
            except Exception as e:
                _log_exc("history open", e)

    def _clear(self):
        save_json(HISTORY_FILE, [])
        self._reload()


class SupportWindow(ctk.CTkToplevel):
    """Supporting the project, and the link to the source code.

    Payment details are deliberately not shown here, only a link to the website
    section: methods change while an installed copy is updated rarely. The
    source link is not decoration — the AGPL-3.0 requires that whoever receives
    the program can get the code it was built from.
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title(t("support_title"))
        self.resizable(False, False)
        self.transient(app)
        center_window(self, 470, 322)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(20, 6))
        ctk.CTkLabel(head, text="", image=icon_active("heart", 26),
                     width=44, height=44, corner_radius=12,
                     fg_color=CLR_ACC).pack(side="left")
        ctk.CTkLabel(head, text=t("support_title"),
                     font=ctk.CTkFont(size=17, weight="bold")).pack(
            side="left", padx=12)

        # wraplength is required: without it CTkLabel stretches to its longest
        # line, overflows the window and gets clipped on both sides. The value
        # is the window width minus the margins.
        ctk.CTkLabel(self, text=t("support_text"), justify="left",
                     wraplength=420, text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=12)).pack(
            anchor="w", padx=22, pady=(6, 14))

        btn_primary(self, text=t("support_open"), height=40,
                    corner_radius=10, image=icon_active("heart", 16),
                    compound="left",
                    font=ctk.CTkFont(size=13, weight="bold"),
                    command=lambda: open_url(PROJECT_SUPPORT)).pack(
            fill="x", padx=22)

        btn_ghost(self, text=t("source_code"), height=36, corner_radius=10,
                  image=icon("code", 16), compound="left",
                  command=lambda: open_url(PROJECT_SOURCE)).pack(
            fill="x", padx=22, pady=(8, 2))
        ctk.CTkLabel(self, text=t("source_hint"), text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=10)).pack(anchor="w", padx=24)

        btn_ghost(self, text=t("close"), width=110, height=32,
                  corner_radius=10, command=self.destroy).pack(
            side="bottom", pady=(0, 16))


class SettingsWindow(ctk.CTkToplevel):
    """Язык, тема, звук — всё, что раньше было раскидано по экранам."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title(t("settings"))
        self.resizable(False, False)
        self.transient(app)
        center_window(self, 352, 322)

        ctk.CTkLabel(self, text=t("settings"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=18, pady=(16, 10))

        s = load_settings()

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=4)
        ctk.CTkLabel(row, text=t("language")).pack(side="left")
        self.lang_seg = ctk.CTkSegmentedButton(
            row, values=["RU", "EN"], command=self._on_lang,
            selected_color=CLR_ACC, selected_hover_color=CLR_ACC_HOVER)
        self.lang_seg.set("EN" if LANG == "en" else "RU")
        self.lang_seg.pack(side="right")

        row2 = ctk.CTkFrame(self, fg_color="transparent")
        row2.pack(fill="x", padx=18, pady=8)
        ctk.CTkLabel(row2, text=t("theme")).pack(side="left")
        # «Авто» следует за темой Windows
        self._theme_map = {t("auto"): "System", t("th_dark"): "Dark",
                           t("th_light"): "Light"}
        self.theme_seg = ctk.CTkSegmentedButton(
            row2, values=list(self._theme_map.keys()),
            command=self._on_theme,
            selected_color=CLR_ACC, selected_hover_color=CLR_ACC_HOVER)
        cur_mode = s.get("theme", "System")
        cur_label = next((lb for lb, m in self._theme_map.items()
                          if m == cur_mode), t("auto"))
        self.theme_seg.set(cur_label)
        self.theme_seg.pack(side="right")

        row3 = ctk.CTkFrame(self, fg_color="transparent")
        row3.pack(fill="x", padx=18, pady=4)
        self.sound_var = ctk.BooleanVar(value=bool(s.get("sound", True)))
        ctk.CTkCheckBox(row3, text=t("sound_done"), variable=self.sound_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER,
                        command=self._on_sound).pack(side="left")

        row4 = ctk.CTkFrame(self, fg_color="transparent")
        row4.pack(fill="x", padx=18, pady=(10, 0))
        self.ctx_var = ctk.BooleanVar(value=is_context_menu_registered())
        ctk.CTkCheckBox(row4, text=t("ctx_menu"), variable=self.ctx_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER,
                        command=self._on_ctx).pack(side="left")
        ctk.CTkLabel(self, text=t("ctx_hint"), text_color=CLR_MUTED,
                     justify="left",
                     font=ctk.CTkFont(size=10)).pack(
            anchor="w", padx=46, pady=(2, 0))

        ctk.CTkLabel(self, text=f"{APP_NAME} v{APP_VERSION}\n{APP_CREDIT}",
                     text_color=CLR_MUTED, justify="center",
                     font=ctk.CTkFont(size=11)).pack(side="bottom", pady=10)

    def _on_lang(self, value):
        new = "en" if value == "EN" else "ru"
        if new == LANG:
            return
        set_lang(new)
        s = load_settings()
        s["lang"] = new
        save_settings(s)
        self.destroy()
        self.app.rebuild()

    def _on_theme(self, value):
        mode = self._theme_map.get(value, "System")
        ctk.set_appearance_mode(mode)
        s = load_settings()
        s["theme"] = mode
        save_settings(s)
        cur = self.app.current
        if cur is not None and hasattr(cur, "restyle"):
            cur.restyle()

    def _on_sound(self):
        s = load_settings()
        s["sound"] = bool(self.sound_var.get())
        save_settings(s)

    def _on_ctx(self):
        on = bool(self.ctx_var.get())
        ok = register_context_menu() if on else unregister_context_menu()
        if not ok:
            self.ctx_var.set(not on)  # откат галочки при неудаче
            win_msgbox(t("ctx_fail"), APP_NAME)
            return
        # Win11 24H2+: HKCU-пункты в меню не видны — нужна системная
        # часть под UAC. Отказ не страшен: «Отправить» работает всегда.
        if _hkcu_menu_invisible() and is_frozen():
            _elevate_menu_flag("--register-menu-hklm" if on
                               else "--unregister-menu-hklm")


class App(TkinterDnD.DnDWrapper, ctk.CTk):
    """Единый root: постоянный сайдбар + сменные разделы-фреймы.
    DnDWrapper даёт drop_target_register/dnd_bind (tkdnd)."""

    SECTIONS = {}
    NAV_ITEMS = (("pdf", "pdf"), ("img", "photo"), ("vid", "video"),
                 ("scan", "scanner"), ("hist", "history"))

    def __init__(self):
        super().__init__()
        s = load_settings()
        set_lang(s.get("lang", "ru"))
        ctk.set_appearance_mode(s.get("theme", "Dark"))
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.minsize(1000, 680)
        restore_window_geo("main", self, 1140, 750)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.current = None
        self._current_name = ""
        self.SECTIONS = {
            "pdf": PdfSection,
            "img": ImageSection,
            "vid": VideoSection,
            "scan": ScanSection,
            "hist": HistorySection,
        }
        self._build_shell()
        self._setup_dnd()
        start = s.get("last_section", "pdf")
        self.show(start if start in self.SECTIONS else "pdf")
        # в режиме «Авто» Windows может сменить тему на лету —
        # перекрашиваем tk-виджеты текущего раздела
        try:
            tracker = getattr(ctk, "AppearanceModeTracker", None)
            if tracker is not None:
                tracker.add(self._on_sys_appearance, self)
        except Exception as e:
            _log_exc("appearance tracker", e)
        # погасить заставку загрузки — окно готово
        _BOOT_SPLASH_STOP.set()
        self._set_app_icon()
        # один экземпляр: принимаем пути от повторных запусков
        # (контекстное меню Проводника шлёт по одному процессу на файл)
        if is_frozen() or os.environ.get("NEXUS_SINGLE") == "1":
            _start_instance_server()
        self.after(300, self._poll_ipc)
        try:
            context_menu_health_check()
        except Exception as e:
            _log_exc("menu health call", e)
        if _CLI_PATHS:
            self.after(450, lambda: self.route_paths(_CLI_PATHS))

    def _on_sys_appearance(self, _mode):
        try:
            cur = self.current
            if cur is not None and hasattr(cur, "restyle"):
                cur.restyle()
        except Exception:
            pass

    def _set_app_icon(self):
        """Фирменная иконка окна/панели задач: бирюзовая плитка с N."""
        try:
            import io
            import base64
            from PIL import ImageDraw as _ID, ImageFont as _IF
            photos = []
            for size in (64, 32):
                im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                d = _ID.Draw(im)
                rad = max(4, size * 22 // 100)
                d.rounded_rectangle([0, 0, size - 1, size - 1], radius=rad,
                                    fill=(18, 164, 147, 255))
                try:
                    fnt = _IF.truetype("segoeuib.ttf", int(size * 0.62))
                except Exception:
                    fnt = None
                d.text((size / 2, size / 2 + size * 0.02), "N",
                       fill=(255, 255, 255, 255), font=fnt, anchor="mm")
                buf = io.BytesIO()
                im.save(buf, "PNG")
                photos.append(tk.PhotoImage(
                    data=base64.b64encode(buf.getvalue())))
            self._icon_imgs = photos  # держим ссылки от сборщика мусора
            self.iconphoto(True, *photos)
        except Exception as e:
            _log_exc("app icon", e)

    def report_callback_exception(self, exc, val, tb):
        log.error("TK CALLBACK: %s",
                  "".join(traceback.format_exception(exc, val, tb)))

    # ---- каркас: сайдбар + область контента ----

    def _build_shell(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0,
                                    fg_color=CLR_SIDEBAR)
        self.sidebar.grid(row=0, column=0, sticky="nsw")
        self.sidebar.grid_propagate(False)

        logo = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo.pack(fill="x", padx=14, pady=(16, 14))
        ctk.CTkLabel(logo, text="N", width=32, height=32, corner_radius=9,
                     fg_color=CLR_ACC, text_color=CLR_ACC_TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkLabel(logo, text="Nexus",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            side="left", padx=9)

        self._nav = {}
        self._nav_icons = {"pdf": "doc", "img": "img", "vid": "vid",
                           "scan": "scan", "hist": "hist"}
        for key, tkey in self.NAV_ITEMS:
            b = ctk.CTkButton(self.sidebar, text=t(tkey), anchor="w",
                              height=38, corner_radius=10,
                              fg_color="transparent",
                              hover_color=CLR_GHOST_HOVER,
                              text_color=CLR_MUTED,
                              image=icon(self._nav_icons[key], 17),
                              compound="left",
                              font=ctk.CTkFont(size=13, weight="bold"),
                              command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=10, pady=2)
            self._nav[key] = b

        spacer = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        spacer.pack(fill="both", expand=True)

        # Support sits above Settings instead of hiding inside it: this is the
        # only way to thank the author and it should be visible without
        # hunting. A soft accent fill makes it noticeable without shouting.
        btn_ghost(self.sidebar, text=t("support"), anchor="w",
                  image=icon("heart", 16), compound="left",
                  fg_color=CLR_ACC_SOFT, hover_color=CLR_GHOST_HOVER,
                  height=36, command=self.open_support).pack(
            fill="x", padx=10, pady=(2, 2))
        btn_ghost(self.sidebar, text=t("settings"), anchor="w",
                  image=icon("gear", 16), compound="left",
                  height=36, command=self.open_settings).pack(
            fill="x", padx=10, pady=(2, 6))
        ctk.CTkLabel(self.sidebar, text=f"v{APP_VERSION}",
                     text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=10)).pack(pady=(0, 0))
        ctk.CTkLabel(self.sidebar, text=APP_CREDIT,
                     text_color=CLR_MUTED,
                     font=ctk.CTkFont(size=10)).pack(pady=(0, 10))

        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.grid(row=0, column=1, sticky="nsew")

    def open_settings(self):
        """Одно окно настроек: повторный клик поднимает существующее."""
        self._open_single("_settings_win", SettingsWindow)

    def open_support(self):
        self._open_single("_support_win", SupportWindow)

    def _open_single(self, attr: str, cls):
        """One window at a time: a repeat call raises the open one."""
        w = getattr(self, attr, None)
        try:
            if w is not None and w.winfo_exists():
                w.lift()
                w.focus_force()
                return
        except Exception:
            pass
        setattr(self, attr, cls(self))

    def _update_nav(self):
        for key, b in self._nav.items():
            if key == self._current_name:
                b.configure(fg_color=CLR_ACC, text_color=CLR_ACC_TEXT,
                            hover_color=CLR_ACC_HOVER,
                            image=icon_active(self._nav_icons[key], 17))
            else:
                b.configure(fg_color="transparent", text_color=CLR_MUTED,
                            hover_color=CLR_GHOST_HOVER,
                            image=icon(self._nav_icons[key], 17))

    def show(self, name: str):
        if name not in self.SECTIONS:
            name = "pdf"
        if name == self._current_name and self.current is not None:
            return
        if self.current is not None:
            try:
                if hasattr(self.current, "on_leave"):
                    self.current.on_leave()
            except Exception as e:
                _log_exc("on_leave", e)
            try:
                self.current.destroy()
            except Exception as e:
                _log_exc("section destroy", e)
            self.current = None
        self._current_name = name
        self._update_nav()
        self.current = self.SECTIONS[name](self)
        self.current.pack(fill="both", expand=True)
        try:
            s = load_settings()
            s["last_section"] = name
            save_settings(s)
        except Exception:
            pass

    def rebuild(self):
        """Полная пересборка UI (например, после смены языка)."""
        name = self._current_name or "pdf"
        if self.current is not None:
            try:
                self.current.on_leave()
            except Exception:
                pass
            try:
                self.current.destroy()
            except Exception:
                pass
            self.current = None
            self._current_name = ""
        try:
            self.sidebar.destroy()
            self.content.destroy()
        except Exception as e:
            _log_exc("rebuild destroy", e)
        self._build_shell()
        self.show(name)

    def _on_close(self):
        stop_flag.set()
        try:
            kill_active_processes()
        except Exception:
            pass
        if self.current is not None and hasattr(self.current, "on_leave"):
            try:
                self.current.on_leave()
            except Exception:
                pass
        save_window_geo("main", self)
        cleanup_temp()
        self.destroy()

    # ---- Drag & Drop на уровне root ----

    def _setup_dnd(self):
        if not HAS_TKDND:
            log.warning("tkinterdnd2 недоступен — Drag&Drop выключен")
            return
        try:
            self.TkdndVersion = TkinterDnD._require(self)
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_tkdnd)
            log.info("tkdnd %s ready", self.TkdndVersion)
        except Exception as e:
            _log_exc("tkdnd init", e)

    def _on_tkdnd(self, event):
        try:
            paths = list(self.tk.splitlist(event.data))
        except Exception:
            paths = _brace_parse(str(event.data))
        self._dispatch(paths)

    def _dispatch(self, paths):
        cur = self.current
        if cur is not None and hasattr(cur, "add_paths"):
            try:
                cur.add_paths(paths)
            except Exception as e:
                _log_exc("drop dispatch", e)

    # ---- приём файлов из контекстного меню / повторных запусков ----

    def _poll_ipc(self):
        """Раз в 250 мс забираем пути от других процессов. Пачка из
        N сообщений (выделили N файлов) склеивается в один вызов."""
        msgs = []
        try:
            while True:
                msgs.append(_IPC_QUEUE.get_nowait())
        except queue.Empty:
            pass
        if msgs:
            if any(m.get("activate") for m in msgs):
                self._activate_window()
            paths = [p for m in msgs for p in (m.get("paths") or [])]
            if paths:
                self.route_paths(paths)
        self.after(250, self._poll_ipc)

    def route_paths(self, paths):
        """Открыть раздел по типам файлов (большинство решает) и добавить."""
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            return
        counts = {"pdf": 0, "img": 0, "vid": 0}

        def bump(ext):
            if ext == ".pdf":
                counts["pdf"] += 1
            elif ext in IMAGE_EXTS:
                counts["img"] += 1
            elif ext in VIDEO_EXTS:
                counts["vid"] += 1

        for p in paths:
            if os.path.isdir(p):
                try:  # неглубокая оценка содержимого папки
                    for i, f in enumerate(os.scandir(p)):
                        if i >= 200:
                            break
                        bump(os.path.splitext(f.name)[1].lower())
                except OSError:
                    pass
            else:
                bump(os.path.splitext(p)[1].lower())
        best = max(counts, key=counts.get)
        log.info("routed %d path(s) -> %s %s", len(paths), best, counts)
        if counts[best] > 0 and best != self._current_name:
            self.show(best)
        self._dispatch(paths)

    def _activate_window(self):
        """Поднять окно наверх (разворачиваем из трея/минимизации)."""
        try:
            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.after(200, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception as e:
            _log_exc("activate window", e)


def main():
    cleanup_stale_temp()
    App().mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()





