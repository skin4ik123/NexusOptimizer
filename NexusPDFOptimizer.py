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
import io
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
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
import multiprocessing
from typing import List, Tuple, Optional
from datetime import datetime, timedelta

APP_NAME = "Nexus Optimizer"
APP_VERSION = "3.9.1"
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
        # dynamic=True: позднее связывание. Без него comtypes генерирует
        # обёртки библиотеки типов, а в собранной программе писать их некуда —
        # приходил «голый» IDispatch без .Save, и каждый раз срабатывал
        # запасной PowerShell (на офисных ПК его часто запрещают политики).
        ws = cc.CreateObject("WScript.Shell", dynamic=True)
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
        "scan_kind_twain32": "TWAIN 32-бит",
        "scan_settings": "Параметры сканирования",
        "scan_use_ui": "Настраивать в окне сканера",
        "scan_hint_ui": "Разрешение, цвет и подачу бумаги выберите\nв окне сканера — оно откроется по кнопке",
        "scan_source": "Подача",
        "scan_src_flatbed": "Стекло (планшет)",
        "scan_src_feeder": "Автоподатчик",
        "scan_src_duplex": "Податчик, 2 стороны",
        "scan_color": "Цвет",
        "scan_col_color": "Цветной",
        "scan_col_gray": "Оттенки серого",
        "scan_col_bw": "Чёрно-белый",
        "scan_dpi": "Разрешение",
        "scan_paper": "Формат",
        "scan_pap_auto": "Вся область сканера",
        "scan_after": "После сканирования",
        "scan_deskew": "Выравнивать перекошенные страницы",
        "scan_skip_blank": "Пропускать пустые листы (автоподатчик)",
        "scan_select_all": "Выбрать все",
        "scan_help": "1) Выберите сканер и параметры слева\n2) «Сканировать» — страницы появляются по мере сканирования\n3) Щелчок выбирает страницу, перетаскивание меняет порядок, ↺ ↻ поворачивают\n4) Сохраните одним PDF или картинками JPG. Страницы не пропадут до «Очистить список»",
        "scan_searching": "Поиск сканеров…",
        "scan_found": "Найдено сканеров: {n}",
        "scan_no_devices": "Сканеры не найдены. Включите сканер, проверьте кабель или сеть и нажмите «Обновить список».",
        "scan_pick_device": "Выберите сканер в списке слева",
        "scan_cancel": "Остановить",
        "scan_st_opening": "Подключение к сканеру…",
        "scan_st_waiting_ui": "Открыто окно сканера…",
        "scan_st_scanning": "Сканирование… получено изображений: {n}",
        "scan_st_scanning0": "Сканирование…",
        "scan_page_n": "страница {n}",
        "scan_done_n": "Отсканировано страниц: {n}",
        "scan_blank_n": "пустых пропущено: {n}",
        "scan_straight_n": "выровнено: {n}",
        "scan_kept_n": "Уже полученные страницы сохранены: {n}",
        "scan_cancelled": "Сканирование остановлено",
        "scan_blank_skipped": "пустой лист пропущен",
        "scan_no_pages_came": "Сканер не прислал ни одной страницы",
        "scan_restored": "Страницы с прошлого раза на месте: {n}",
        "select_pages_first": "Сначала выберите страницы — щелчком по ним",
        "scan_warn_ui_required": "Этот сканер работает только через своё окно — оно открыто.",
        "scan_warn_no_duplex": "Сканер не умеет сканировать обе стороны — отсканирована одна.",
        "scan_warn_color_substituted": "Сканер не поддерживает выбранный цвет — программа привела страницы к нему сама.",
        "scan_warn_resolution_refused": "Сканер не принял разрешение — использовано {dpi} dpi.",
        "scan_err_helpers_missing": "Компонент сканирования не найден. Переустановите программу.",
        "scan_err_dsm_missing": "Не найден менеджер TWAIN. Переустановите программу.",
        "scan_err_no_device": "Сканер не найден. Проверьте, что он включён и подключён, и обновите список.",
        "scan_err_device_busy": "Сканер занят другой программой или другим пользователем. Закройте её и повторите.",
        "scan_err_device_offline": "Сканер не отвечает. Проверьте питание и кабель (или сеть).",
        "scan_err_device_open_failed": "Не удалось подключиться к сканеру. Проверьте, что он включён, или переустановите его драйвер.",
        "scan_err_paper_jam": "Замятие бумаги. Извлеките лист и нажмите «Сканировать» ещё раз.",
        "scan_err_double_feed": "Податчик захватил несколько листов сразу. Разложите стопку и повторите.",
        "scan_err_feeder_empty": "В автоподатчике нет бумаги.",
        "scan_err_no_feeder": "У этого сканера нет автоподатчика — выберите подачу «Стекло».",
        "scan_err_simplex_refused": "Драйвер не отключил сканирование двух сторон. Сканирование остановлено; проверьте настройки драйвера Epson.",
        "scan_err_cover_open": "Открыта крышка сканера.",
        "scan_err_warming_up": "Сканер прогревается — повторите через минуту.",
        "scan_err_paper_problem": "Проблема с бумагой в податчике — проверьте лоток.",
        "scan_err_user_intervention": "Сканер просит внимания — проверьте его панель.",
        "scan_err_communication": "Связь со сканером прервалась. Проверьте кабель или сеть.",
        "scan_err_bad_setting": "Сканер не принял эти параметры — попробуйте другое разрешение или цвет.",
        "scan_err_access_denied": "Нет доступа к сканеру.",
        "scan_err_wia_unavailable": "Служба Windows для сканеров (WIA) недоступна на этом компьютере.",
        "scan_err_wia_service": "Не запущена служба «Служба загрузки изображений Windows (WIA)». Включите её в «Службах» Windows.",
        "scan_err_out_of_memory": "Не хватило памяти для страницы — уменьшите разрешение.",
        "scan_err_driver_error": "Драйвер сканера сообщил об ошибке.",
        "scan_err_driver_crashed": "Драйвер сканера аварийно завершился. Программа продолжает работу.",
        "scan_err_timeout": "Сканер перестал отвечать — сканирование остановлено.",
        "scan_err_internal": "Ошибка сканирования — подробности в журнале программы.",
        "scan_pages": "Отсканированные страницы",
        "scan_btn": "Сканировать",
        "save_pdf": "Сохранить PDF",
        "save_jpg": "Сохранить JPG",
        "compress_doc": "Сжимать документ",
        "folder": "Папка сохранения",
        "refresh_dev": "Обновить список",
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
        "no_pages_scan": "Нет страниц. Нажмите «Сканировать».",
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
        "err_same_file": "Исходный файл и результат должны иметь разные имена.",
        "err_no_gain": "Почти без выигрыша — оригинал не тронут",
        "err_bad_result": "Результат не прошёл проверку — оригинал не тронут",
        "err_no_access": "Нет доступа к файлу",
        "err_pymupdf": "Нужен PyMuPDF",
        "err_no_write": "Нет прав на запись в папку",
        "err_dir": "Папка недоступна",
        "err_add_pdf": "Не удалось добавить PDF",
        "err_add_pages": "Не удалось добавить ни одной страницы",
        "single_pdf_note": "один файл → изображения обрабатываются параллельно",
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
        "empty_pages_hint": "Нажмите «Сканировать» —\nстраницы появятся здесь\n\nили перетащите сюда картинки",
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
        "scan_kind_twain32": "TWAIN 32-bit",
        "scan_settings": "Scan settings",
        "scan_use_ui": "Set up in the scanner's window",
        "scan_hint_ui": "Choose resolution, colour and paper source\nin the scanner's window — it opens on Scan",
        "scan_source": "Source",
        "scan_src_flatbed": "Glass (flatbed)",
        "scan_src_feeder": "Document feeder",
        "scan_src_duplex": "Feeder, both sides",
        "scan_color": "Colour",
        "scan_col_color": "Colour",
        "scan_col_gray": "Greyscale",
        "scan_col_bw": "Black and white",
        "scan_dpi": "Resolution",
        "scan_paper": "Paper",
        "scan_pap_auto": "Whole scan area",
        "scan_after": "After scanning",
        "scan_deskew": "Straighten skewed pages",
        "scan_skip_blank": "Skip blank sheets (feeder)",
        "scan_select_all": "Select all",
        "scan_help": "1) Pick a scanner and settings on the left\n2) Scan — pages appear as they are scanned\n3) Click selects a page, drag reorders, ↺ ↻ rotate\n4) Save as one PDF or as JPG images. Pages stay until Clear list",
        "scan_searching": "Looking for scanners…",
        "scan_found": "Scanners found: {n}",
        "scan_no_devices": "No scanners found. Turn the scanner on, check the cable or network and click Refresh list.",
        "scan_pick_device": "Pick a scanner in the list on the left",
        "scan_cancel": "Stop",
        "scan_st_opening": "Connecting to the scanner…",
        "scan_st_waiting_ui": "The scanner's window is open…",
        "scan_st_scanning": "Scanning… images received: {n}",
        "scan_st_scanning0": "Scanning…",
        "scan_page_n": "page {n}",
        "scan_done_n": "Pages scanned: {n}",
        "scan_blank_n": "blank skipped: {n}",
        "scan_straight_n": "straightened: {n}",
        "scan_kept_n": "Pages received so far are kept: {n}",
        "scan_cancelled": "Scanning stopped",
        "scan_blank_skipped": "blank sheet skipped",
        "scan_no_pages_came": "The scanner sent no pages",
        "scan_restored": "Pages from last time are still here: {n}",
        "select_pages_first": "Select pages first — click on them",
        "scan_warn_ui_required": "This scanner only works through its own window — it is open.",
        "scan_warn_no_duplex": "The scanner cannot scan both sides — one side was scanned.",
        "scan_warn_color_substituted": "The scanner does not offer the chosen colour mode — the pages were converted by the program.",
        "scan_warn_resolution_refused": "The scanner refused the resolution — {dpi} dpi was used.",
        "scan_err_helpers_missing": "The scanning component is missing. Reinstall the program.",
        "scan_err_dsm_missing": "The TWAIN manager is missing. Reinstall the program.",
        "scan_err_no_device": "Scanner not found. Check that it is on and connected, then refresh the list.",
        "scan_err_device_busy": "The scanner is busy with another program or user. Close it and try again.",
        "scan_err_device_offline": "The scanner does not respond. Check its power and cable (or network).",
        "scan_err_device_open_failed": "Could not connect to the scanner. Check that it is on, or reinstall its driver.",
        "scan_err_paper_jam": "Paper jam. Remove the sheet and click Scan again.",
        "scan_err_double_feed": "The feeder took several sheets at once. Fan the stack and try again.",
        "scan_err_feeder_empty": "The document feeder is empty.",
        "scan_err_no_feeder": "This scanner has no document feeder — choose Glass as the source.",
        "scan_err_simplex_refused": "The driver did not turn off duplex. Scanning was stopped; check the scanner driver settings.",
        "scan_err_cover_open": "The scanner cover is open.",
        "scan_err_warming_up": "The scanner is warming up — try again in a minute.",
        "scan_err_paper_problem": "Paper problem in the feeder — check the tray.",
        "scan_err_user_intervention": "The scanner needs attention — check its panel.",
        "scan_err_communication": "The connection to the scanner was lost. Check the cable or network.",
        "scan_err_bad_setting": "The scanner refused these settings — try another resolution or colour mode.",
        "scan_err_access_denied": "Access to the scanner was denied.",
        "scan_err_wia_unavailable": "The Windows scanner service (WIA) is not available on this computer.",
        "scan_err_wia_service": "The Windows Image Acquisition (WIA) service is not running. Start it in Windows Services.",
        "scan_err_out_of_memory": "Not enough memory for the page — lower the resolution.",
        "scan_err_driver_error": "The scanner driver reported an error.",
        "scan_err_driver_crashed": "The scanner driver crashed. The program keeps running.",
        "scan_err_timeout": "The scanner stopped responding — scanning was stopped.",
        "scan_err_internal": "Scanning failed — details are in the program log.",
        "scan_pages": "Scanned pages",
        "scan_btn": "Scan",
        "save_pdf": "Save PDF",
        "save_jpg": "Save JPG",
        "compress_doc": "Compress document",
        "folder": "Output folder",
        "refresh_dev": "Refresh list",
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
        "no_pages_scan": "No pages. Click Scan first.",
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
        "err_same_file": "The source and output must have different file names.",
        "err_no_gain": "No real gain — original kept",
        "err_bad_result": "The result failed verification — original kept",
        "err_no_access": "Access denied",
        "err_pymupdf": "PyMuPDF is required",
        "err_no_write": "No write permission for the folder",
        "err_dir": "Folder unavailable",
        "err_add_pdf": "Could not add PDF",
        "err_add_pages": "Could not add any pages",
        "single_pdf_note": "single file → images processed in parallel",
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
        "empty_pages_hint": "Click Scan —\npages will appear here\n\nor drop pictures here",
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

# -------------------- SCANNER --------------------
# Scanning runs in a separate worker process: NexusScan64.exe or NexusScan32.exe
# (scanner/NexusScan, .NET Framework 4.6.2, which is part of Windows 10/11).
#  - Only a 32-bit process can load the 32-bit TWAIN drivers that most office
#    MFPs still install; a 64-bit program cannot see them at all.
#  - WIA runs on the worker's STA thread. The old in-process code called WIA
#    from a plain thread without COM initialised: "CoInitialize has not been
#    called", and the WIA device list was always empty.
#  - A driver that crashes or hangs takes the worker down, never the program
#    and the pages already scanned.
# The worker reports JSON lines; pages arrive as BMP files and are turned into
# final page files here (orientation, blank check, JPEG or 1-bit PNG).

SCAN_SESSION_DIR = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                    / "NexusOptimizer" / "ScanSession")
SCAN_HELPER_LOG = APPDATA_DIR / "scanner.log"
SCAN_DPIS = (150, 200, 300, 400, 600)
SCAN_SOURCES = ("flatbed", "feeder", "duplex")
SCAN_COLORS = ("color", "gray", "bw")
SCAN_PAPERS = ("a4", "letter", "legal", "auto")
SCAN_LIST_TIMEOUT = 30      # s: a driver that hangs while being listed
SCAN_STALL_TIMEOUT = 240    # s without any sign of life while scanning
SCAN_CANCEL_GRACE = 10      # s to stop politely before the worker is killed
SCAN_BLANK_INK = 0.0002     # share of dark pixels below which a page is blank


def scan_helper_dir() -> Optional[Path]:
    """The folder with NexusScan64.exe / NexusScan32.exe and their TWAIN
    managers: inside the build, or scanner/out next to the sources."""
    cands = []
    env = os.environ.get("NEXUS_SCAN_HELPERS")
    if env:
        cands.append(Path(env))
    if is_frozen():
        cands.append(Path(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))) / "scan")
        cands.append(Path(os.path.dirname(sys.executable)) / "scan")
    cands.append(Path(os.path.dirname(os.path.abspath(__file__))) / "scanner" / "out")
    for c in cands:
        if (c / "NexusScan64.exe").is_file():
            return c
    return None


class ScanDevice:
    """A scanner as one of the workers sees it."""

    def __init__(self, ev: dict):
        self.backend = str(ev.get("backend") or "twain")
        self.arch = int(ev.get("arch") or 64)
        self.name = str(ev.get("name") or "").strip() or "Scanner"
        self.manufacturer = str(ev.get("manufacturer") or "").strip()
        self.family = str(ev.get("family") or "").strip()
        # what the worker needs to find the device again
        self.device = str(ev.get("id") or ev.get("name") or "")
        self.key = f"{self.backend}{self.arch}:{self.device}"

    @property
    def kind(self) -> str:
        if self.backend == "wia":
            return "WIA"
        return "TWAIN" if self.arch == 64 else t("scan_kind_twain32")

    @property
    def label(self) -> str:
        return f"{self.name}  ·  {self.kind}"

    def __repr__(self):
        return f"ScanDevice({self.key!r})"


def _helper_exe(helpers: Path, arch: int) -> Path:
    return helpers / ("NexusScan64.exe" if arch == 64 else "NexusScan32.exe")


def _run_helper(args: list, timeout: float) -> List[dict]:
    """Runs a worker command to the end; a hung driver is killed by timeout."""
    events: List[dict] = []
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, creationflags=CNW,
                                startupinfo=_startupinfo(), cwd=os.path.dirname(args[0]))
    except OSError as e:
        return [{"event": "note", "code": "helpers_missing", "detail": str(e)}]
    _register_proc(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        events.append({"event": "note", "code": "timeout",
                       "detail": f"{os.path.basename(args[0])} {args[1]}"})
    finally:
        _unregister_proc(proc)
    for line in (out or b"").splitlines():
        try:
            events.append(json.loads(line.decode("utf-8", "replace")))
        except ValueError:
            continue
    return events


def list_scanners() -> Tuple[List[ScanDevice], List[dict]]:
    """All scanners the workers can reach: WIA, 64-bit and 32-bit TWAIN.

    Both workers are asked at the same time. The duplicates Windows creates
    are dropped: WIA devices re-exposed to 32-bit TWAIN as "WIA-..." sources,
    and a TWAIN driver installed in both bitnesses (the 64-bit one is kept).
    """
    helpers = scan_helper_dir()
    if helpers is None:
        return [], [{"event": "note", "code": "helpers_missing"}]
    results: dict = {}

    def ask(arch, extra):
        exe = _helper_exe(helpers, arch)
        if not exe.is_file():
            results[arch] = [{"event": "note", "code": "helpers_missing",
                              "detail": str(exe)}]
            return
        results[arch] = _run_helper(
            [str(exe), "list", "--log", str(SCAN_HELPER_LOG)] + extra,
            SCAN_LIST_TIMEOUT)

    threads = [threading.Thread(target=ask, args=(64, []), daemon=True),
               threading.Thread(target=ask, args=(32, ["--skip-wia"]), daemon=True)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(SCAN_LIST_TIMEOUT + 5)
    devices, notes = merge_scanner_events(results)
    log.info("scanners: %s; notes: %s", [d.key for d in devices],
             [n.get("code") for n in notes])
    return devices, notes


def merge_scanner_events(results: dict) -> Tuple[List[ScanDevice], List[dict]]:
    """{arch: [worker events]} → (devices without duplicates, notes)."""
    devices: List[ScanDevice] = []
    notes: List[dict] = []
    for arch in (64, 32):
        for ev in results.get(arch, []):
            if ev.get("event") == "device":
                if ev.get("bridge"):
                    continue
                devices.append(ScanDevice(ev))
            elif ev.get("event") in ("note", "error"):
                notes.append(ev)
    twain64 = {d.name.lower() for d in devices if d.backend == "twain" and d.arch == 64}
    uniq, seen = [], set()
    for d in devices:
        if d.backend == "twain" and d.arch == 32 and d.name.lower() in twain64:
            continue
        if d.key in seen:
            continue
        seen.add(d.key)
        uniq.append(d)
    order = {"wia": 0, "twain": 1}
    uniq.sort(key=lambda d: (order.get(d.backend, 2), -d.arch, d.name.lower()))
    return uniq, notes


def wia_service_running() -> Optional[bool]:
    """State of the Windows Image Acquisition service (stisvc); None if unknown."""
    try:
        adv = ctypes.windll.advapi32
        scm = adv.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
        if not scm:
            return None
        try:
            svc = adv.OpenServiceW(scm, "stisvc", 0x0004)  # SERVICE_QUERY_STATUS
            if not svc:
                return None
            try:
                status = (ctypes.c_uint32 * 7)()
                if not adv.QueryServiceStatus(svc, status):
                    return None
                return status[1] == 4  # SERVICE_RUNNING
            finally:
                adv.CloseServiceHandle(svc)
        finally:
            adv.CloseServiceHandle(scm)
    except Exception:
        return None


class ScanJob:
    """One scan in a worker process.

    on_event(dict) is called from a reader thread with every worker event. It
    always ends with exactly one "done" event: when the worker dies (a crashed
    driver), is stopped by the stall watchdog or is killed after a cancel, the
    job reports that itself, so the pages received until then are kept.
    """

    def __init__(self, device: ScanDevice, opts: dict, out_dir: str,
                 parent_hwnd: int, on_event):
        self.device = device
        self.opts = dict(opts)
        self.out_dir = out_dir
        self.parent_hwnd = int(parent_hwnd or 0)
        self.on_event = on_event
        self.proc = None
        self.pages = 0
        self._last = time.time()
        self._state = "starting"
        self._done = False
        self._cancel = False
        self._killed = None  # None | "cancel" | "stall"
        self._lock = threading.Lock()

    def args(self, helpers: Path) -> list:
        o = self.opts
        a = [str(_helper_exe(helpers, self.device.arch)), "scan",
             "--backend", self.device.backend, "--device", self.device.device,
             "--out", self.out_dir,
             "--dpi", str(int(o.get("dpi", 300))),
             "--color", o.get("color", "color"),
             "--source", o.get("source", "flatbed"),
             "--paper", o.get("paper", "a4"),
             "--parent", str(self.parent_hwnd),
             "--log", str(SCAN_HELPER_LOG)]
        if o.get("ui"):
            a.append("--ui")
        if o.get("transfer") == "memory":
            a += ["--transfer", "memory"]
        return a

    def start(self):
        helpers = scan_helper_dir()
        if helpers is None:
            raise FileNotFoundError("NexusScan64.exe")
        try:
            # The driver's window may come to the front: Windows allows that
            # only to processes the foreground program permits.
            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass
        args = self.args(helpers)
        log.info("scan: %s", " ".join(args[1:]))
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, creationflags=CNW,
                                     startupinfo=_startupinfo(), cwd=str(helpers))
        _register_proc(self.proc)
        threading.Thread(target=self._read, daemon=True, name="scan-read").start()
        threading.Thread(target=self._watch, daemon=True, name="scan-watch").start()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def cancel(self):
        """Stop after the current page; kill the worker if it does not listen."""
        with self._lock:
            if self._cancel or not self.running:
                return
            self._cancel = True
        try:
            self.proc.stdin.write(b"cancel\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass
        threading.Timer(SCAN_CANCEL_GRACE, self._kill, args=("cancel",)).start()

    def _kill(self, why: str):
        if self.running:
            self._killed = why
            log.warning("scan worker killed (%s)", why)
            try:
                self.proc.kill()
            except OSError:
                pass

    def _watch(self):
        while self.running:
            time.sleep(1.0)
            # With the driver's window open the user takes their time.
            if self._state == "waiting_ui":
                continue
            if time.time() - self._last > SCAN_STALL_TIMEOUT:
                self._kill("stall")
                return

    def _emit(self, ev: dict):
        try:
            self.on_event(ev)
        except Exception as e:
            _log_exc("scan event handler", e)

    def _read(self):
        proc = self.proc
        try:
            for raw in proc.stdout:
                try:
                    ev = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._last = time.time()
                kind = ev.get("event")
                if kind == "status":
                    self._state = ev.get("code") or self._state
                elif kind == "page":
                    self.pages += 1
                    self._state = "scanning"
                elif kind == "done":
                    self._done = True
                self._emit(ev)
        except Exception as e:
            _log_exc("scan read", e)
        rc = proc.wait()
        _unregister_proc(proc)
        try:
            proc.stdin.close()
        except Exception:
            pass
        if not self._done:
            if self._killed == "cancel":
                self._emit({"event": "done", "pages": self.pages, "cancelled": True})
            else:
                code = "timeout" if self._killed == "stall" else "driver_crashed"
                self._emit({"event": "error", "code": code,
                            "detail": f"worker exit code 0x{rc & 0xFFFFFFFF:08X}"})
                self._emit({"event": "done", "pages": self.pages, "cancelled": False})


# ---- page processing ----

def _otsu_threshold(hist: List[int]) -> int:
    """The grey level that best separates ink from paper (Otsu's method)."""
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = sum_b = 0
    best, first, last = -1.0, 128, 128
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best * (1 + 1e-9):
            best, first, last = between, i, i
        elif between >= best * (1 - 1e-9):
            last = i
    # With no grey levels between ink and paper every threshold in the gap
    # is equally good: take the middle of it, not the edge next to the ink,
    # or faint print would fall on the paper side.
    return (first + last) // 2


def to_black_and_white(im):
    """Greyscale or colour to crisp 1-bit, with the threshold taken from the
    page itself rather than a fixed 128 (pale paper and faint print survive)."""
    g = im.convert("L")
    t_ = _otsu_threshold(g.histogram())
    t_ = max(96, min(208, t_))
    return g.point(lambda v: 255 if v > t_ else 0, mode="1")


def is_blank_page(im) -> bool:
    """A blank sheet: practically no pixels much darker than the paper.

    Counted at full resolution in the middle of the page (the edges carry the
    feeder's shadows). Bleed-through from the other side is only a little
    darker than the paper and does not count; a lone signature does.
    """
    g = im.convert("L")
    w, h = g.size
    mx, my = int(w * 0.06), int(h * 0.06)
    if w - 2 * mx < 10 or h - 2 * my < 10:
        return False
    core = g.crop((mx, my, w - mx, h - my))
    hist = core.histogram()
    total = sum(hist)
    cum, paper = 0, 255
    for v in range(256):
        cum += hist[v]
        if cum * 2 >= total:
            paper = v
            break
    limit = max(0, paper - 80)
    dark = sum(hist[:limit])
    return total > 0 and dark / total < SCAN_BLANK_INK


def detect_skew_angle(im) -> float:
    """The small rotation (degrees, counter-clockwise) that makes text lines
    horizontal; 0.0 when the page has no clear lines.

    The page is reduced to a black-and-white ink mask; for each candidate
    angle the mask is rotated and squeezed into one column of row averages.
    Straight text lines give the sharpest alternation of dark and light rows.
    """
    g = im.convert("L")
    w, h = g.size
    scale = 1100.0 / max(w, h)
    if scale < 1.0:
        g = g.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                     Image.Resampling.BOX)
    t_ = _otsu_threshold(g.histogram())
    ink = g.point(lambda v: 255 if v < t_ else 0)
    W, H = ink.size
    m = int(min(W, H) * 0.05)
    ink = ink.crop((m, m, W - m, H - m))

    def score(angle: float) -> float:
        r = ink.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False, fillcolor=0)
        prof = list(r.resize((1, r.size[1]), Image.Resampling.BOX).getdata())
        return float(sum((prof[i + 1] - prof[i]) ** 2 for i in range(len(prof) - 1)))

    base = score(0.0)
    best_a, best_s = 0.0, base
    for k in range(-10, 11):
        a = k * 0.5
        if a == 0.0:
            continue
        s = score(a)
        if s > best_s:
            best_a, best_s = a, s
    center = best_a
    for k in range(-5, 6):
        a = round(center + k * 0.1, 2)
        if a == center:
            continue
        s = score(a)
        if s > best_s:
            best_a, best_s = a, s
    if base <= 0 or best_s < base * 1.06:
        return 0.0
    return best_a


def deskew_image(im):
    """Straightens a slightly rotated page. Returns (image, angle)."""
    angle = detect_skew_angle(im)
    if abs(angle) < 0.2:
        return im, 0.0
    if im.mode == "1":
        g = im.convert("L").rotate(angle, resample=Image.Resampling.BICUBIC,
                                   expand=False, fillcolor=255)
        return g.point(lambda v: 255 if v > 127 else 0, mode="1"), angle
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    fill = 255 if im.mode == "L" else (255, 255, 255)
    return im.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False,
                     fillcolor=fill), angle


def save_page_image(im, path_base: str, dpi: float) -> str:
    """Final page file: 1-bit pages as PNG (lossless and tiny), the rest as a
    high-quality JPEG without colour subsampling (stamps and blue signatures
    keep their edges)."""
    d = (round(dpi), round(dpi)) if dpi and dpi > 0 else None
    if im.mode == "1":
        path = path_base + ".png"
        im.save(path, "PNG", optimize=True, **({"dpi": d} if d else {}))
        return path
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    path = path_base + ".jpg"
    extra = {"subsampling": 0} if im.mode == "RGB" else {}
    im.save(path, "JPEG", quality=90, optimize=True, **extra, **({"dpi": d} if d else {}))
    return path


def finalize_scan_page(src: str, dst_dir: Path, want_color: str, deskew: bool,
                       skip_blank: bool, dpi_hint: float = 0) -> dict:
    """A page from the worker (BMP or the driver's format) to a page file.

    Returns {"path", "blank", "angle"}. The source file is always deleted.
    The colour mode is enforced here as well: a driver that refused the
    requested mode delivers a richer one, which is converted down.
    """
    try:
        with Image.open(src) as im0:
            im0.load()
            info_dpi = im0.info.get("dpi")
            im = ImageOps.exif_transpose(im0)
        dpi = 0.0
        try:
            if info_dpi and float(info_dpi[0]) >= 50:
                dpi = float(info_dpi[0])
        except (TypeError, ValueError):
            dpi = 0.0
        if not dpi:
            dpi = float(dpi_hint or 0)
        if im.mode in ("P", "PA"):
            im = im.convert("RGB")
        if im.mode in ("RGBA", "LA", "CMYK", "I", "I;16", "F"):
            im = im.convert("RGB")
        if want_color == "gray" and im.mode != "L":
            im = im.convert("L")
        elif want_color == "bw" and im.mode != "1":
            im = to_black_and_white(im)
        if skip_blank and is_blank_page(im):
            return {"path": None, "blank": True, "angle": 0.0}
        angle = 0.0
        if deskew:
            im, angle = deskew_image(im)
        dst_dir.mkdir(parents=True, exist_ok=True)
        path = save_page_image(im, str(dst_dir / f"page_{time.time_ns()}"), dpi)
        return {"path": path, "blank": False, "angle": angle}
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def rotate_page_file(path: str, degrees: int, dst_dir: Path) -> str:
    """A page turned by 90/180/270 degrees (counter-clockwise), as a new file.

    A page that belongs to the scan session is replaced; a picture that was
    dragged in from elsewhere is never modified - the turned copy goes into
    the session folder instead.
    """
    method = {90: Image.Transpose.ROTATE_90, 180: Image.Transpose.ROTATE_180,
              270: Image.Transpose.ROTATE_270}[degrees % 360]
    with Image.open(path) as im0:
        im0.load()
        info_dpi = im0.info.get("dpi")
        im = ImageOps.exif_transpose(im0).transpose(method)
    dpi = 0.0
    try:
        dpi = float(info_dpi[0]) if info_dpi else 0.0
    except (TypeError, ValueError):
        dpi = 0.0
    if im.mode not in ("1", "L", "RGB"):
        im = im.convert("RGB")
    dst_dir.mkdir(parents=True, exist_ok=True)
    new = save_page_image(im, str(dst_dir / f"page_{time.time_ns()}"), dpi)
    if ScanSessionStore.owns(path):
        try:
            os.unlink(path)
        except OSError:
            pass
    return new


class ScanSessionStore:
    """Scanned pages live in %LOCALAPPDATA%\\NexusOptimizer\\ScanSession with
    a manifest of their order. They survive switching sections, closing the
    program and a crash: a 40-page feeder run is never lost to a misclick."""

    DIR = SCAN_SESSION_DIR
    FILE = SCAN_SESSION_DIR / "pages.json"

    @classmethod
    def load(cls) -> List[str]:
        try:
            with open(cls.FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        pages = []
        for p in data.get("pages", []):
            p = str(p)
            if not os.path.isabs(p):
                p = str(cls.DIR / p)
            if os.path.isfile(p):
                pages.append(p)
        return pages

    @classmethod
    def save(cls, pages: List[str]):
        try:
            cls.DIR.mkdir(parents=True, exist_ok=True)
            items = [os.path.basename(p) if cls.owns(p) else p for p in pages]
            tmp = str(cls.FILE) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "pages": items}, f, ensure_ascii=False)
            os.replace(tmp, cls.FILE)
        except OSError as e:
            _log_exc("scan session save", e)

    @classmethod
    def owns(cls, path: str) -> bool:
        try:
            return os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
                os.path.normcase(str(cls.DIR))
        except Exception:
            return False

    @classmethod
    def sweep(cls, keep: List[str]):
        """Deletes session files no longer in the list (left by a crash)."""
        keep_set = {os.path.normcase(os.path.abspath(p)) for p in keep}
        try:
            for f in cls.DIR.glob("page_*"):
                if os.path.normcase(str(f)) not in keep_set:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass


# -------------------- PDF --------------------
#
# Сжатие PDF — собственный конвейер по изображениям (тот же подход, что в
# NexusPDF 0.24). Раньше изображения отдавались rewrite_images самого MuPDF,
# и на корпусе типичных офисных сканов он делал три вещи неправильно:
#  - уменьшал только ВДВОЕ, а не до цели: скан 300 dpi при цели 100 выходил
#    150 dpi, а при цели 165 не уменьшался вовсе — весь «выигрыш» давало
#    низкое качество JPEG, отсюда мыло без экономии;
#  - серые и CMYK-сканы портил: серый перепривязывался к чужому цветовому
#    пространству (двоящаяся каша), CMYK становился чёрным прямоугольником;
#  - 1-битные сканы (факсы, ч/б режим МФУ) уменьшал до 150 dpi — буквы
#    рассыпались.
# Теперь каждое изображение решается отдельно, и у каждого решения есть
# откат. Текстовый слой OCR, закладки, ссылки и формы не трогаются: меняются
# только потоки изображений.

PDF_IMG_WORTH = 0.85            # замена: новый поток хотя бы на 15 % меньше
PDF_FILE_WORTH = 0.985          # файл: хотя бы на 1,5 % меньше, иначе исходник
PDF_DOWN_TRIGGER = 1.30         # плотнее цели на треть — уменьшаем до цели
PDF_SCAN_COVER = 0.80           # картинка на 80 % листа — страница-скан
PDF_MIN_STREAM = 2048           # мелочь не окупает ни риска, ни времени
PDF_INFLIGHT_BYTES = 320 << 20  # пиксели «в работе» одновременно (офисные ПК)


def _placement_size(info: dict) -> Tuple[float, float]:
    """Показанный размер изображения в пунктах — из матрицы размещения.
    bbox у повёрнутых картинок врёт: это описанный прямоугольник, и скан,
    вставленный «на боку», выглядел бы в полтора раза плотнее."""
    try:
        a, b, c, d = info["transform"][:4]
        return math.hypot(a, b), math.hypot(c, d)
    except Exception:
        bb = info.get("bbox") or (0, 0, 0, 0)
        return abs(bb[2] - bb[0]), abs(bb[3] - bb[1])


def analyze_pdf(path: str) -> dict:
    """Профиль документа для пресета «умный»: похож ли он на скан и какой у
    сканов фактический DPI. Скан узнаётся по картинке на весь лист, а не по
    отсутствию текста: у скана после OCR текста полно (невидимого)."""
    info = {"pages": 0, "images": 0, "text_len": 0, "is_scanned_like": False,
            "avg_image_dpi": 0.0, "scan_pages": 0, "encrypted": False, "error": ""}
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
        sample = min(info["pages"], 12)
        text_len = image_count = scan_pages = 0
        dpis: List[float] = []
        for i in range(sample):
            page = doc[i]
            try:
                text_len += len(page.get_text("text") or "")
            except Exception:
                pass
            try:
                prect = page.rect
                area = max(1.0, abs(prect))
                best = 0.0
                # get_image_info: размещения БЕЗ декодирования пикселей
                for im in page.get_image_info():
                    image_count += 1
                    bb = fitz.Rect(im.get("bbox") or (0, 0, 0, 0)) & prect
                    cover = 0.0 if bb.is_empty else abs(bb) / area
                    best = max(best, cover)
                    sw, sh = _placement_size(im)
                    w, h = im.get("width") or 0, im.get("height") or 0
                    if cover >= 0.5 and sw > 1 and sh > 1 and w and h:
                        dpis.append(min(w / (sw / 72.0), h / (sh / 72.0)))
                if best >= PDF_SCAN_COVER:
                    scan_pages += 1
            except Exception:
                pass
        info["images"] = image_count
        info["text_len"] = text_len
        info["scan_pages"] = scan_pages
        if dpis:
            dpis.sort()
            info["avg_image_dpi"] = dpis[len(dpis) // 2]  # медиана
        sampled = max(1, sample)
        by_cover = scan_pages > 0 and scan_pages * 2 >= sampled
        by_text = image_count >= max(1, sampled // 2) and text_len < 40 * sampled
        info["is_scanned_like"] = by_cover or by_text
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
    """Пресет → конкретные (dpi, quality, structure_only).

    Числа рассчитаны на честное уменьшение до цели: основной выигрыш даёт
    удаление лишних пикселей, поэтому качество JPEG держится человеческим.
    Прежние 34–56 были вынужденными: движок не уменьшал, и экономию выжимали
    из качества — документы выходили мыльными."""
    structure_only = False
    if preset == "smart":
        if info.get("is_scanned_like"):
            # Скан плотнее 160 dpi: 130 dpi на экране и в офисной печати не
            # отличить, а весит вдвое меньше. Скан около 150 dpi уже в цели —
            # уменьшать его не из чего, и это честно.
            dpi, quality = (130, 68) if info.get("avg_image_dpi", 0) > 160 else (150, 70)
        else:
            dpi, quality = 150, 76
    elif preset == "aggressive":
        dpi, quality = 96, 45
    elif preset == "max":
        dpi, quality = 85, 40
    elif preset == "balanced":
        dpi, quality = 120, 60
    elif preset == "quality":
        dpi, quality = 220, 85
    elif preset == "structure":
        structure_only = True
        dpi, quality = 130, 60
    elif preset == "images":
        dpi, quality = 100, 50
    else:  # custom
        dpi = max(50, min(300, safe_int(dpi, 130)))
        quality = max(20, min(95, safe_int(quality, 55)))
    return dpi, quality, structure_only


def _pdf_stream_len(doc, xref: int) -> int:
    """Длина сжатого потока изображения (по словарю, без чтения данных)."""
    try:
        kind, val = doc.xref_get_key(xref, "Length")
        if kind == "int":
            return int(val)
        if kind == "xref":
            return int(doc.xref_object(int(val.split()[0]), compressed=True).strip())
    except Exception:
        pass
    try:
        return len(doc.xref_stream_raw(xref) or b"")
    except Exception:
        return 0


def _pdf_obj_text(doc, value: str, depth: int = 2) -> str:
    """Текст значения и объектов, на которые оно ссылается: у ICCBased число
    компонентов лежит в словаре профиля, у Indexed база — отдельным объектом."""
    parts = [value or ""]
    if depth > 0:
        for num in re.findall(r"(\d+) \d+ R", value or ""):
            try:
                parts.append(_pdf_obj_text(
                    doc, doc.xref_object(int(num), compressed=True) or "", depth - 1))
            except Exception:
                pass
    return " ".join(parts)


def _pdf_mask_is_opaque(doc, value: str) -> bool:
    """Only an ordinary, entirely white 8-bit mask can be discarded safely.
    Read from the inspection document: stream reads can change MuPDF caches."""
    try:
        xref = int(value.split()[0])
        if doc.xref_get_key(xref, "BitsPerComponent") != ("int", "8"):
            return False
        if doc.xref_get_key(xref, "ColorSpace") != ("name", "/DeviceGray"):
            return False
        if any(doc.xref_get_key(xref, k)[0] != "null" for k in ("Decode", "Mask", "SMask")):
            return False
        width = int(doc.xref_get_key(xref, "Width")[1])
        height = int(doc.xref_get_key(xref, "Height")[1])
        if not 0 < width * height <= 40_000_000:
            return False
        pixels = doc.xref_stream(xref)
        return len(pixels) == width * height and pixels.count(255) == len(pixels)
    except Exception:
        return False


def _pdf_image_meta(doc, xref: int) -> Optional[dict]:
    """Ключи изображения без декодирования. None — изображение не трогать."""
    def key(name):
        try:
            return doc.xref_get_key(xref, name)
        except Exception:
            return ("null", "null")

    # Прозрачность (мягкая маска, цветовой ключ, трафарет) JPEG испортит.
    mask_kind, mask_value = key("SMask")
    opaque_mask = mask_kind == "xref" and _pdf_mask_is_opaque(doc, mask_value)
    if (mask_kind != "null" and not opaque_mask) or key("Mask")[0] != "null":
        return None
    if key("ImageMask")[1] == "true":
        return None
    filt = key("Filter")[1]
    # CCITT/JBIG2 плотнее любого JPEG, а JPX перекодировать — риск без выгоды.
    if any(f in filt for f in ("CCITT", "JBIG2", "JPX")):
        return None
    kind, bpc = key("BitsPerComponent")
    if kind == "int" and int(bpc) <= 1:
        return None  # 1-битные сканы: уменьшение рассыпает буквы
    cs_kind, cs_val = key("ColorSpace")
    cs_text = _pdf_obj_text(doc, cs_val)
    # Плашечные цвета и Lab: перевод в RGB меняет вид документа.
    if any(s in cs_text for s in ("/Separation", "/DeviceN", "/Lab", "/Pattern")):
        return None
    indexed = "/Indexed" in cs_text
    comps = 0
    if not indexed:
        if "/DeviceGray" in cs_text or "/CalGray" in cs_text:
            comps = 1
        elif "/DeviceRGB" in cs_text or "/CalRGB" in cs_text:
            comps = 3
        elif "/DeviceCMYK" in cs_text:
            comps = 4
        m = re.search(r"/N (\d)", cs_text)
        if "/ICCBased" in cs_text and m:
            comps = int(m.group(1))
    return {
        "jpeg": "DCT" in filt,
        "cs_val": cs_val if cs_kind in ("xref", "array", "name") else "",
        "icc": "/ICCBased" in cs_text and not indexed,
        "comps": comps,
        "length": _pdf_stream_len(doc, xref),
        "opaque_mask": opaque_mask,
    }


def _pdf_collect_plan(doc) -> dict:
    """Collect actual image references and their largest displayed dimensions.
    Pixel hashes used by PyMuPDF resolve objects correctly even when several
    images have the same dimensions and compressed length. This document is
    for inspection only; its native caches are never saved."""
    plan: dict = {}
    for pno in range(doc.page_count):
        if stop_flag.is_set():
            break
        for info in doc[pno].get_image_info(xrefs=True):
            xref = info.get("xref", 0)
            if xref <= 0:
                continue  # inline images have no replaceable object
            sw, sh = _placement_size(info)
            entry = plan.setdefault(xref, {"w": 0.0, "h": 0.0, "page": pno})
            entry["w"] = max(entry["w"], sw)
            entry["h"] = max(entry["h"], sh)
    return plan


def _pdf_looks_graphic(img) -> bool:
    """Мало различных цветов — схема, график, скриншот. Такие картинки
    остаются без потерь: JPEG даёт ореолы вокруг линий и букв."""
    w, h = img.size
    step = max(1, max(w, h) // 256)
    small = img.resize((max(1, w // step), max(1, h // step)),
                       Image.Resampling.NEAREST)
    return small.getcolors(24 if small.mode == "L" else 256) is not None


def _pdf_encode_job(job: dict):
    """Уменьшение и JPEG — без MuPDF, поэтому можно в пуле потоков (Pillow
    отпускает GIL на ресэмплинге и кодировании). None — оставить как было."""
    mode = "L" if job["n"] == 1 else "RGB"
    img = Image.frombytes(mode, (job["w"], job["h"]), job.pop("samples"))
    if job["graphic_check"] and _pdf_looks_graphic(img):
        return None
    if job["gray"] and img.mode != "L":
        img = img.convert("L")
    size = (job["new_w"], job["new_h"])
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=job["quality"], optimize=True)
    data = buf.getvalue()
    if len(data) >= job["old_len"] * PDF_IMG_WORTH:
        return None  # выигрыш не стоит потери качества
    return data, img.mode, img.size[0], img.size[1]


def _pdf_prepare_image(doc, xref: int, place: dict, target_dpi: int,
                       quality: int, force_gray: bool) -> Optional[dict]:
    """Решение по одному изображению и декодирование (только MuPDF-поток)."""
    meta = _pdf_image_meta(doc, xref)
    if meta is None or meta["length"] <= PDF_MIN_STREAM:
        return None
    try:
        width = int(doc.xref_get_key(xref, "Width")[1])
        height = int(doc.xref_get_key(xref, "Height")[1])
    except Exception:
        return None
    if width < 16 or height < 16:
        return None
    sx = sy = 1.0
    if place["w"] > 1 and place["h"] > 1:
        dpi_x = width / (place["w"] / 72.0)
        dpi_y = height / (place["h"] / 72.0)
        if max(dpi_x, dpi_y) > target_dpi * PDF_DOWN_TRIGGER:
            # каждая ось — к цели отдельно: растянутый скан (факс 204×98)
            # не теряет плотность по менее плотной оси
            sx = min(1.0, target_dpi / dpi_x)
            sy = min(1.0, target_dpi / dpi_y)
    shrink = sx < 0.999 or sy < 0.999
    gray = force_gray and meta["comps"] != 1
    # Quality applies even at the target DPI. The encoded-size gate below
    # retains already compact JPEGs without another generation of losses.
    pix = fitz.Pixmap(doc, xref)  # MuPDF сам применит Decode, Indexed, ICC
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    converted = False
    if pix.n >= 4:
        # CMYK → RGB: четырёхкомпонентный JPEG читают не все программы
        pix = fitz.Pixmap(fitz.csRGB, pix)
        converted = True
    if pix.n not in (1, 3):
        return None
    if pix.n == 1:
        gray = False
    new_w = max(16, int(round(width * sx)))
    new_h = max(16, int(round(height * sy)))
    # Грубое уменьшение кратно двум — средствами MuPDF (меньше памяти),
    # последние «меньше двух раз» — Lanczos, чтобы буквы остались чёткими.
    ratio = min(new_w / pix.width, new_h / pix.height)
    k = 0
    while k < 4 and ratio * (2 ** (k + 1)) <= 0.5:
        k += 1
    if k:
        pix.shrink(k)
    job = {
        "samples": pix.samples, "w": pix.width, "h": pix.height, "n": pix.n,
        "new_w": new_w, "new_h": new_h, "gray": gray, "quality": quality,
        "graphic_check": not meta["jpeg"], "old_len": meta["length"],
        # собственный ICC-профиль сохраняем, если каналы не менялись
        "keep_cs": meta["cs_val"] if meta["icc"] and not converted and not gray else "",
        "opaque_mask": meta["opaque_mask"],
    }
    del pix
    return job


def _pdf_apply_image(doc, xref: int, data: bytes, mode: str, w: int, h: int,
                     keep_cs: str, opaque_mask: bool = False):
    """Подмена потока на месте: все ссылки (страницы, формы) остаются."""
    doc.update_stream(xref, data, compress=0)  # снимает Filter и DecodeParms
    doc.xref_set_key(xref, "Filter", "/DCTDecode")
    doc.xref_set_key(xref, "Width", str(w))
    doc.xref_set_key(xref, "Height", str(h))
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "ColorSpace", keep_cs or (
        "/DeviceGray" if mode == "L" else "/DeviceRGB"))
    # пиксели уже декодированы: старая инверсия/параметры фильтра неверны
    doc.xref_set_key(xref, "Decode", "null")
    doc.xref_set_key(xref, "DecodeParms", "null")
    if opaque_mask:
        doc.xref_set_key(xref, "SMask", "null")


def _pdf_recompress_images(doc, target_dpi: int, quality: int, force_gray: bool,
                           threads: int = 1, progress=None, inspection=None) -> Tuple[int, int]:
    """Пер-картиночный конвейер. Возвращает (заменено, оставлено как было).
    MuPDF-вызовы — только в этом потоке; уменьшение и кодирование уходят в
    пул, а объём пикселей «в работе» ограничен памятью офисного ПК."""
    from collections import deque

    if inspection is None:
        # A separate instance also works for unsaved documents used by callers.
        with fitz.open(stream=doc.tobytes(), filetype="pdf") as reader:
            return _pdf_recompress_images(doc, target_dpi, quality, force_gray,
                                          threads, progress, reader)
    plan = _pdf_collect_plan(inspection)
    items = sorted(plan.items(), key=lambda kv: kv[1]["page"])
    total = max(1, len(items))
    state = {"done": 0, "replaced": 0, "kept": 0, "inflight": 0}
    pool = ThreadPoolExecutor(max_workers=threads) if threads > 1 else None
    pending = deque()

    def finish(xref, result, size, keep_cs, opaque_mask=False):
        state["inflight"] -= size
        if result:
            # A failure here may leave the object's dictionary half changed.
            # Abort the entire staged output; never save a partial replacement.
            _pdf_apply_image(doc, xref, *result, keep_cs, opaque_mask)
            state["replaced"] += 1
        else:
            state["kept"] += 1
        state["done"] += 1
        if progress:
            progress(state["done"] / total)

    def drain_one():
        xref, fut, size, keep_cs, opaque_mask = pending.popleft()
        try:
            res = fut.result()
        except Exception as e:
            _log_exc(f"pdf image encode {xref}", e)
            res = None
        finish(xref, res, size, keep_cs, opaque_mask)

    try:
        for xref, place in items:
            if stop_flag.is_set():
                break
            try:
                job = _pdf_prepare_image(inspection, xref, place, target_dpi, quality,
                                         force_gray)
            except Exception as e:
                _log_exc(f"pdf image prepare {xref}", e)
                job = None
            if job is None:
                finish(xref, None, 0, "")
                continue
            keep_cs = job.pop("keep_cs")
            opaque_mask = job.pop("opaque_mask")
            size = len(job["samples"])
            state["inflight"] += size
            if pool is None:
                try:
                    res = _pdf_encode_job(job)
                except Exception as e:
                    _log_exc(f"pdf image encode {xref}", e)
                    res = None
                finish(xref, res, size, keep_cs, opaque_mask)
                continue
            while pending and (len(pending) >= threads * 2 or
                               state["inflight"] > PDF_INFLIGHT_BYTES):
                drain_one()
            pending.append((xref, pool.submit(_pdf_encode_job, job), size, keep_cs, opaque_mask))
        while pending:
            drain_one()
    finally:
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
    return state["replaced"], state["kept"]


def _pdf_output_ok(path: str, pages_expected: int, source: str = None) -> bool:
    """Validate pages, live image streams and searchable content after saving.
    A page count alone accepts PDFs whose picture streams have disappeared."""
    try:
        with fitz.open(path) as result:
            if result.page_count != pages_expected:
                return False
            for page in result:
                for image in page.get_images(full=True):
                    if not result.xref_stream_raw(image[0]):
                        return False
            if source is not None:
                with fitz.open(source) as original:
                    if original.get_toc() != result.get_toc():
                        return False
                    for index in range(pages_expected):
                        before, after = original[index], result[index]
                        if before.get_text() != after.get_text():
                            return False
                        if len(before.get_links()) != len(after.get_links()):
                            return False
                        if before.read_contents() and not after.read_contents():
                            return False
                        # Rendering both files catches an otherwise valid blank
                        # or black page after a failed image rewrite.
                        a = before.get_pixmap(dpi=36, colorspace=fitz.csGRAY, alpha=False).samples
                        b = after.get_pixmap(dpi=36, colorspace=fitz.csGRAY, alpha=False).samples
                        if a and b and max(a) - min(a) >= 16 and max(b) - min(b) < 4:
                            return False
            return True
    except Exception as e:
        _log_exc("pdf validation", e)
        return False


def _pdf_same_file(inp: str, out: str) -> bool:
    if os.path.normcase(os.path.realpath(inp)) == os.path.normcase(os.path.realpath(out)):
        return True
    try:
        return os.path.samefile(inp, out)
    except OSError:
        return False


def process_pdf(inp: str, out: str, preset: str, dpi: int, quality: int, gray: bool,
                _resolved: dict = None, progress_cb=None, threads: int = 1):
    """
    Сжатие одного PDF. progress_cb(0..1) — прогресс по стадиям.
    _resolved — заранее рассчитанные параметры (без повторного анализа).
    threads — потоки на уменьшение и кодирование изображений внутри
    документа (для одиночного большого файла; в пакете — 1, там параллельны
    сами файлы).
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
    tmp = candidate = None
    try:
        if not os.path.isfile(inp):
            return inp, False, "err_not_found", 0, 0
        orig = os.path.getsize(inp)
        if _pdf_same_file(inp, out):
            return inp, False, "err_same_file", orig, orig
        if stop_flag.is_set():
            return inp, False, "stopped", orig, 0
        if not ensure_disk_space(out, int(orig * 1.2)):
            return inp, False, "err_no_space", orig, 0
        cb(0.04)

        if _resolved:
            rdpi = _resolved["dpi"]
            rq = _resolved["quality"]
            structure_only = _resolved["structure_only"]
            use_gray = _resolved.get("use_gray", _resolved.get("use_gray_raster", False))
        else:
            info = analyze_pdf(inp)
            if info.get("encrypted"):
                return inp, False, "err_protected", orig, 0
            rdpi, rq, structure_only = resolve_pdf_params(preset, dpi, quality, info)
            # Серый — для сканов: у вёрстки цвет не трогаем, только жмём
            # картинки чуть сильнее.
            use_gray = bool(gray) and bool(info.get("is_scanned_like")) \
                and not structure_only
            if gray and not use_gray and not structure_only:
                rq = max(28, rq - 8)

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(TEMP_DIR))
        os.close(fd)
        cb(0.08)

        doc = fitz.open(inp)
        try:
            pages_before = doc.page_count
            if not structure_only:
                with fitz.open(inp) as inspection:
                    _pdf_recompress_images(doc, rdpi, rq, use_gray,
                                           threads=max(1, int(threads or 1)),
                                           progress=lambda f: cb(0.08 + 0.72 * f),
                                           inspection=inspection)
                if stop_flag.is_set():
                    return inp, False, "stopped", orig, 0
            cb(0.80)
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

        fd, candidate = tempfile.mkstemp(prefix=".nexus-", suffix=".pdf",
                                           dir=os.path.dirname(os.path.abspath(out)))
        os.close(fd)
        if pikepdf:
            try:
                with pikepdf.open(tmp) as pdf:
                    try:
                        if "/Metadata" in pdf.Root:
                            del pdf.Root.Metadata
                    except Exception:
                        pass
                    pdf.save(candidate, compress_streams=True,
                             object_stream_mode=pikepdf.ObjectStreamMode.generate,
                             recompress_flate=True)
            except Exception as e:
                _log_exc("pikepdf pass", e)
                shutil.copy2(tmp, candidate)
        else:
            shutil.copy2(tmp, candidate)
        cb(0.95)

        if not _pdf_output_ok(candidate, pages_before, inp):
            try:
                os.unlink(candidate)
            except Exception:
                pass
            return inp, False, "err_bad_result", orig, 0

        new = os.path.getsize(candidate)
        if new >= orig * PDF_FILE_WORTH:
            try:
                os.unlink(candidate)
            except Exception:
                pass
            return inp, False, "err_no_gain", orig, orig
        if stop_flag.is_set():
            return inp, False, "stopped", orig, 0
        os.replace(candidate, out)
        candidate = None
        cb(1.0)
        return inp, True, "OK", orig, new
    except PermissionError as e:
        _log_exc("process_pdf perm", e)
        return inp, False, "err_no_access", orig, 0
    except Exception as e:
        _log_exc("process_pdf", e)
        return inp, False, str(e)[:120], orig, 0
    finally:
        for staged in (tmp, candidate):
            if staged and os.path.exists(staged):
                try:
                    os.unlink(staged)
                except Exception:
                    pass


def process_single_pdf_parallel(inp: str, out: str, preset: str, dpi: int,
                                quality: int, gray: bool, workers: int = 0,
                                progress_cb=None) -> Tuple[str, bool, str, int, int]:
    """
    Один (большой) PDF. Документ больше НЕ режется на куски: прежняя нарезка
    со склейкой теряла закладки, формы и общие ресурсы, а одинаковые картинки
    разных кусков решались по-разному. Параллелится тяжёлая часть —
    уменьшение и кодирование изображений — в пуле потоков внутри документа.
    progress_cb(done, total, text) — как у прежней версии.
    """
    workers = max(1, min(int(workers or MAX_WORKERS), 8))

    def cb(frac):
        if progress_cb:
            progress_cb(frac, 1.0, f"{int(frac * 100)}%")

    return process_pdf(inp, out, preset, dpi, quality, gray,
                       progress_cb=cb, threads=workers)


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
    elif name in ("rot_l", "rot_r"):  # поворот страницы: дуга со стрелкой
        import math as _m
        cx = cy = 32
        r = 19
        # дуга по часовой от 300° до 210°: открыта сверху слева
        d.arc([cx - r, cy - r, cx + r, cy + r], start=300, end=210, fill=c, width=w)
        a = _m.radians(300)
        ex, ey = cx + r * _m.cos(a), cy + r * _m.sin(a)
        ux, uy = _m.sin(a), -_m.cos(a)          # движение против часовой
        px, py = -uy, ux
        tip = (ex + ux * 11, ey + uy * 11)
        d.polygon([tip, (ex + px * 9 - ux * 2, ey + py * 9 - uy * 2),
                   (ex - px * 9 - ux * 2, ey - py * 9 - uy * 2)], fill=c)
        if name == "rot_r":
            im = ImageOps.mirror(im)
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
                else:
                    self._on_msg(item)
        except queue.Empty:
            pass
        except Exception as e:
            _log_exc("poll", e)
        self._poll_id = self.after(60, self._poll)

    def _on_msg(self, item):
        """Сообщения, которые понимает только конкретный раздел."""

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
    Сканер: выбор устройства → свои настройки или окно драйвера → страницы
    появляются по одной, пока сканер их отдаёт → поворот, порядок, удаление →
    PDF или JPG. Само сканирование идёт в отдельном процессе (ScanJob), так
    что упавший драйвер не уносит программу и уже полученные страницы.
    """
    EXTS = IMAGE_EXTS
    EMPTY_KEY = "no_pages_scan"
    MODE = "Scan"

    OPT_W = 196  # ширина выпадающих списков в левой колонке

    def __init__(self, app):
        self.devices: List[ScanDevice] = []
        self.pages: List[str] = ScanSessionStore.load()
        ScanSessionStore.sweep(self.pages)
        self._job: Optional[ScanJob] = None
        self._job_info: dict = {}
        self._pages_q: queue.Queue = queue.Queue()
        self._listing = False
        super().__init__(app, "scanner")
        threading.Thread(target=self._page_worker, daemon=True,
                         name="scan-pages").start()
        self._refresh_pages_ui()
        if self.pages:
            self._log(t("scan_restored").format(n=len(self.pages)))
        self._refresh_scanners()

    # ---------------------------------------------------------------- UI

    def _build(self):
        self._build_header(t("scanner"))
        body = ctk.CTkFrame(self, fg_color="transparent")
        # body упаковывается ПОСЛЕ нижних панелей — кнопки всегда видны

        left = ctk.CTkScrollableFrame(body, corner_radius=10, width=320)
        left.pack(side="left", fill="y", padx=(0, 6))

        def head(text, top=10):
            ctk.CTkLabel(left, text=text, font=ctk.CTkFont(weight="bold")).pack(
                anchor="w", padx=10, pady=(top, 4))

        head(t("scanner"), 6)
        self.dev_list = tk.Listbox(left, height=4, font=("Segoe UI", 12),
                                   relief="flat", highlightthickness=0,
                                   activestyle="none", exportselection=False)
        style_listbox(self.dev_list)
        self._lbs.append(self.dev_list)
        self.dev_list.pack(fill="x", padx=10, pady=2)
        self.dev_list.bind("<<ListboxSelect>>", lambda _e: self._remember_device())
        self.refresh_btn = btn_ghost(left, text=t("refresh_dev"),
                                     command=self._refresh_scanners)
        self.refresh_btn.pack(fill="x", padx=10, pady=(4, 2))
        self.dev_note = ctk.CTkLabel(left, text="", text_color=CLR_MUTED,
                                     justify="left", wraplength=280,
                                     font=ctk.CTkFont(size=12))
        self.dev_note.pack(anchor="w", padx=10, pady=(0, 2))

        head(t("scan_settings"))
        self.ui_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(left, text=t("scan_use_ui"), variable=self.ui_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER,
                        command=self._toggle_ui_mode).pack(anchor="w", padx=10,
                                                           pady=(0, 4))
        # подсказка и поля живут в одном контейнере: переключение не
        # сдвигает остальные блоки колонки
        mode_box = ctk.CTkFrame(left, fg_color="transparent")
        mode_box.pack(fill="x")
        self.ui_hint = ctk.CTkLabel(mode_box, text=t("scan_hint_ui"),
                                    text_color=CLR_MUTED, justify="left",
                                    wraplength=280, font=ctk.CTkFont(size=12))
        self.opts_frame = ctk.CTkFrame(mode_box, fg_color="transparent")
        self.opts_frame.pack(fill="x", padx=6, pady=0)

        self._src_map = {t("scan_src_flatbed"): "flatbed",
                         t("scan_src_feeder"): "feeder",
                         t("scan_src_duplex"): "duplex"}
        self._col_map = {t("scan_col_color"): "color",
                         t("scan_col_gray"): "gray",
                         t("scan_col_bw"): "bw"}
        self._dpi_map = {f"{d} dpi": d for d in SCAN_DPIS}
        self._pap_map = {"A4": "a4", "Letter": "letter", "Legal": "legal",
                         t("scan_pap_auto"): "auto"}
        self.src_var = ctk.StringVar(value=t("scan_src_flatbed"))
        self.col_var = ctk.StringVar(value=t("scan_col_color"))
        self.dpi_var = ctk.StringVar(value="300 dpi")
        self.pap_var = ctk.StringVar(value="A4")
        for label, var, mapping in ((t("scan_source"), self.src_var, self._src_map),
                                    (t("scan_color"), self.col_var, self._col_map),
                                    (t("scan_dpi"), self.dpi_var, self._dpi_map),
                                    (t("scan_paper"), self.pap_var, self._pap_map)):
            row = ctk.CTkFrame(self.opts_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=3)
            ctk.CTkLabel(row, text=label, anchor="w", width=88).pack(side="left")
            # dynamic_resizing=False: все списки одной ширины, длинное
            # «Автоподатчик, 2 стороны» не выпирает влево
            ctk.CTkOptionMenu(row, variable=var, values=list(mapping.keys()),
                              width=self.OPT_W, dynamic_resizing=False,
                              fg_color=CLR_GHOST,
                              button_color=CLR_GHOST_HOVER,
                              button_hover_color=CLR_ACC,
                              text_color=CLR_GHOST_TEXT).pack(side="right")

        head(t("scan_after"))
        self.deskew_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left, text=t("scan_deskew"), variable=self.deskew_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            anchor="w", padx=10, pady=2)
        self.blank_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(left, text=t("scan_skip_blank"), variable=self.blank_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            anchor="w", padx=10, pady=2)

        head(t("folder"), 12)
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

        head(t("comp_settings"))
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
        self.comp_q = ctk.StringVar(value="68")
        ctk.CTkEntry(r3, textvariable=self.comp_q, width=50).pack(side="left")
        self.gray_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.comp_frame, text=t("gray_pdf"),
                        variable=self.gray_var,
                        fg_color=CLR_ACC, hover_color=CLR_ACC_HOVER).pack(
            anchor="w", padx=4, pady=(4, 8))

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
        btn_ghost(br, text="", image=icon("rot_l", 20), width=44,
                  command=lambda: self._rotate_selected(90)).pack(side="left")
        btn_ghost(br, text="", image=icon("rot_r", 20), width=44,
                  command=lambda: self._rotate_selected(270)).pack(side="left",
                                                                   padx=(4, 10))
        btn_ghost(br, text=t("scan_select_all"), width=110,
                  command=self._select_all_pages).pack(side="left", padx=(0, 6))
        btn_ghost(br, text=t("remove_sel"), width=130,
                  command=self._remove_pages).pack(side="left", padx=(0, 6))
        btn_ghost(br, text=t("clear_list"), width=120,
                  command=self._clear_pages).pack(side="left")

        # Страницы — сетка миниатюр: клик = выбор, перетаскивание = порядок
        self.pages_area = ctk.CTkScrollableFrame(right, fg_color=CLR_LIST_BG,
                                                 corner_radius=10)
        self.pages_area.pack(fill="both", expand=True, padx=10, pady=4)
        self._page_sel: set = set()
        self._page_cards: dict = {}
        self._drag = {"path": None, "moved": False, "x": 0, "y": 0}
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
        self.scan_btn = btn_primary(af, text=t("scan_btn"), width=180, height=42,
                                    image=icon_active("scan", 16), compound="left",
                                    font=ctk.CTkFont(size=14, weight="bold"),
                                    command=self._scan)
        self.scan_btn.pack(side="left", padx=(0, 10))
        btn_ghost(af, text=t("save_pdf"), width=150, height=42,
                  command=self._save_one_pdf).pack(side="left", padx=(0, 8))
        btn_ghost(af, text=t("save_jpg"), width=150, height=42,
                  command=self._save_jpg).pack(side="left")

        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(side="bottom", fill="x", padx=12, pady=(2, 10))
        self.progress = ctk.CTkProgressBar(bot, height=7, progress_color=CLR_ACC)
        self.progress.pack(fill="x", pady=(0, 4))
        self.progress.set(0)
        self.log = ctk.CTkTextbox(bot, height=84, font=ctk.CTkFont(size=13))
        self.log.pack(fill="x")
        body.pack(side="top", fill="both", expand=True, padx=12, pady=4)
        self.log.insert("1.0", t("scan_help") + "\n")
        self.log.configure(state="disabled")
        self.count_label = self.pages_count
        self._toggle_comp_ui()
        self._toggle_ui_mode()

    def _toggle_comp_ui(self):
        # блок сжатия — последний в колонке, ему достаточно встать в конец
        if self.compress_var.get():
            self.comp_frame.pack(fill="x", padx=6, pady=2)
        else:
            self.comp_frame.pack_forget()

    def _toggle_ui_mode(self):
        """В окне драйвера пользователь всё выбирает сам — наши поля прячем."""
        if self.ui_var.get():
            self.opts_frame.pack_forget()
            self.ui_hint.pack(anchor="w", padx=10, pady=(0, 4))
        else:
            self.ui_hint.pack_forget()
            self.opts_frame.pack(fill="x", padx=6, pady=0)

    def on_leave(self):
        # Страницы НЕ удаляются: они живут в сеансе до «Очистить список».
        if self._job is not None:
            self._job.cancel()
        self._pages_q.put(None)
        super().on_leave()

    def _browse_folder(self):
        d = filedialog.askdirectory(title=t("folder"))
        if d:
            self.folder_var.set(d)

    def _on_pdf_preset(self, choice: str):
        # те же числа, что у resolve_pdf_params (скан плотнее 160 dpi)
        m = {
            t("smart_pdf"): (130, 68),
            t("aggressive"): (96, 45),
            t("max_compress"): (85, 40),
            t("balanced"): (120, 60),
            t("quality"): (220, 85),
        }
        if choice in m:
            d, q = m[choice]
            self.comp_dpi.set(str(d))
            self.comp_q.set(str(q))

    # ---------------------------------------------------------- devices

    def _refresh_scanners(self):
        if self._listing:
            return
        self._listing = True
        try:
            self.refresh_btn.configure(state="disabled")
            self.dev_note.configure(text=t("scan_searching"))
        except Exception:
            pass

        def work():
            try:
                devices, notes = list_scanners()
            except Exception as e:
                _log_exc("refresh scanners", e)
                devices, notes = [], [{"code": "internal", "detail": str(e)}]
            wia_off = False
            if not any(d.backend == "wia" for d in devices):
                wia_off = wia_service_running() is False
            self.msg_queue.put(("scan_devices", (devices, notes, wia_off)))

        threading.Thread(target=work, daemon=True, name="scan-list").start()

    def _apply_devices(self, devices, notes, wia_off):
        self._listing = False
        self.devices = devices
        try:
            self.refresh_btn.configure(state="normal")
            self.dev_list.delete(0, "end")
            for d in devices:
                self.dev_list.insert("end", d.label)
            want = load_settings().get("scan_device") or ""
            idx = next((i for i, d in enumerate(devices) if d.key == want), 0)
            if devices:
                self.dev_list.selection_set(idx)
                self.dev_list.see(idx)
            codes = {n.get("code") for n in notes}
            if "helpers_missing" in codes:
                note = t("scan_err_helpers_missing")
            elif not devices:
                note = t("scan_no_devices")
                if wia_off:
                    note += "\n\n" + t("scan_err_wia_service")
            else:
                note = t("scan_found").format(n=len(devices))
            self.dev_note.configure(text=note)
            self._log(f"{t('scanner')}: " + (", ".join(d.label for d in devices) or "—"))
            for n in notes:
                log.info("scanner note: %s", n)
        except Exception as e:
            _log_exc("scanners ui", e)

    def _selected_device(self) -> Optional[ScanDevice]:
        sel = self.dev_list.curselection()
        if sel and 0 <= sel[0] < len(self.devices):
            return self.devices[sel[0]]
        return self.devices[0] if len(self.devices) == 1 else None

    def _remember_device(self):
        dev = self._selected_device()
        if dev is not None:
            s = load_settings()
            s["scan_device"] = dev.key
            save_settings(s)

    # ------------------------------------------------------------ scanning

    def _scan_opts(self) -> dict:
        return {
            "ui": bool(self.ui_var.get()),
            "source": self._src_map.get(self.src_var.get(), "flatbed"),
            "color": self._col_map.get(self.col_var.get(), "color"),
            "dpi": self._dpi_map.get(self.dpi_var.get(), 300),
            "paper": self._pap_map.get(self.pap_var.get(), "a4"),
        }

    def _scan(self):
        if self._job is not None:
            self._cancel_scan()
            return
        if self.is_running:
            return
        dev = self._selected_device()
        if dev is None:
            self.show_toast(t("scan_pick_device") if self.devices
                            else t("scan_no_devices"), "warn")
            return
        opts = self._scan_opts()
        out_dir = TEMP_DIR / f"scanjob_{time.time_ns()}"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.show_toast(str(e), "err")
            return
        try:
            parent = ctypes.windll.user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
        except Exception:
            parent = 0
        self._job_info = {"opts": opts, "device": dev, "out": str(out_dir),
                          "added": 0, "blank": 0, "straight": 0,
                          "error": None, "warnings": [], "feeder": None,
                          "dpi": opts["dpi"]}
        job = ScanJob(dev, opts, str(out_dir), parent,
                      lambda ev: self.msg_queue.put(("scan_event", ev)))
        try:
            job.start()
        except Exception as e:
            _log_exc("scan start", e)
            self.show_toast(t("scan_err_helpers_missing"), "err")
            return
        self._job = job
        self.is_running = True
        save_settings(self._collect())
        self._log(f"——— {dev.label} ———")
        self.scan_btn.configure(text=t("scan_cancel"))
        self._set_status(t("scan_st_opening"), CLR_WARN)
        self.msg_queue.put(("busy", True))

    def _cancel_scan(self):
        if self._job is None:
            return
        self._set_status(t("stopping"), CLR_WARN)
        self._log("⏹ " + t("stopping"))
        self._job.cancel()

    def _stop(self):
        """Esc: остановить сканирование."""
        if self._job is not None:
            self._cancel_scan()

    def _set_status(self, text: str, color=None):
        try:
            self.status.configure(text=text, text_color=color or CLR_MUTED)
        except Exception:
            pass

    def _on_msg(self, item):
        kind = item[0]
        if kind == "scan_event":
            self._on_scan_event(item[1])
        elif kind == "scan_devices":
            self._apply_devices(*item[1])
        elif kind == "scan_page_ready":
            self._add_scanned_page(item[1])
        elif kind == "scan_finished":
            self._finish_scan(item[1])
        elif kind == "scan_rotated":
            self._apply_rotation(item[1])

    def _on_scan_event(self, ev: dict):
        info = self._job_info
        kind = ev.get("event")
        if kind == "status":
            code = ev.get("code")
            if code == "opening":
                self._set_status(t("scan_st_opening"), CLR_WARN)
            elif code == "waiting_ui":
                self._set_status(t("scan_st_waiting_ui"), CLR_WARN)
            elif code == "scanning":
                # счётчик честный: со сканера пришло столько-то изображений
                # (пустые обороты тоже), сохранено — видно по карточкам
                n = info.get("received", 0)
                self._set_status(t("scan_st_scanning").format(n=n) if n
                                 else t("scan_st_scanning0"), CLR_WARN)
        elif kind == "settings":
            info["feeder"] = bool(ev.get("feeder"))
            if ev.get("dpi"):
                info["dpi"] = ev.get("dpi")
            color = {"color": t("scan_col_color"), "gray": t("scan_col_gray"),
                     "bw": t("scan_col_bw")}.get(ev.get("color"), ev.get("color"))
            src = (t("scan_src_duplex") if ev.get("duplex") else
                   t("scan_src_feeder") if ev.get("feeder") else
                   t("scan_src_flatbed"))
            self._log(f"{ev.get('dpi')} dpi · {color} · {src}")
        elif kind == "warning":
            code = ev.get("code", "")
            text = t("scan_warn_" + code) if ("scan_warn_" + code) in \
                TRANSLATIONS.get(LANG, {}) else code
            if "{dpi}" in text:
                text = text.format(dpi=ev.get("dpi", ""))
            info["warnings"].append(text)
            self._log("⚠ " + text)
        elif kind == "page":
            info["received"] = info.get("received", 0) + 1
            self._set_status(t("scan_st_scanning").format(n=info["received"]),
                             CLR_WARN)
            # Обработка (поворот, проверка на пустоту, JPEG) — в фоне, по
            # порядку прихода страниц.
            self._pages_q.put(("page", ev, dict(info["opts"]), info.get("feeder"),
                               info.get("dpi"), bool(self.deskew_var.get()),
                               bool(self.blank_var.get())))
        elif kind == "error":
            info["error"] = ev
            self._log("✗ " + self._error_text(ev))
        elif kind == "done":
            # «Готово» — после всех страниц в очереди обработки
            self._pages_q.put(("done", ev))

    def _page_worker(self):
        while True:
            item = self._pages_q.get()
            if item is None:
                return
            if item[0] == "done":
                self.msg_queue.put(("scan_finished", item[1]))
                continue
            _k, ev, opts, feeder, dpi, deskew, skip_blank = item
            # пустые страницы — забота автоподатчика; на стекле пустой
            # лист значит, что сканировали не то, и это надо видеть
            use_feeder = feeder if feeder is not None else opts.get("source") != "flatbed"
            try:
                res = finalize_scan_page(ev["path"], ScanSessionStore.DIR,
                                         opts.get("color", "color"), deskew,
                                         skip_blank and bool(use_feeder),
                                         float(ev.get("dpi") or dpi or 0))
            except Exception as e:
                _log_exc("scan page", e)
                res = {"path": None, "blank": False, "angle": 0.0,
                       "error": str(e)[:120]}
            self.msg_queue.put(("scan_page_ready", res))

    def _add_scanned_page(self, res: dict):
        info = self._job_info
        if res.get("blank"):
            info["blank"] = info.get("blank", 0) + 1
            self._log("◌ " + t("scan_blank_skipped"))
            return
        path = res.get("path")
        if not path:
            if res.get("error"):
                self._log("✗ " + res["error"])
            return
        if abs(res.get("angle") or 0) >= 0.2:
            info["straight"] = info.get("straight", 0) + 1
        info["added"] = info.get("added", 0) + 1
        self.pages.append(path)
        ScanSessionStore.save(self.pages)
        try:
            self._make_page_card(len(self.pages) - 1, path)
            self.pages_count.configure(text=str(len(self.pages)))
            self.pages_hint.place_forget()
            self.after(30, lambda: self.pages_area._parent_canvas.yview_moveto(1.0))
        except Exception as e:
            _log_exc("scan page card", e)
        self._log(f"✓ {t('scan_page_n').format(n=len(self.pages))}")

    def _finish_scan(self, ev: dict):
        info = self._job_info
        self._job = None
        self.is_running = False
        # Неопределённый прогресс гасим здесь же, а не через очередь:
        # сообщение из очереди пришло бы после итога и обнулило полосу.
        if self._busy:
            self._busy = False
            try:
                self.progress.stop()
            except Exception:
                pass
            self.progress.configure(mode="determinate")
        try:
            self.scan_btn.configure(text=t("scan_btn"))
        except Exception:
            pass
        try:
            shutil.rmtree(info.get("out", ""), ignore_errors=True)
        except Exception:
            pass
        added, blank, straight = info.get("added", 0), info.get("blank", 0), \
            info.get("straight", 0)
        parts = [t("scan_done_n").format(n=added)]
        if blank:
            parts.append(t("scan_blank_n").format(n=blank))
        if straight:
            parts.append(t("scan_straight_n").format(n=straight))
        summary = "  ·  ".join(parts)
        err = info.get("error")
        if err:
            text = self._error_text(err)
            if added:
                text += "\n" + t("scan_kept_n").format(n=added)
            self._set_status(t("error"), CLR_ERR)
            self.progress.set(0)
            self.show_toast(text, "err", timeout=10000)
        elif ev.get("cancelled"):
            self._set_status(t("stopped"), CLR_WARN)
            self.progress.set(0)
            self.show_toast(t("scan_cancelled") + (f"  ·  {summary}" if added else ""),
                            "warn")
        else:
            self._set_status(t("ready"), CLR_MUTED)
            self.progress.set(1.0)
            kind = "warn" if (info.get("warnings") or (not added and not blank)) else "ok"
            text = summary
            if info.get("warnings"):
                text += "\n" + info["warnings"][-1]
            if not added and not blank:
                text = t("scan_no_pages_came")
            self.show_toast(text, kind)
            if added and load_settings().get("sound", True):
                play_sound()
        self._log(summary)

    def _error_text(self, ev: dict) -> str:
        code = ev.get("code") or "driver_error"
        key = "scan_err_" + code
        table = TRANSLATIONS.get(LANG, TRANSLATIONS["ru"])
        text = table.get(key) or table.get("scan_err_driver_error", code)
        detail = (ev.get("detail") or "").strip()
        if code in ("driver_error", "internal", "device_open_failed") and detail:
            text += f" ({detail[:90]})"
        return text

    # --------------------------------------------------------------- pages

    def _ingest_file(self, path: str) -> list:
        """Добавить картинку; многостраничный TIFF разбить на страницы."""
        added = []
        path = str(path)
        if not os.path.isfile(path):
            return added
        if Path(path).suffix.lower() in (".tif", ".tiff"):
            try:
                with Image.open(path) as im:
                    n = getattr(im, "n_frames", 1)
                    if n > 1:
                        for i in range(n):
                            try:
                                im.seek(i)
                                dpi = (im.info.get("dpi") or (0, 0))[0]
                                frame = im.convert("1") if im.mode == "1" else \
                                    im.convert("L" if im.mode in ("L", "I;16") else "RGB")
                                out = save_page_image(
                                    frame, str(ScanSessionStore.DIR /
                                               f"page_{time.time_ns()}"), dpi)
                                self.pages.append(out)
                                added.append(out)
                            except Exception as e:
                                _log_exc("tiff frame", e)
                        return added
            except Exception as e:
                _log_exc("tiff ingest", e)
        self.pages.append(path)
        added.append(path)
        return added

    def add_paths(self, paths: List[str]):
        """DnD: перетащенные картинки становятся страницами."""
        added = 0
        ScanSessionStore.DIR.mkdir(parents=True, exist_ok=True)
        for p in paths:
            for f in collect_files(p, IMAGE_EXTS, False):
                added += len(self._ingest_file(f))
        if added:
            ScanSessionStore.save(self.pages)
            self._refresh_pages_ui()
            self._log(f"+ {added}")

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
            if path in self._page_sel:
                self._page_sel.discard(path)
            else:
                self._page_sel.add(path)
            self._paint_selection(path)
            return
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

    def _paint_selection(self, path):
        cd = self._page_cards.get(path)
        if cd:
            cd["card"].configure(border_color=(CLR_ACC if path in self._page_sel
                                               else CLR_ROW))

    def _select_all_pages(self):
        if self.pages and len(self._page_sel) == len(self.pages):
            self._page_sel.clear()
        else:
            self._page_sel = set(self.pages)
        for p in self.pages:
            self._paint_selection(p)

    def _reorder_pages(self, src: str, target: str):
        try:
            self.pages.remove(src)
            self.pages.insert(self.pages.index(target), src)
        except ValueError:
            return
        ScanSessionStore.save(self.pages)
        self._relayout_pages()  # без пересоздания — мгновенно

    def _rotate_selected(self, degrees: int):
        targets = [p for p in self.pages if p in self._page_sel]
        if not targets:
            self.show_toast(t("select_pages_first"), "warn")
            return
        if self.is_running:
            return

        def work():
            mapping = {}
            for p in targets:
                try:
                    mapping[p] = rotate_page_file(p, degrees, ScanSessionStore.DIR)
                except Exception as e:
                    _log_exc("rotate page", e)
            self.msg_queue.put(("scan_rotated", mapping))

        threading.Thread(target=work, daemon=True).start()

    def _apply_rotation(self, mapping: dict):
        if not mapping:
            return
        self.pages = [mapping.get(p, p) for p in self.pages]
        self._page_sel = {mapping.get(p, p) for p in self._page_sel}
        for old in mapping:
            self._thumb_cache.pop(old, None)
        ScanSessionStore.save(self.pages)
        self._refresh_pages_ui()

    def _remove(self):
        # Del в сканере работает со страницами, а не с self.files
        self._remove_pages()

    def _clear(self):
        self._clear_pages()

    def _remove_pages(self):
        if not self._page_sel:
            return
        for p in list(self._page_sel):
            if ScanSessionStore.owns(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            if p in self.pages:
                self.pages.remove(p)
            self._thumb_cache.pop(p, None)
        self._page_sel.clear()
        ScanSessionStore.save(self.pages)
        self._refresh_pages_ui()

    def _clear_pages(self):
        if self.pages and not messagebox.askyesno(APP_NAME, t("clear_pages_q")):
            return
        for p in self.pages:
            if ScanSessionStore.owns(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        self.pages.clear()
        self._page_sel.clear()
        self._thumb_cache.clear()
        ScanSessionStore.save(self.pages)
        self._refresh_pages_ui()

    # ---------------------------------------------------------------- save

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

    @staticmethod
    def _maybe_open(folder: str, wanted: bool):
        """Вызывается из фонового потока: значение галочки читается заранее,
        в потоке интерфейса — Tk из чужого потока трогать нельзя."""
        if wanted:
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
        out_pdf = unique_out_path(str(Path(folder) / f"document_{now}.pdf"))
        raw_pdf = str(TEMP_DIR / f"doc_raw_{now}.pdf")
        compress = self.compress_var.get()
        preset = self._preset_key()
        dpi = safe_int(self.comp_dpi.get(), 130, 50, 300)
        quality = safe_int(self.comp_q.get(), 68, 20, 95)
        gray = self.gray_var.get()
        pages = list(self.pages)
        open_after = bool(self.open_after_var.get())

        def work():
            try:
                TEMP_DIR.mkdir(parents=True, exist_ok=True)
                self.msg_queue.put(("log", f"→ PDF ({len(pages)})…"))
                span = 0.5 if compress else 1.0
                ok, msg, _added, skipped = images_to_pdf(
                    pages, raw_pdf if compress else out_pdf,
                    progress_cb=lambda i, n: self.msg_queue.put(
                        ("progress", span * i / max(1, n), f"{i}/{n}")))
                if not ok:
                    self.msg_queue.put(("log", f"✗ {tr_msg(msg)}"))
                    self.msg_queue.put(("toast", tr_msg(msg), "err", None))
                    return
                if compress:
                    _i, ok2, msg2, orig, new = process_pdf(
                        raw_pdf, out_pdf, preset, dpi, quality, gray,
                        progress_cb=lambda f: self.msg_queue.put(
                            ("progress", 0.5 + 0.5 * f, f"{int(f * 100)}%")),
                        threads=min(4, MAX_WORKERS))
                    if not ok2:
                        # сжатие без выигрыша → исходник ещё на месте
                        shutil.copy2(raw_pdf, out_pdf)
                size = os.path.getsize(out_pdf)
                self.msg_queue.put(("log", f"✓ PDF: {out_pdf} ({size / 1024:.0f} KB)"))
                text = f"{t('pdf_created')} {os.path.basename(out_pdf)}  ·  " \
                       f"{human_size(size)}"
                if skipped:
                    text += f"  ·  ✗ {t('skipped_n').format(n=skipped)}"
                self.msg_queue.put(("toast", text, "warn" if skipped else "ok", folder))
                add_history({
                    "time": datetime.now().strftime("%d.%m %H:%M"),
                    "mode": "Scan→PDF",
                    "count": len(pages),
                    "saved_mb": 0,
                    "folder": folder,
                    "names": [os.path.basename(out_pdf)],
                })
                self._maybe_open(folder, open_after)
                self.msg_queue.put(("progress", 1, t("done")))
            except Exception as e:
                _log_exc("scan save pdf", e)
                self.msg_queue.put(("toast", str(e)[:160], "err", None))
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
        base_q = safe_int(self.comp_q.get(), 68, 20, 95)
        q = max(40, min(92, base_q + (0 if compress else 25)))
        open_after = bool(self.open_after_var.get())

        def work():
            try:
                ok_n = 0
                stamp = datetime.now().strftime("%H%M%S")
                for i, p in enumerate(pages, 1):
                    out = unique_out_path(str(Path(folder) / f"scan_{i:02d}_{stamp}.jpg"))
                    ok = False
                    msg = ""
                    if compress:
                        _a, ok, msg, _o, _n = process_image(p, out, q, 0, "JPEG", True)
                        if not ok and msg == "err_no_gain":
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
                self.msg_queue.put(("toast", f"JPG: {ok_n}/{len(pages)}",
                                    "ok" if ok_n == len(pages) else "warn", folder))
                add_history({
                    "time": datetime.now().strftime("%d.%m %H:%M"),
                    "mode": "Scan→files",
                    "count": ok_n,
                    "saved_mb": 0,
                    "folder": folder,
                })
                self._maybe_open(folder, open_after)
            finally:
                self.is_running = False

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _plain_jpg(src: str, out: str) -> bool:
        try:
            with Image.open(src) as im0:
                im = ImageOps.exif_transpose(im0)
                if im.mode not in ("L", "RGB"):
                    im = im.convert("RGB")
                im.save(out, "JPEG", quality=92, optimize=True)
            return True
        except Exception as e:
            _log_exc("plain jpg", e)
            return False

    # ------------------------------------------------------------ settings

    @staticmethod
    def _label_for(mapping: dict, value, default_label: str) -> str:
        return next((lb for lb, v in mapping.items() if v == value), default_label)

    def _apply(self):
        s = self.settings
        self.compress_var.set(bool(s.get("scan_compress", True)))
        self.pdf_preset_var.set(s.get("scan_pdf_preset") or t("smart_pdf"))
        self.comp_dpi.set(str(safe_int(s.get("scan_comp_dpi"), 130, 50, 300)))
        self.comp_q.set(str(safe_int(s.get("scan_comp_q"), 68, 20, 95)))
        self.gray_var.set(bool(s.get("scan_gray", False)))
        self.open_after_var.set(bool(s.get("scan_open_folder", True)))
        if s.get("scan_folder"):
            self.folder_var.set(s["scan_folder"])
        self.ui_var.set(bool(s.get("scan_use_ui", False)))
        self.src_var.set(self._label_for(self._src_map, s.get("scan_source"),
                                         t("scan_src_flatbed")))
        self.col_var.set(self._label_for(self._col_map, s.get("scan_color"),
                                         t("scan_col_color")))
        self.dpi_var.set(self._label_for(self._dpi_map, safe_int(s.get("scan_dpi"), 300),
                                         "300 dpi"))
        self.pap_var.set(self._label_for(self._pap_map, s.get("scan_paper"), "A4"))
        self.deskew_var.set(bool(s.get("scan_deskew", True)))
        self.blank_var.set(bool(s.get("scan_skip_blank", True)))
        self._toggle_comp_ui()
        self._toggle_ui_mode()

    def _collect(self) -> dict:
        s = load_settings()
        opts = self._scan_opts()
        s.update({
            "scan_compress": self.compress_var.get(),
            "scan_pdf_preset": self.pdf_preset_var.get(),
            "scan_comp_dpi": safe_int(self.comp_dpi.get(), 130, 50, 300),
            "scan_comp_q": safe_int(self.comp_q.get(), 68, 20, 95),
            "scan_gray": self.gray_var.get(),
            "scan_folder": self.folder_var.get(),
            "scan_open_folder": self.open_after_var.get(),
            "scan_use_ui": opts["ui"],
            "scan_source": opts["source"],
            "scan_color": opts["color"],
            "scan_dpi": opts["dpi"],
            "scan_paper": opts["paper"],
            "scan_deskew": self.deskew_var.get(),
            "scan_skip_blank": self.blank_var.get(),
            "theme": ctk.get_appearance_mode(),
        })
        dev = self._selected_device() if hasattr(self, "dev_list") else None
        if dev is not None:
            s["scan_device"] = dev.key
        return s

    def _start(self):
        self._scan()


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





