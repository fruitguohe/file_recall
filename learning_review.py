#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from pathlib import Path
from datetime import datetime, timedelta
import subprocess
import platform
import sys
import plistlib
import traceback
import threading
import json
import os
import shlex

# ---------- Shared window refs (GUI) ----------
# 供 py2app 收集 PyObjC 依赖（仅 macOS 用）
if platform.system() == "Darwin":
    try:
        import objc  # noqa: F401
        from Foundation import NSObject  # noqa: F401
        from PyObjCTools import AppHelper  # noqa: F401
        from AppKit import NSApp, NSAlert  # noqa: F401  # GUI 与错误弹窗
    except ImportError:
        pass

# ---------- V1: Storage ----------
LIBRARY_DIR = Path.home() / ".learning-review"
LIBRARY_PATH = LIBRARY_DIR / "library.json"
FLAG_PATH = LIBRARY_DIR / "show_today.flag"

LIBRARY_DID_CHANGE_NOTIFICATION = "LearningReviewLibraryDidChange"

NOTIFY_TITLE = "今日复习提醒"
INTERVALS = [1, 2, 4, 7, 15, 30]

# 手动语言偏好：UserDefaults 键；值为 ""（跟随系统）、"zh-Hans"、"en"
LANG_PREFERENCE_KEY = "LearningReviewLanguage"
LANGUAGE_DID_CHANGE_NOTIFICATION = "LearningReviewLanguageDidChange"
_strings_cache = {}  # lang -> {key: value}，手动切换时使用


def _get_lang_preference() -> str:
    """返回用户手动选择的语言："" 跟随系统，"zh-Hans" 或 "en"。"""
    if platform.system() != "Darwin":
        return ""
    try:
        from Foundation import NSUserDefaults
        ud = NSUserDefaults.standardUserDefaults()
        if ud is None:
            return ""
        o = ud.stringForKey_(LANG_PREFERENCE_KEY)
        return (o or "") if o else ""
    except Exception:
        return ""


def _set_lang_preference(lang: str) -> None:
    """设置手动语言：""、"zh-Hans" 或 "en"。"""
    if platform.system() != "Darwin":
        return
    try:
        from Foundation import NSUserDefaults
        ud = NSUserDefaults.standardUserDefaults()
        if ud:
            ud.setObject_forKey_(lang, LANG_PREFERENCE_KEY)
            ud.synchronize()
        global _strings_cache
        _strings_cache = {}
    except Exception:
        pass


def _load_strings_for_language(lang: str) -> dict:
    """从 .app 或脚本目录加载指定语言的 Localizable.strings，带缓存。"""
    if lang in _strings_cache:
        return _strings_cache[lang]
    out = {}
    try:
        from Foundation import NSBundle
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = None
        bundle = NSBundle.mainBundle()
        if bundle and bundle.bundlePath().endswith(".app"):
            res_path = bundle.resourcePath()
            if res_path:
                path = os.path.join(str(res_path), lang + ".lproj", "Localizable.strings")
        if not path or not os.path.isfile(path):
            path = os.path.join(script_dir, lang + ".lproj", "Localizable.strings")
        if path and os.path.isfile(path):
            out = _parse_strings_file(path)
        _strings_cache[lang] = out
    except Exception:
        pass
    return out


def _localized_string(key: str, default: str) -> str:
    """使用 macOS 本地化；支持手动语言偏好（UserDefaults）。"""
    if platform.system() != "Darwin":
        return default
    try:
        manual = _get_lang_preference()
        if manual in ("zh-Hans", "en"):
            d = _load_strings_for_language(manual)
            return d.get(key, default)

        from Foundation import NSBundle, NSLocale
        bundle = NSBundle.mainBundle()
        if bundle and bundle.bundlePath().endswith(".app"):
            s = bundle.localizedStringForKey_value_table_(key, default, None)
            return s if s else default
        # 脚本运行：从脚本所在目录的 .lproj 加载
        script_dir = os.path.dirname(os.path.abspath(__file__))
        preferred = None
        try:
            loc = NSLocale.currentLocale()
            if loc:
                langs = NSLocale.preferredLanguages()
                if langs and langs.count() > 0:
                    preferred = str(langs.objectAtIndex_(0))
        except Exception:
            pass
        if not preferred:
            preferred = "en"
        for lang in (preferred, "en", "zh-Hans", "Base"):
            if lang and lang.startswith("zh"):
                lang = "zh-Hans"
            if not lang:
                continue
            path = os.path.join(script_dir, lang + ".lproj", "Localizable.strings")
            if not os.path.isfile(path):
                continue
            parsed = _parse_strings_file(path)
            if key in parsed:
                return parsed[key]
        return default
    except Exception:
        return default


def _parse_strings_file(path: str) -> dict:
    """解析 .strings 文件，返回 key -> value。"""
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return out
    import re
    # "key" = "value"; 支持 \n \" \\ 等转义
    pattern = re.compile(r'"([^"\\]*(?:\\.[^"\\]*)*)"\s*=\s*"([^"\\]*(?:\\.[^"\\]*)*)"\s*;', re.MULTILINE | re.DOTALL)
    for m in pattern.finditer(content):
        k = m.group(1).encode().decode("unicode_escape") if ("\\" in m.group(1)) else m.group(1).replace("\\n", "\n").replace("\\\"", '"').replace("\\\\", "\\")
        v = m.group(2).replace("\\n", "\n").replace("\\\"", '"').replace("\\\\", "\\")
        out[k] = v
    return out


def L(key: str, default: str) -> str:
    """本地化：L("key", "默认中文")"""
    return _localized_string(key, default)

# LaunchAgent (reuse)
LAUNCH_AGENT_LABEL = "com.learning.review"
LAUNCH_AGENT_PLIST = Path.home() / "Library/LaunchAgents/com.learning.review.plist"
DEFAULT_REVIEW_HOUR = 9
DEFAULT_REVIEW_MINUTE = 0
NOTIFICATION_CLICK_TIMEOUT = 120
LAUNCH_AGENT_STDOUT = "/tmp/learning_review.log"
LAUNCH_AGENT_STDERR = "/tmp/learning_review_error.log"

# 定时任务发通知后，用户点击通知时置 True，__main__ 中据此在当前进程直接拉起 GUI
_notification_clicked_show_today = [False]

_library_cache = None


def _ensure_library_dir():
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)


def load_library():
    """Load library from disk; return {"files": [], "folders": []}. Dedupe by path."""
    global _library_cache
    _ensure_library_dir()
    if not LIBRARY_PATH.exists():
        _library_cache = {"files": [], "folders": []}
        return _library_cache
    try:
        with open(LIBRARY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"files": [], "folders": []}
    files = data.get("files") or []
    seen = set()
    deduped = []
    for rec in files:
        p = rec.get("path") or ""
        if p and p not in seen:
            seen.add(p)
            deduped.append(rec)
    data["files"] = deduped
    data["folders"] = list(data.get("folders") or [])
    _library_cache = data
    return _library_cache


def get_library():
    global _library_cache
    if _library_cache is None:
        load_library()
    return _library_cache


def save_library(data=None):
    if data is None:
        data = get_library()
    _ensure_library_dir()
    with open(LIBRARY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    global _library_cache
    _library_cache = data
    # GUI 场景下自动通知刷新；非 macOS/无 PyObjC 时会被安全忽略
    _post_library_did_change()


def _post_library_did_change():
    """Notify in-app observers that library.json changed (GUI refresh)."""
    if platform.system() != "Darwin":
        return
    try:
        import objc
        NSNotificationCenter = objc.lookUpClass("NSNotificationCenter")
        NSNotificationCenter.defaultCenter().postNotificationName_object_(LIBRARY_DID_CHANGE_NOTIFICATION, None)
    except Exception:
        pass


def reset_all_data():
    """一键清空：清空复习库（关联文件夹与文件记录），并清除 show_today 标记。"""
    empty = {"files": [], "folders": []}
    save_library(empty)
    try:
        if FLAG_PATH.exists():
            FLAG_PATH.unlink()
    except Exception:
        pass


# ---------- V1: File filtering ----------
IGNORED_FILENAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORED_EXTENSIONS = {".tmp", ".temp", ".part", ".crdownload"}
IGNORED_FOLDER_NAMES = {".git", "node_modules", "venv", "__pycache__", ".cache"}


def is_ignored_file(path: Path) -> bool:
    if path.name in IGNORED_FILENAMES:
        return True
    if path.suffix.lower() in IGNORED_EXTENSIONS:
        return True
    return False


def is_ignored_folder(name: str) -> bool:
    return name in IGNORED_FOLDER_NAMES


# ---------- V1: Library API ----------

def _today_str():
    return datetime.today().date().isoformat()


def add_file(path) -> str:
    path = Path(path).resolve()
    if not path.is_file():
        return "skipped"
    path_str = str(path)
    lib = get_library()
    for rec in lib["files"]:
        if (rec.get("path") or "") == path_str:
            return "skipped"
    source_folder = str(path.parent)
    rec = {
        "path": path_str,
        "added_at": _today_str(),
        "last_review": None,
        "interval": 1,
        "next_review": _today_str(),
        "source_folder": source_folder,
    }
    lib["files"].append(rec)
    save_library()
    return "added"


def remove_file(path: str):
    lib = get_library()
    lib["files"] = [r for r in lib["files"] if (r.get("path") or "") != path]
    save_library()


def _next_interval(current: int) -> int:
    try:
        i = INTERVALS.index(current)
        if i + 1 < len(INTERVALS):
            return INTERVALS[i + 1]
    except ValueError:
        pass
    return 30


def update_file_after_review(path: str):
    lib = get_library()
    today = _today_str()
    for rec in lib["files"]:
        if (rec.get("path") or "") == path:
            rec["last_review"] = today
            rec["interval"] = _next_interval(rec.get("interval") or 1)
            delta = timedelta(days=rec["interval"])
            next_date = datetime.today().date() + delta
            rec["next_review"] = next_date.isoformat()
            save_library()
            return
    save_library()


def add_folder(folder_path):
    folder_path = Path(folder_path).resolve()
    if not folder_path.is_dir():
        return {"added": 0, "skipped": 0, "ignored": 0}
    lib = get_library()
    folder_str = str(folder_path)
    if folder_str not in lib["folders"]:
        lib["folders"].append(folder_str)
    existing_paths = {r.get("path") for r in lib["files"] if r.get("path")}
    added, skipped, ignored = 0, 0, 0
    for root, dirs, files in os.walk(folder_path):
        root_p = Path(root)
        dirs[:] = [d for d in dirs if not is_ignored_folder(d)]
        for name in files:
            fpath = root_p / name
            if is_ignored_file(fpath):
                ignored += 1
                continue
            path_str = str(fpath.resolve())
            if path_str in existing_paths:
                skipped += 1
                continue
            rec = {
                "path": path_str,
                "added_at": _today_str(),
                "last_review": None,
                "interval": 1,
                "next_review": _today_str(),
                "source_folder": str(root_p.resolve()),
            }
            lib["files"].append(rec)
            existing_paths.add(path_str)
            added += 1
    save_library()
    return {"added": added, "skipped": skipped, "ignored": ignored}


def get_today_files():
    today = _today_str()
    lib = get_library()
    out = [r for r in lib["files"] if (r.get("next_review") or "") <= today]

    def key(r):
        return (
            r.get("next_review") or "",
            r.get("added_at") or "",
            (Path(r.get("path") or "").name or ""),
        )

    out.sort(key=key)
    return out


def scan_folders_and_add_new():
    lib = get_library()
    for folder_str in lib.get("folders") or []:
        add_folder(folder_str)


def write_show_today_flag():
    _ensure_library_dir()
    FLAG_PATH.touch()


def consume_show_today_flag() -> bool:
    if FLAG_PATH.exists():
        try:
            FLAG_PATH.unlink()
            return True
        except Exception:
            pass
    return False


# ---------- LaunchAgent 相关 ----------

def _get_app_bundle_path():
    """打包后返回 .app 的绝对路径；开发时返回 None（用脚本方式）。"""
    if getattr(sys, "frozen", False) != "macosx_app":
        return None
    # sys.executable = .../FileRecall.app/Contents/MacOS/FileRecall
    return str(Path(sys.executable).resolve().parent.parent.parent)


def _get_app_program_arguments():
    """返回 LaunchAgent ProgramArguments：与手写 plist 一致，用 open -a <app> --args --scheduled。"""
    app_path = _get_app_bundle_path()
    if app_path:
        return ["/usr/bin/open", "-a", app_path, "--args", "--scheduled"]
    # 开发时：直接用 Python 执行本脚本
    return [sys.executable, str(Path(__file__).resolve()), "--scheduled"]


def _read_launch_agent_plist():
    """读取当前 plist，不存在或解析失败返回 None。"""
    if not LAUNCH_AGENT_PLIST.exists():
        return None
    try:
        with open(LAUNCH_AGENT_PLIST, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return None


# 定时任务日志（与手写 plist 一致）


def _write_launch_agent_plist(hour: int, minute: int, run_at_load=None):
    """写入 plist。run_at_load=None 时沿用已有 plist 的 RunAtLoad，否则用默认 False。"""
    LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_launch_agent_plist()
    if run_at_load is None and existing:
        run_at_load = existing.get("RunAtLoad", False)
    elif run_at_load is None:
        run_at_load = False
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": _get_app_program_arguments(),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": run_at_load,
        "StandardOutPath": LAUNCH_AGENT_STDOUT,
        "StandardErrorPath": LAUNCH_AGENT_STDERR,
    }
    with open(LAUNCH_AGENT_PLIST, "wb") as f:
        plistlib.dump(plist, f)


def _launchctl_load():
    """加载当前用户的 LaunchAgent。"""
    subprocess.run(
        ["launchctl", "load", str(LAUNCH_AGENT_PLIST)],
        capture_output=True,
        check=False,
    )


def _launchctl_unload():
    """卸载 LaunchAgent。"""
    subprocess.run(
        ["launchctl", "unload", str(LAUNCH_AGENT_PLIST)],
        capture_output=True,
        check=False,
    )


def _is_launch_agent_loaded():
    """检查是否已加载（通过 launchctl list 输出判断）。"""
    r = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    return LAUNCH_AGENT_LABEL in (r.stdout or "")


def ensure_launch_agent_on_first_run():
    """首次运行：若 plist 不存在则创建默认定时并加载。
    注意：开发环境（非 .app）下不自动安装，避免 launchd 后台运行时触发 macOS 隐私权限问题。
    """
    if platform.system() != "Darwin":
        return
    # 仅在打包成 .app 后自动安装（sys.frozen == macosx_app）
    if _get_app_bundle_path() is None:
        return
    if LAUNCH_AGENT_PLIST.exists():
        return
    _write_launch_agent_plist(DEFAULT_REVIEW_HOUR, DEFAULT_REVIEW_MINUTE)
    _launchctl_load()


def get_schedule_from_plist():
    """从 plist 读取当前设定的 时、分，若没有则返回默认。"""
    data = _read_launch_agent_plist()
    if not data:
        return DEFAULT_REVIEW_HOUR, DEFAULT_REVIEW_MINUTE
    interval = data.get("StartCalendarInterval") or {}
    return (
        interval.get("Hour", DEFAULT_REVIEW_HOUR),
        interval.get("Minute", DEFAULT_REVIEW_MINUTE),
    )


# ---------- 通知（V1: 点击打开 app --show-today） ----------

def _send_notification_native_with_click(message: str, title: str = None):
    """NSUserNotification；点击时在当前进程拉起 GUI（--show-today），不依赖 open -a。"""
    import objc
    from Foundation import NSObject
    from PyObjCTools import AppHelper

    NSApplication = objc.lookUpClass("NSApplication")
    NSUserNotification = objc.lookUpClass("NSUserNotification")
    NSUserNotificationCenter = objc.lookUpClass("NSUserNotificationCenter")
    NSTimer = objc.lookUpClass("NSTimer")
    NSRunLoop = objc.lookUpClass("NSRunLoop")

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)

    app_bundle_path = _get_app_bundle_path()
    script_path = str(Path(__file__).resolve())
    python_exec = sys.executable

    class NotificationDelegate(NSObject):
        def userNotificationCenter_didActivateNotification_(self, center, notification):
            global _notification_clicked_show_today
            _notification_clicked_show_today[0] = True
            # 若已有其它实例在前台，用 open -a 激活并传 --show-today；否则本进程后续会直接 run_app_gui
            if app_bundle_path:
                subprocess.run(
                    ["/usr/bin/open", "-a", app_bundle_path, "--args", "--show-today"],
                    check=False,
                )
            else:
                cmd = f"{shlex.quote(python_exec)} {shlex.quote(script_path)} --show-today"
                osa = (
                    'tell application "Terminal"\n'
                    '  activate\n'
                    f'  do script "{cmd}"\n'
                    "end tell\n"
                )
                subprocess.run(
                    ["/usr/bin/osascript", "-e", osa],
                    check=False,
                )
            AppHelper.stopEventLoop()

        def onTimeout_(self, timer):
            AppHelper.stopEventLoop()

    delegate = NotificationDelegate.alloc().init()
    center = NSUserNotificationCenter.defaultUserNotificationCenter()
    center.setDelegate_(delegate)

    notif = NSUserNotification.alloc().init()
    notif.setTitle_(title if title else NOTIFY_TITLE)
    notif.setInformativeText_(message)
    notif.setUserInfo_({})
    center.deliverNotification_(notif)

    sel = objc.selector(delegate.onTimeout_, signature=b"v@:@")
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        NOTIFICATION_CLICK_TIMEOUT, delegate, sel, None, False
    )
    NSRunLoop.currentRunLoop().addTimer_forMode_(timer, "kCFRunLoopDefaultMode")

    AppHelper.runConsoleEventLoop()


def _send_notification_fallback(message: str, title: str = None):
    t = title if title else NOTIFY_TITLE
    script = f'display notification "{message}" with title "{t}"'
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False)


def _deliver_notification_native_no_wait(message: str, title: str = None):
    """仅用 NSUserNotification 发一条通知（带 app 图标），不等待点击、不跑 event loop。
    须在主线程调用；点击由 run_app_gui 里设置的 delegate 处理（窗口前置）。
    """
    if platform.system() != "Darwin":
        return
    try:
        import objc
        NSUserNotification = objc.lookUpClass("NSUserNotification")
        NSUserNotificationCenter = objc.lookUpClass("NSUserNotificationCenter")
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        notif = NSUserNotification.alloc().init()
        notif.setTitle_(title if title else NOTIFY_TITLE)
        notif.setInformativeText_(message)
        notif.setUserInfo_({})
        center.deliverNotification_(notif)
    except Exception:
        traceback.print_exc()
        _send_notification_fallback(message, title=title)


def send_macos_notification(message: str, title: str = None, wait_for_click: bool = True):
    if platform.system() != "Darwin":
        return
    if wait_for_click:
        try:
            _send_notification_native_with_click(message, title=title)
            return
        except Exception:
            traceback.print_exc()
    _send_notification_fallback(message, title=title)


# ---------- V1: Scheduled flow & notification body ----------

def _format_review_notification(today_files):
    """返回 (标题, 正文)：沿用之前的样式，📅 今日复习 + ⭐ 文件名。"""
    title = L("review_time_title", "👋 复习时间到！")
    if not today_files:
        return L("notify_title", NOTIFY_TITLE), L("today_empty", "今天没有需要复习的文件。")
    names = [Path(r.get("path") or "").name or "?" for r in today_files[:3]]
    body = L("today_review_label", "📅 今日复习") + "\n⭐ " + (", ".join(names) if names else L("none_yet", "（暂无）"))
    if len(today_files) > 3:
        body += "\n" + (L("more_count", "还有 %d 个") % (len(today_files) - 3))
    return title, body


def run_scheduled_flow(from_gui: bool = False):
    """Scheduled entry: load library, scan folders, get today, notify, write flag.
    from_gui=True 时返回 (title, body) 供主线程发通知+弹窗，不在此处发通知（避免子线程 callAfter 不可靠）。
    """
    load_library()
    lib = get_library()
    if not lib.get("files") and not lib.get("folders"):
        if from_gui:
            return None, None
        return
    scan_folders_and_add_new()
    today_files = get_today_files()
    if today_files:
        title, body = _format_review_notification(today_files)
        write_show_today_flag()
        if from_gui:
            return title, body
        send_macos_notification(body, title=title, wait_for_click=True)
    else:
        if from_gui:
            return L("notify_title", NOTIFY_TITLE), L("today_empty", "今天没有需要复习的文件。")
        send_macos_notification(L("today_empty", "今天没有需要复习的文件。"), title=L("notify_title", NOTIFY_TITLE), wait_for_click=False)


# ---------- GUI ----------

def _run_scheduled_in_thread(callback=None):
    """Run scheduled flow in background thread; callback(notif_title, notif_body) 在主线程执行（发通知+弹窗）。"""
    def _run():
        notif_title, notif_body = None, None
        try:
            result = run_scheduled_flow(from_gui=True)
            if isinstance(result, (list, tuple)) and len(result) >= 2:
                notif_title, notif_body = result[0], result[1]
            elif result is not None:
                notif_title, notif_body = NOTIFY_TITLE, str(result)
        except Exception:
            traceback.print_exc()
        if callback:
            try:
                callback(notif_title, notif_body)
            except Exception:
                pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ---------- Today UI ----------

def _short_path(path_str: str) -> str:
    """Short path for display (e.g. relative to home)."""
    try:
        p = Path(path_str)
        home = Path.home()
        if p.is_relative_to(home):
            return "~/" + str(p.relative_to(home))
    except Exception:
        pass
    return path_str


def run_today_ui(settings_window=None, for_embed=False):
    """Today window: list files due today; double-click opens file and marks reviewed.
    If for_embed=True, only build and return the window (don't run app).
    """
    if platform.system() != "Darwin":
        return None
    try:
        import objc
        from AppKit import (
            NSWindow, NSTextField, NSButton, NSAlert, NSScrollView, NSTableView,
            NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
            NSBackingStoreBuffered, NSAlertStyleInformational, NSMakeRect,
            NSTableColumn, NSTableColumnAutoresizingMask, NSColor,
            NSViewMinXMargin, NSViewWidthSizable, NSViewMinYMargin, NSViewHeightSizable,
            NSView, NSImage, NSTableCellView, NSCursor, NSPanel,
            NSWindowStyleMaskBorderless, NSFont, NSEvent, NSTrackingArea,
            NSTrackingMouseEnteredAndExited, NSTrackingActiveAlways, NSTrackingInVisibleRect,
            NSTrackingCursorUpdate,
            NSBezierPath,
            NSFontAttributeName,
            NSTextFieldCell,
        )
        from Foundation import NSObject, NSMakeRange, NSMakeSize, NSString
        from PyObjCTools import AppHelper
    except ImportError as e:
        raise RuntimeError("今日复习窗口依赖 PyObjC/AppKit 导入失败，请检查 pyobjc 是否已安装。") from e

    class VerticallyCenteredTextFieldCell(NSTextFieldCell):
        def titleRectForBounds_(self, rect):
            titleRect = NSTextFieldCell.titleRectForBounds_(self, rect)
            try:
                titleSize = self.attributedStringValue().size()
                dy = (rect.size.height - titleSize.height) / 2.0
                if dy > 0:
                    titleRect.origin.y = rect.origin.y + dy
                    titleRect.size.height = titleSize.height
            except Exception:
                pass
            return titleRect

        def drawInteriorWithFrame_inView_(self, frame, view):
            NSTextFieldCell.drawInteriorWithFrame_inView_(self, self.titleRectForBounds_(frame), view)

    # 首次点击即可选中行（不浪费一次点击在“激活窗口”上）
    class TodayTableView(NSTableView):
        def acceptsFirstMouse_(self, event):
            return True

        def setFrameSize_(self, size):
            # 让表格宽度与 scroll 的 content 一致，已读行的 rowView 背景才能铺满整行
            sup = self.superview()
            if sup is not None:
                cw = sup.bounds().size.width
                if cw > 0:
                    try:
                        h = size.height if hasattr(size, "height") else size[1]
                        size = NSMakeSize(cw, h)
                    except Exception:
                        pass
            NSTableView.setFrameSize_(self, size)

        def viewDidMoveToSuperview_(self, superview):
            NSTableView.viewDidMoveToSuperview_(self, superview)
            # 刚加入 scroll 时 clip view 可能尚未布局，稍延后撑满宽度
            if self.superview() is not None:
                self.performSelector_withObject_afterDelay_("_ensureFullWidth:", None, 0.05)

        def viewDidMoveToWindow(self):
            NSTableView.viewDidMoveToWindow(self)
            # 嵌入主窗口后 window 变化，此时再撑满一次（clip view 已按 container 布局）
            if self.window() is not None and self.superview() is not None:
                self.performSelector_withObject_afterDelay_("_ensureFullWidth:", None, 0.0)

        def _ensureFullWidth_(self, _):
            sup = self.superview()
            if sup is not None:
                cw = sup.bounds().size.width
                if cw > 0:
                    try:
                        f = self.frame()
                        fh = f.size.height if hasattr(f.size, "height") else f[1]
                        objc.super(TodayTableView, self).setFrameSize_(NSMakeSize(cw, fh))
                        # 「位置」列填满剩余宽度
                        cols = self.tableColumns()
                        if cols is not None and cols.count() >= 3:
                            col0 = cols.objectAtIndex_(0)
                            w0 = col0.width() if hasattr(col0, "width") else 250
                            col2 = cols.objectAtIndex_(2)
                            w2 = col2.width() if hasattr(col2, "width") else 56
                            col1 = cols.objectAtIndex_(1)
                            col1.setWidth_(max(100, cw - w0 - w2))
                    except Exception:
                        pass

        def layout(self):
            NSTableView.layout(self)
            sup = self.superview()
            if sup is not None:
                cw = sup.bounds().size.width
                if cw > 0:
                    try:
                        f = self.frame()
                        fw = f.size.width if hasattr(f.size, "width") else f[0]
                        if abs(fw - cw) > 0.5:
                            fh = f.size.height if hasattr(f.size, "height") else f[1]
                            objc.super(TodayTableView, self).setFrameSize_(NSMakeSize(cw, fh))
                        cols = self.tableColumns()
                        if cols is not None and cols.count() >= 3:
                            col0 = cols.objectAtIndex_(0)
                            w0 = col0.width() if hasattr(col0, "width") else 250
                            col2 = cols.objectAtIndex_(2)
                            w2 = col2.width() if hasattr(col2, "width") else 56
                            col1 = cols.objectAtIndex_(1)
                            col1.setWidth_(max(100, cw - w0 - w2))
                    except Exception:
                        pass

    class State:
        window = None
        table = None
        empty_label = None
        data_source = []
        opened_paths_in_session = None  # set of path_str: 本次会话内已打开过的，用于已读灰显
        window_delegate = None
        table_ds = None
        btn_delegate = None

    state = State()
    state.opened_paths_in_session = set()
    state.lib_observer = None

    class _LibraryObserver(NSObject):
        def onLibraryDidChange_(self, note):
            # 关联文件夹改动不应影响已入库文件的展示；收到变更通知后刷新今日列表
            try:
                refresh_today_list()
            except Exception:
                pass

    def _reveal_in_finder_for_row(row_index):
        if row_index < 0 or row_index >= len(state.data_source):
            return
        rec = state.data_source[row_index]
        path_str = rec.get("path") or ""
        if not path_str or not Path(path_str).exists():
            return
        subprocess.run(["/usr/bin/open", "-R", path_str], check=False)

    def _finder_button_image():
        """放大镜/在 Finder 中显示 图标。"""
        for name in ("NSRevealInFinderTemplate", "NSTouchBarSearchTemplate", "NSFolder"):
            img = NSImage.imageNamed_(name)
            if img is not None:
                return img
        return NSImage.alloc().initWithSize_((16, 16))

    def refresh_today_list():
        state.data_source = get_today_files()
        if state.table:
            state.table.reloadData()
            # 强制更新所有行视图的背景色
            from AppKit import NSTableRowView, NSColor
            for row in range(len(state.data_source)):
                rowView = state.table.rowViewAtRow_makeIfNecessary_(row, True)
                if rowView:
                    rec = state.data_source[row]
                    path_str = rec.get("path") or ""
                    if path_str in state.opened_paths_in_session:
                        try:
                            gray = NSColor.colorWithCalibratedWhite_alpha_(0.6, 1.0)
                            rowView.setBackgroundColor_(gray)
                        except Exception:
                            pass
                    else:
                        try:
                            rowView.setBackgroundColor_(None)
                        except Exception:
                            pass
        if state.empty_label:
            state.empty_label.setHidden_(len(state.data_source) > 0)

    def open_selected_and_mark():
        if not state.table:
            return
        indexes = state.table.selectedRowIndexes()
        if indexes is None or indexes.count() == 0:
            return
        # 收集所有选中的行（支持 Command 多选）
        rows_to_open = []
        for row in range(state.table.numberOfRows()):
            if indexes.containsIndex_(row) and row < len(state.data_source):
                rows_to_open.append(row)
        if not rows_to_open:
            return
        removed_any = False
        for row in rows_to_open:
            rec = state.data_source[row]
            path_str = rec.get("path") or ""
            if not path_str:
                continue
            p = Path(path_str)
            if not p.exists():
                remove_file(path_str)
                removed_any = True
                continue
            subprocess.run(["/usr/bin/open", path_str], check=False)
            state.opened_paths_in_session.add(path_str)
        if removed_any:
            refresh_today_list()
            alert = NSAlert.alloc().init()
            alert.setMessageText_(L("files_removed_title", "部分文件不存在"))
            alert.setInformativeText_(L("files_removed_info", "已从复习库中移除。"))
            alert.setAlertStyle_(NSAlertStyleInformational)
            alert.runModal()
        else:
            state.table.reloadData()
            # 强制更新所有行视图的背景色
            from AppKit import NSTableRowView, NSColor
            for row in range(len(state.data_source)):
                rowView = state.table.rowViewAtRow_makeIfNecessary_(row, True)
                if rowView:
                    rec = state.data_source[row]
                    path_str = rec.get("path") or ""
                    if path_str in state.opened_paths_in_session:
                        try:
                            gray = NSColor.colorWithCalibratedWhite_alpha_(0.6, 1.0)
                            rowView.setBackgroundColor_(gray)
                        except Exception:
                            pass
                    else:
                        try:
                            rowView.setBackgroundColor_(None)
                        except Exception:
                            pass

    class FinderCellView(NSView):
        """放大镜列容器：悬浮在按钮上时显示手型光标。"""
        def resetCursorRects(self):
            self.discardCursorRects()
            subs = self.subviews()
            if subs is not None and subs.count() > 0:
                r = subs.objectAtIndex_(0).frame()
                self.addCursorRect_cursor_(r, NSCursor.pointingHandCursor())

    _hover_tip = {"panel": None, "bubble": None, "label": None, "timer": None, "hovering": False}

    class TooltipBubbleView(NSView):
        def drawRect_(self, rect):
            try:
                r = self.bounds()
                path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, 7.0, 7.0)
                NSColor.colorWithCalibratedWhite_alpha_(0.12, 0.92).setFill()
                path.fill()
            except Exception:
                pass

    def _ensure_hover_tip_panel():
        if _hover_tip["panel"] is not None:
            return
        # Borderless 小提示框：绕开系统 tooltip 的最小延迟限制
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 160, 28),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        panel.setOpaque_(False)
        try:
            panel.setBackgroundColor_(NSColor.clearColor())
        except Exception:
            pass
        panel.setHasShadow_(True)
        panel.setLevel_(3)  # floating-ish
        panel.setIgnoresMouseEvents_(True)
        panel.setReleasedWhenClosed_(False)
        bubble = TooltipBubbleView.alloc().initWithFrame_(NSMakeRect(0, 0, 160, 28))
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(10, 6, 140, 16))
        label.setBordered_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        try:
            label.setFont_(NSFont.systemFontOfSize_(12))
            label.setTextColor_(NSColor.whiteColor())
        except Exception:
            pass
        bubble.addSubview_(label)
        panel.setContentView_(bubble)
        _hover_tip["panel"] = panel
        _hover_tip["bubble"] = bubble
        _hover_tip["label"] = label

    def _hide_hover_tip():
        try:
            if _hover_tip.get("timer") is not None:
                _hover_tip["timer"].invalidate()
        except Exception:
            pass
        _hover_tip["timer"] = None
        _hover_tip["hovering"] = False
        try:
            if _hover_tip.get("panel") is not None:
                _hover_tip["panel"].orderOut_(None)
        except Exception:
            pass

    def _show_hover_tip_now(text: str, anchor_btn=None):
        _ensure_hover_tip_panel()
        if not _hover_tip["hovering"]:
            return
        try:
            _hover_tip["label"].setStringValue_(text)
        except Exception:
            pass
        # 计算文本宽度，动态调整气泡大小（padding 左右 10，上下 6）
        try:
            s = NSString.stringWithString_(text if text else "")
            font = NSFont.systemFontOfSize_(12)
            size = s.sizeWithAttributes_({NSFontAttributeName: font})
            # 多给一些余量，避免最后一个字被边界裁切（不同字体渲染会略超出 width）
            w = max(80.0, float(size.width) + 28.0)
            h = 28.0
            _hover_tip["panel"].setContentSize_((w, h))
            if _hover_tip.get("bubble") is not None:
                _hover_tip["bubble"].setFrameSize_(NSMakeSize(w, h))
            _hover_tip["label"].setFrame_(NSMakeRect(10, 6, w - 20, 16))
        except Exception:
            w, h = 160.0, 28.0

        # 优先贴着按钮出现（屏幕坐标）；否则回退到鼠标位置
        x, y = None, None
        if anchor_btn is not None:
            try:
                b = anchor_btn.bounds()
                r = anchor_btn.convertRect_toView_(b, None)
                sr = anchor_btn.window().convertRectToScreen_(r)
                x = float(sr.origin.x) + float(sr.size.width) * 0.5 - w * 0.5
                y = float(sr.origin.y) + float(sr.size.height) + 6.0
            except Exception:
                x, y = None, None
        if x is None or y is None:
            try:
                p = NSEvent.mouseLocation()
                x, y = float(p.x) + 12.0, float(p.y) - 34.0
            except Exception:
                x, y = 200.0, 200.0
        try:
            _hover_tip["panel"].setFrameOrigin_((x, y))
            _hover_tip["panel"].orderFront_(None)
        except Exception:
            pass

    def _schedule_hover_tip(text: str, anchor_btn=None, delay_s: float = 0.6):
        _hide_hover_tip()
        _hover_tip["hovering"] = True
        try:
            NSTimer = objc.lookUpClass("NSTimer")
            _hover_tip["timer"] = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                delay_s, state.btn_delegate, "onHoverTipTimer:", {"text": text, "btn": anchor_btn}, False
            )
        except Exception:
            # timer 失败则立即显示
            _show_hover_tip_now(text, anchor_btn=anchor_btn)

    class FinderIconButton(NSButton):
        """放大镜按钮：600ms 后自定义提示框 + 小手光标。"""
        def updateTrackingAreas(self):
            try:
                objc.super(FinderIconButton, self).updateTrackingAreas()
            except Exception:
                pass
            try:
                if getattr(self, "_tracking", None) is not None:
                    self.removeTrackingArea_(self._tracking)
            except Exception:
                pass
            try:
                opts = (
                    NSTrackingMouseEnteredAndExited
                    | NSTrackingActiveAlways
                    | NSTrackingInVisibleRect
                    | NSTrackingCursorUpdate
                )
                self._tracking = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
                    NSMakeRect(0, 0, 0, 0), opts, self, None
                )
                self.addTrackingArea_(self._tracking)
            except Exception:
                self._tracking = None

        def resetCursorRects(self):
            self.discardCursorRects()
            self.addCursorRect_cursor_(self.bounds(), NSCursor.pointingHandCursor())

        def cursorUpdate_(self, event):
            # 有时 resetCursorRects 不会及时触发，用 cursorUpdate 强制设置
            try:
                NSCursor.pointingHandCursor().set()
            except Exception:
                pass

        def mouseEntered_(self, event):
            _schedule_hover_tip(L("finder_show", "在 Finder 中显示"), anchor_btn=self, delay_s=0.6)

        def mouseExited_(self, event):
            _hide_hover_tip()

    class TableDataSource(NSObject):
        def numberOfRowsInTableView_(self, tv):
            return len(state.data_source)

        def tableView_objectValueForTableColumn_row_(self, tv, col, row):
            if row < 0 or row >= len(state.data_source):
                return ""
            rec = state.data_source[row]
            cid = col.identifier()
            if cid == "filename":
                s = Path(rec.get("path") or "").name or "?"
            elif cid == "location":
                s = _short_path(rec.get("source_folder") or rec.get("path") or "")
            else:
                return ""
            path_str = rec.get("path") or ""
            is_read = path_str in state.opened_paths_in_session
            try:
                # 已读行用与未读相同的文字颜色，保证可读；已读仅用背景区分
                color = NSColor.labelColor()
                attr = objc.lookUpClass("NSMutableAttributedString").alloc().initWithString_(s)
                rng = NSMakeRange(0, len(s))
                attr.addAttribute_value_range_("NSForegroundColorAttributeName", color, rng)
                return attr
            except Exception:
                return s

        def tableView_willDisplayCell_forTableColumn_row_(self, tv, cell, col, row):
            # 已读行：浅灰背景（需开启 drawsBackground 并按行设置颜色，避免复用错色）
            if row < 0 or row >= len(state.data_source):
                cell.setDrawsBackground_(False)
                cell.setBackgroundColor_(None)
                return
            rec = state.data_source[row]
            path_str = rec.get("path") or ""
            if path_str in state.opened_paths_in_session:
                try:
                    cell.setDrawsBackground_(True)
                    # 浅灰背景，与截图第二行效果一致
                    gray = NSColor.colorWithCalibratedWhite_alpha_(0.6, 1.0)
                    cell.setBackgroundColor_(gray)
                except Exception:
                    cell.setDrawsBackground_(False)
                    cell.setBackgroundColor_(None)
            else:
                cell.setDrawsBackground_(False)
                cell.setBackgroundColor_(None)

        def tableView_viewForTableColumn_row_(self, tv, col, row):
            """View-based 列：文件名/位置用 NSTableCellView，最后一列用「在 Finder 中显示」按钮（放大镜图标+悬浮提示）。"""
            if row < 0 or row >= len(state.data_source):
                return None
            rec = state.data_source[row]
            cid = col.identifier() if col else ""
            cw = col.width() if col else 100
            if cid == "finder":
                try:
                    row_h = tv.rowHeight()
                except Exception:
                    row_h = 22
                view = tv.makeViewWithIdentifier_owner_("finder", self)
                if view is None:
                    # 列宽 56：左留白 + 图标区 24x20 + 右留白，图标完整不裁切、与滚动条有距
                    view = FinderCellView.alloc().initWithFrame_(NSMakeRect(0, 0, 56, row_h))
                    view.setIdentifier_("finder")
                    btn = FinderIconButton.alloc().initWithFrame_(NSMakeRect(6, (row_h - 20) * 0.5 + 2, 24, 20))
                    btn.setButtonType_(12)
                    btn.setBezelStyle_(0)
                    btn.setBordered_(False)
                    btn.setTitle_("")  # 不显示任何文字，仅图标
                    btn.setImage_(_finder_button_image())
                    btn.setImagePosition_(4)  # 仅显示图片
                    btn.setImageScaling_(2)  # 比例缩放，完整落在 24x20 内
                    btn.setTarget_(state.btn_delegate)
                    btn.setAction_("onRevealInFinder:")
                    view.addSubview_(btn)
                else:
                    view.setFrameSize_(NSMakeSize(cw, row_h))
                    subs = view.subviews()
                    if subs is not None and subs.count() > 0:
                        subs.objectAtIndex_(0).setFrame_(NSMakeRect(6, (row_h - 20) * 0.5 + 2, 24, 20))
                subs = view.subviews()
                if subs is not None and subs.count() > 0:
                    subs.objectAtIndex_(0).setTag_(row)
                return view
            # 文件名、位置列：NSTableCellView + 文本
            try:
                row_h = tv.rowHeight()
            except Exception:
                row_h = 22
            view = tv.makeViewWithIdentifier_owner_(cid, self)
            if view is None:
                view = NSTableCellView.alloc().initWithFrame_(NSMakeRect(0, 0, max(100, cw), row_h))
                view.setIdentifier_(cid)
                tf = NSTextField.alloc().initWithFrame_(NSMakeRect(2, 0, max(0, cw - 4), row_h))
                tf.setCell_(VerticallyCenteredTextFieldCell.alloc().init())
                tf.setBordered_(False)
                tf.setDrawsBackground_(False)
                tf.setEditable_(False)
                tf.setSelectable_(False)
                view.setTextField_(tf)
                view.addSubview_(tf)
            view.setFrameSize_(NSMakeSize(max(100, cw), row_h))
            tf = view.textField()
            if tf is not None:
                tf.setFrame_(NSMakeRect(2, 0, max(0, cw - 4), row_h))
            if cid == "filename":
                view.textField().setStringValue_(Path(rec.get("path") or "").name or "?")
            elif cid == "location":
                view.textField().setStringValue_(_short_path(rec.get("source_folder") or rec.get("path") or ""))
            return view

        def tableView_rowViewForRow_(self, tv, row):
            # 创建自定义行视图，确保背景色能够打满整个行宽
            from AppKit import NSTableRowView
            rowView = NSTableRowView.alloc().init()
            if row < 0 or row >= len(state.data_source):
                return rowView
            rec = state.data_source[row]
            path_str = rec.get("path") or ""
            if path_str in state.opened_paths_in_session:
                try:
                    # 为整行设置背景色，打满整个行宽
                    from AppKit import NSColor
                    gray = NSColor.colorWithCalibratedWhite_alpha_(0.6, 1.0)
                    rowView.setBackgroundColor_(gray)
                except Exception:
                    pass
            else:
                try:
                    rowView.setBackgroundColor_(None)
                except Exception:
                    pass
            return rowView

    class TodayWindowDelegate(NSObject):
        def windowWillClose_(self, notification):
            for path_str in (state.opened_paths_in_session or []):
                try:
                    update_file_after_review(path_str)
                except Exception:
                    pass
            a = objc.lookUpClass("NSApplication").sharedApplication()
            if not a:
                return
            try:
                closing = notification.object() if notification else None
                # 只统计「除正在关闭的窗口外」是否还有其它窗口（不要求可见，避免设置被 orderOut 时误判为 0）
                others = [w for w in (a.windows() or []) if w and w != closing]
                if len(others) == 0:
                    a.terminate_(None)
                else:
                    # 还有其它窗口（如设置），不退出应用，把其中一个推到前面
                    for w in others:
                        w.makeKeyAndOrderFront_(None)
                        break
            except Exception:
                a.terminate_(None)

    class TodayButtonDelegate(NSObject):
        def onOpen_(self, sender):
            open_selected_and_mark()

        def onRevealInFinder_(self, sender):
            row = sender.tag() if sender is not None else -1
            _reveal_in_finder_for_row(row)

        def onHoverTipTimer_(self, timer):
            # userInfo 里传的是提示文字
            try:
                info = timer.userInfo() or {}
                txt = info.get("text") if hasattr(info, "get") else info
                btn = info.get("btn") if hasattr(info, "get") else None
            except Exception:
                txt = L("finder_show", "在 Finder 中显示")
                btn = None
            _show_hover_tip_now((txt if txt else L("finder_show", "在 Finder 中显示")), anchor_btn=btn)

    state.table_ds = TableDataSource.alloc().init()
    state.btn_delegate = TodayButtonDelegate.alloc().init()
    state.window_delegate = TodayWindowDelegate.alloc().init()

    def _refresh_today_ui_strings():
        try:
            if state.window:
                state.window.setTitle_(L("window_today", "今日复习"))
            if state.empty_label:
                state.empty_label.setStringValue_(L("today_empty", "今天没有需要复习的文件。"))
            if getattr(state, "open_btn", None):
                state.open_btn.setTitle_(L("btn_batch_open", "批量打开"))
            if state.table:
                for col in state.table.tableColumns() or []:
                    cid = col.identifier()
                    if cid == "filename":
                        col.setTitle_(L("col_filename", "文件名"))
                    elif cid == "location":
                        col.setTitle_(L("col_location", "位置"))
        except Exception:
            pass

    class TodayLanguageObserver(NSObject):
        def onLanguageDidChange_(self, notification):
            s = getattr(self, "state", None)
            if s and getattr(s, "_refresh_today_ui", None):
                s._refresh_today_ui()

    def build_window():
        # 可拖动调整大小：加上 NSWindowStyleMaskResizable，并设最小尺寸
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
        state.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 600, 420), style, NSBackingStoreBuffered, False
        )
        state.window.setMinSize_((520, 340))
        state.window.setTitle_(L("window_today", "今日复习"))
        state.window.setReleasedWhenClosed_(False)
        state.window.setDelegate_(state.window_delegate)
        content = state.window.contentView()

        # 监听库变更（例如「关联文件夹」增删），确保今日列表自动刷新
        try:
            if state.lib_observer is None:
                state.lib_observer = _LibraryObserver.alloc().init()
                NSNotificationCenter = objc.lookUpClass("NSNotificationCenter")
                NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                    state.lib_observer, "onLibraryDidChange:", LIBRARY_DID_CHANGE_NOTIFICATION, None
                )
        except Exception:
            pass

        # Table（随窗口拉大时表格区域一起变大）
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 50, 560, 318))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(1)  # bezel
        scroll.setAutoresizingMask_(NSViewMinXMargin | NSViewWidthSizable | NSViewMinYMargin | NSViewHeightSizable)
        content.addSubview_(scroll)

        state.table = TodayTableView.alloc().initWithFrame_(scroll.bounds())
        state.table.setRowHeight_(22)  # 略高以便放大镜图标完整显示
        state.table.setIntercellSpacing_((0, 0))
        col1 = NSTableColumn.alloc().initWithIdentifier_("filename")
        col1.setTitle_(L("col_filename", "文件名"))
        col1.setWidth_(250)
        col1.setResizingMask_(0)  # NSTableColumnNoResizing，第一列固定
        state.table.addTableColumn_(col1)
        col2 = NSTableColumn.alloc().initWithIdentifier_("location")
        col2.setTitle_(L("col_location", "位置"))
        col2.setWidth_(300)
        col2.setResizingMask_(NSTableColumnAutoresizingMask)
        state.table.addTableColumn_(col2)
        col_finder = NSTableColumn.alloc().initWithIdentifier_("finder")
        col_finder.setTitle_("")
        col_finder.setWidth_(56)  # 足够宽：图标 24pt + 左右留白，完整显示且与滚动条有距
        col_finder.setResizingMask_(0)
        state.table.addTableColumn_(col_finder)
        # 「位置」列自动拉宽，填满表格
        state.table.setColumnAutoresizingStyle_(3)  # NSTableViewLastColumnOnlyAutoresizingStyle
        state.table.setDataSource_(state.table_ds)
        state.table.setDelegate_(state.table_ds)
        state.table.setDoubleAction_("onOpen:")
        state.table.setTarget_(state.btn_delegate)
        state.table.setAllowsMultipleSelection_(True)  # Command+点击多选，批量打开
        scroll.setDocumentView_(state.table)
        # 表格宽度与 scroll 内容区一致，已读行背景才能铺满整行
        cv = scroll.contentView()
        if cv is not None:
            cw = cv.bounds().size.width
            th = state.table.frame().size.height
            state.table.setFrameSize_(NSMakeSize(cw, th))

        state.empty_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 140, 480, 40))
        state.empty_label.setStringValue_(L("today_empty", "今天没有需要复习的文件。"))
        state.empty_label.setBezeled_(False)
        state.empty_label.setDrawsBackground_(False)
        state.empty_label.setEditable_(False)
        state.empty_label.setAlignment_(1)  # center
        content.addSubview_(state.empty_label)

        open_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, 12, 120, 28))
        open_btn.setTitle_(L("btn_batch_open", "批量打开"))
        open_btn.setTarget_(state.btn_delegate)
        open_btn.setAction_("onOpen:")
        content.addSubview_(open_btn)
        state.open_btn = open_btn

        refresh_today_list()
        state._refresh_today_ui = _refresh_today_ui_strings
        _obs = TodayLanguageObserver.alloc().init()
        _obs.state = state
        state.lang_observer = _obs
        try:
            objc.lookUpClass("NSNotificationCenter").defaultCenter().addObserver_selector_name_object_(
                _obs, "onLanguageDidChange:", LANGUAGE_DID_CHANGE_NOTIFICATION, None
            )
        except Exception:
            pass
        state.window.center()
        if not for_embed:
            state.window.makeKeyAndOrderFront_(None)

    # When embedding (for_embed=True), we only need to construct and return a window.
    # Avoid re-initializing NSApplication here (it can fail in some dev contexts and cause today_win=None).
    if for_embed:
        build_window()
        if state.window is None:
            raise RuntimeError("今日复习窗口 build_window() 未创建窗口（state.window 为 None）。")
        return state.window

    NSApplication = objc.lookUpClass("NSApplication")
    app = NSApplication.sharedApplication()
    if app is None:
        app = NSApplication.alloc().init()
    if app is None:
        raise RuntimeError("无法初始化 NSApplication（Today UI）。")
    app.setActivationPolicy_(0)
    app.activateIgnoringOtherApps_(True)
    build_window()
    app.run()
    return None


# ---------- Settings GUI ----------

def run_settings_gui(for_embed=False):
    """PyObjC/Cocoa Settings window: schedule, associated folders, Add folder/file.
    If for_embed=True, only build and return the window (don't run app).
    """
    if platform.system() != "Darwin":
        run_scheduled_flow()
        return None
    try:
        import objc
        from AppKit import (
            NSWindow, NSTextField, NSButton, NSPopUpButton, NSAlert, NSScrollView, NSTableView,
            NSOpenPanel, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, NSAlertStyleInformational, NSMakeRect,
            NSTableColumn, NSTableColumnAutoresizingMask,
        )
        from Foundation import NSObject
        from PyObjCTools import AppHelper
    except ImportError as e:
        raise RuntimeError("设置窗口依赖 PyObjC/AppKit 导入失败，请检查 pyobjc 是否已安装。") from e

    class State:
        status_label = None
        hour_popup = None
        minute_popup = None
        window = None
        folders_table = None
        folders_data = []
        empty_folders_label = None
        remove_folder_btn = None
        window_delegate = None
        folders_ds = None
        folders_delegate = None
        btn_delegate = None
        add_folder_btn = None
        add_file_btn = None
        busy_label = None
        is_busy = False

    state = State()

    def _set_busy(is_busy: bool, text=None):
        state.is_busy = is_busy
        if state.add_folder_btn:
            state.add_folder_btn.setEnabled_(not is_busy)
        if state.add_file_btn:
            state.add_file_btn.setEnabled_(not is_busy)
        if state.remove_folder_btn:
            row = state.folders_table.selectedRow() if state.folders_table else -1
            state.remove_folder_btn.setEnabled_((not is_busy) and row >= 0 and row < len(state.folders_data))
        if state.busy_label:
            if text:
                state.busy_label.setStringValue_(text)
            state.busy_label.setHidden_(not is_busy)

    def refresh_folders_list():
        lib = get_library()
        state.folders_data = list(lib.get("folders") or [])
        if state.folders_table:
            state.folders_table.reloadData()
        if state.empty_folders_label:
            state.empty_folders_label.setHidden_(len(state.folders_data) > 0)
        if state.remove_folder_btn:
            row = state.folders_table.selectedRow() if state.folders_table else -1
            state.remove_folder_btn.setEnabled_((not state.is_busy) and row >= 0 and row < len(state.folders_data))

    def refresh_status():
        if state.status_label is None:
            return
        if not LAUNCH_AGENT_PLIST.exists():
            state.status_label.setStringValue_(L("status_not_installed", "定时未安装"))
        else:
            state.status_label.setStringValue_(
                L("status_on", "定时已开启") if _is_launch_agent_loaded() else L("status_off", "定时已关闭")
            )

    def _refresh_settings_ui_strings():
        try:
            if state.window:
                state.window.setTitle_(L("window_settings", "日复一日 — 设置"))
            if getattr(state, "settings_lang_label", None):
                state.settings_lang_label.setStringValue_(L("ui_language", "界面语言："))
            if getattr(state, "lang_popup", None):
                state.lang_popup.removeAllItems()
                state.lang_popup.addItemsWithTitles_([
                    L("lang_follow_system", "跟随系统"),
                    L("lang_zh_hans", "简体中文"),
                    L("lang_en", "English"),
                ])
                pref = _get_lang_preference()
                state.lang_popup.selectItemAtIndex_(1 if pref == "zh-Hans" else (2 if pref == "en" else 0))
            if getattr(state, "settings_schedule_label", None):
                state.settings_schedule_label.setStringValue_(L("schedule_status", "定时提醒状态："))
            refresh_status()
            if getattr(state, "settings_daily_label", None):
                state.settings_daily_label.setStringValue_(L("daily_time", "每日提醒时间："))
            if getattr(state, "settings_time_label", None):
                state.settings_time_label.setStringValue_(L("time_hour_min", "时 分"))
            if getattr(state, "settings_save_btn", None):
                state.settings_save_btn.setTitle_(L("btn_save_schedule", "保存并更新定时"))
            if getattr(state, "settings_disable_btn", None):
                state.settings_disable_btn.setTitle_(L("btn_disable_schedule", "关闭定时"))
            if getattr(state, "settings_run_btn", None):
                state.settings_run_btn.setTitle_(L("btn_run_now", "立即运行"))
            if getattr(state, "settings_folders_label", None):
                state.settings_folders_label.setStringValue_(L("folders_section", "关联文件夹："))
            if state.folders_table:
                for col in state.folders_table.tableColumns() or []:
                    if col.identifier() == "path":
                        col.setTitle_(L("col_path", "路径"))
                        break
            if state.empty_folders_label:
                state.empty_folders_label.setStringValue_(L("empty_folders", "暂无关联文件夹，请点击「添加文件夹」或「添加文件」添加。"))
            if state.add_folder_btn:
                state.add_folder_btn.setTitle_(L("btn_add_folder", "添加文件夹"))
            if state.remove_folder_btn:
                state.remove_folder_btn.setTitle_(L("btn_remove", "移除"))
            if state.add_file_btn:
                state.add_file_btn.setTitle_(L("btn_add_file", "添加文件"))
            if getattr(state, "settings_reset_btn", None):
                state.settings_reset_btn.setTitle_(L("btn_reset_data", "一键清空数据"))
        except Exception:
            pass

    class SettingsLanguageObserver(NSObject):
        def onLanguageDidChange_(self, notification):
            s = getattr(self, "state", None)
            if s and getattr(s, "_refresh_settings_ui", None):
                s._refresh_settings_ui()

    def load_schedule():
        h, m = get_schedule_from_plist()
        if state.hour_popup:
            state.hour_popup.selectItemAtIndex_(h)
        if state.minute_popup:
            state.minute_popup.selectItemAtIndex_(m)

    def on_save_():
        h = state.hour_popup.indexOfSelectedItem() if state.hour_popup else 8
        m = state.minute_popup.indexOfSelectedItem() if state.minute_popup else 0
        _write_launch_agent_plist(h, m)
        _launchctl_unload()
        _launchctl_load()
        refresh_status()
        _show_alert(L("alert_hint", "提示"), L("alert_schedule_saved", "已保存并更新定时提醒时间。"))

    def on_disable_():
        if not LAUNCH_AGENT_PLIST.exists():
            _show_alert(L("alert_hint", "提示"), L("alert_schedule_not_installed", "当前未安装定时任务。"))
            return
        _launchctl_unload()
        refresh_status()
        _show_alert(L("alert_hint", "提示"), L("alert_schedule_disabled", "已关闭定时提醒；可随时重新「保存并更新定时」以再次开启。"))

    def on_run_now_():
        def done(notif_title, notif_body):
            def _on_main():
                if notif_title is not None and notif_body is not None:
                    _deliver_notification_native_no_wait(notif_body, title=notif_title)
                # app 在前台时系统可能不弹通知条，统一弹窗提示「今日复习时间到」
                msg = (notif_body or L("alert_refreshed", "已刷新今日列表，可到「今日复习」查看。"))
                _show_alert(L("alert_review_time", "今日复习时间到！"), msg)
            AppHelper.callAfter(_on_main)
        _run_scheduled_in_thread(done)

    def _show_alert(title, msg):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(msg)
        alert.setAlertStyle_(NSAlertStyleInformational)
        alert.runModal()

    def on_add_folder_():
        if state.is_busy:
            return
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        if panel.runModal() != 1:  # NSModalResponseOK
            return
        url = panel.URL()
        if url is None:
            return
        path = url.path()
        if not path:
            return
        _set_busy(True, L("busy_adding_folder", "正在添加文件夹并扫描文件，请稍候…"))

        def work():
            try:
                result = add_folder(path)
            except Exception as e:
                traceback.print_exc()
                result = None

            def done():
                _set_busy(False, "")
                refresh_folders_list()
                if result is None:
                    _show_alert(L("alert_add_folder_fail", "添加文件夹失败"), L("alert_add_folder_fail_detail", "请查看终端/日志了解详细错误。"))
                else:
                    _show_alert(
                        L("alert_add_folder_ok", "添加文件夹"),
                        (L("alert_add_folder_result", "已添加 %d，跳过 %d，忽略 %d。") % (result["added"], result["skipped"], result["ignored"])),
                    )

            AppHelper.callAfter(done)

        threading.Thread(target=work, daemon=True).start()

    def on_remove_folder_():
        if state.is_busy:
            return
        row = state.folders_table.selectedRow() if state.folders_table else -1
        if row < 0 or row >= len(state.folders_data):
            _show_alert(L("alert_hint", "提示"), L("alert_select_folder", "请先选择要移除的文件夹。"))
            return
        folder_str = state.folders_data[row]
        lib = get_library()
        lib["folders"] = [f for f in lib["folders"] if f != folder_str]
        save_library()
        _post_library_did_change()
        refresh_folders_list()
        try:
            state.folders_table.deselectAll_(None)
        except Exception:
            pass

    def on_add_file_():
        if state.is_busy:
            return
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(True)
        if panel.runModal() != 1:
            return
        urls = panel.URLs()
        if not urls:
            return
        paths = [u.path() for u in urls if u and u.path()]
        if not paths:
            return
        _set_busy(True, L("busy_adding_file", "正在添加文件，请稍候…"))

        def work():
            try:
                added, skipped = 0, 0
                for p in paths:
                    r = add_file(p)
                    if r == "added":
                        added += 1
                    else:
                        skipped += 1
                result = (added, skipped)
            except Exception:
                traceback.print_exc()
                result = None

            def done():
                _set_busy(False, "")
                refresh_folders_list()
                if result is None:
                    _show_alert(L("alert_add_file_fail", "添加文件失败"), L("alert_add_folder_fail_detail", "请查看终端/日志了解详细错误。"))
                else:
                    a, s = result
                    _show_alert(L("alert_add_file_ok", "添加文件"), L("alert_add_file_result", "已添加 %d，跳过 %d。") % (a, s))

            AppHelper.callAfter(done)

        threading.Thread(target=work, daemon=True).start()

    class FoldersTableDataSource(NSObject):
        def numberOfRowsInTableView_(self, tv):
            return len(state.folders_data)

        def tableView_objectValueForTableColumn_row_(self, tv, col, row):
            if row < 0 or row >= len(state.folders_data):
                return ""
            return _short_path(state.folders_data[row])

    class FoldersTableDelegate(NSObject):
        def tableViewSelectionDidChange_(self, notification):
            if state.remove_folder_btn and state.folders_table:
                row = state.folders_table.selectedRow()
                state.remove_folder_btn.setEnabled_((not state.is_busy) and row >= 0 and row < len(state.folders_data))

    class SettingsWindowDelegate(NSObject):
        def windowWillClose_(self, notification):
            # 关闭「设置」窗口不应退出应用；由「今日复习」窗口负责决定是否退出
            return

    class SettingsButtonDelegate(NSObject):
        def onSave_(self, sender):
            on_save_()
        def onDisable_(self, sender):
            on_disable_()
        def onRunNow_(self, sender):
            on_run_now_()
        def onAddFolder_(self, sender):
            on_add_folder_()
        def onRemoveFolder_(self, sender):
            on_remove_folder_()
        def onAddFile_(self, sender):
            on_add_file_()
        def onLanguageChange_(self, sender):
            idx = sender.indexOfSelectedItem() if sender else 0
            lang = ("", "zh-Hans", "en")[idx] if idx in (0, 1, 2) else ""
            _set_lang_preference(lang)
            try:
                nc = objc.lookUpClass("NSNotificationCenter").defaultCenter()
                nc.postNotificationName_object_(LANGUAGE_DID_CHANGE_NOTIFICATION, None)
            except Exception:
                pass

        def onReset_(self, sender):
            alert = NSAlert.alloc().init()
            alert.setMessageText_(L("reset_confirm_title", "确认清空"))
            alert.setInformativeText_(L("reset_confirm_msg", "将清空所有关联文件夹与复习记录，此操作不可恢复。确定继续？"))
            alert.setAlertStyle_(3)  # NSWarningAlertStyle
            alert.addButtonWithTitle_(L("reset_confirm_btn", "确定继续"))  # first = default
            alert.addButtonWithTitle_(L("cancel", "取消"))  # second = cancel
            if alert.runModal() != 1000:  # NSAlertFirstButtonReturn
                return
            reset_all_data()
            refresh_folders_list()
            _show_alert(L("reset_confirm_title", "确认清空"), L("reset_done", "数据已清空。"))

    state.folders_ds = FoldersTableDataSource.alloc().init()
    state.folders_delegate = FoldersTableDelegate.alloc().init()
    state.btn_delegate = SettingsButtonDelegate.alloc().init()
    state.window_delegate = SettingsWindowDelegate.alloc().init()

    def build_window():
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        state.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 420, 408), style, NSBackingStoreBuffered, False
        )
        state.window.setTitle_(L("window_settings", "日复一日 — 设置"))
        state.window.setReleasedWhenClosed_(False)
        state.window.setDelegate_(state.window_delegate)
        content = state.window.contentView()
        y = 376

        # 界面语言（手动切换中/英）
        lang_lab = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, 100, 20))
        lang_lab.setStringValue_(L("ui_language", "界面语言："))
        lang_lab.setBezeled_(False)
        lang_lab.setDrawsBackground_(False)
        lang_lab.setEditable_(False)
        content.addSubview_(lang_lab)
        state.lang_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(120, y - 2, 160, 24), False)
        state.lang_popup.addItemsWithTitles_([
            L("lang_follow_system", "跟随系统"),
            L("lang_zh_hans", "简体中文"),
            L("lang_en", "English"),
        ])
        pref = _get_lang_preference()
        state.lang_popup.selectItemAtIndex_(1 if pref == "zh-Hans" else (2 if pref == "en" else 0))
        state.lang_popup.setTarget_(state.btn_delegate)
        state.lang_popup.setAction_("onLanguageChange:")
        content.addSubview_(state.lang_popup)
        state.settings_lang_label = lang_lab
        y -= 28

        # Schedule status
        lab = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, 120, 20))
        lab.setStringValue_(L("schedule_status", "定时提醒状态："))
        lab.setBezeled_(False)
        lab.setDrawsBackground_(False)
        lab.setEditable_(False)
        content.addSubview_(lab)
        state.settings_schedule_label = lab
        state.status_label = NSTextField.alloc().initWithFrame_(NSMakeRect(140, y, 260, 20))
        state.status_label.setStringValue_("")
        state.status_label.setBezeled_(False)
        state.status_label.setDrawsBackground_(False)
        state.status_label.setEditable_(False)
        content.addSubview_(state.status_label)
        y -= 28

        # Time
        lab2 = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, 120, 20))
        lab2.setStringValue_(L("daily_time", "每日提醒时间："))
        lab2.setBezeled_(False)
        lab2.setDrawsBackground_(False)
        lab2.setEditable_(False)
        content.addSubview_(lab2)
        state.settings_daily_label = lab2
        state.hour_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(140, y - 2, 60, 24), False)
        state.hour_popup.addItemsWithTitles_([str(i) for i in range(24)])
        content.addSubview_(state.hour_popup)
        state.minute_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(210, y - 2, 60, 24), False)
        state.minute_popup.addItemsWithTitles_([str(i) for i in range(60)])
        content.addSubview_(state.minute_popup)
        lab3 = NSTextField.alloc().initWithFrame_(NSMakeRect(278, y, 40, 20))
        lab3.setStringValue_(L("time_hour_min", "时 分"))
        lab3.setBezeled_(False)
        lab3.setDrawsBackground_(False)
        lab3.setEditable_(False)
        content.addSubview_(lab3)
        state.settings_time_label = lab3
        y -= 36

        # Schedule buttons
        btn_w, btn_h, pad = 110, 28, 8
        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, btn_w, btn_h))
        save_btn.setTitle_(L("btn_save_schedule", "保存并更新定时"))
        save_btn.setTarget_(state.btn_delegate)
        save_btn.setAction_("onSave:")
        content.addSubview_(save_btn)
        state.settings_save_btn = save_btn
        dis_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20 + btn_w + pad, y, 80, btn_h))
        dis_btn.setTitle_(L("btn_disable_schedule", "关闭定时"))
        dis_btn.setTarget_(state.btn_delegate)
        dis_btn.setAction_("onDisable:")
        content.addSubview_(dis_btn)
        state.settings_disable_btn = dis_btn
        run_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20 + btn_w + pad + 80 + pad, y, 80, btn_h))
        run_btn.setTitle_(L("btn_run_now", "立即运行"))
        run_btn.setTarget_(state.btn_delegate)
        run_btn.setAction_("onRunNow:")
        content.addSubview_(run_btn)
        state.settings_run_btn = run_btn
        y -= 44

        # Associated folders
        lab4 = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, 200, 20))
        lab4.setStringValue_(L("folders_section", "关联文件夹："))
        lab4.setBezeled_(False)
        lab4.setDrawsBackground_(False)
        lab4.setEditable_(False)
        content.addSubview_(lab4)
        state.settings_folders_label = lab4
        y -= 24

        # 表格区域：留出足够空间给滚动条，避免被裁切
        scroll_w, scroll_h = 382, 102
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(18, y - scroll_h, scroll_w, scroll_h))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(1)
        scroll.setDrawsBackground_(True)
        content.addSubview_(scroll)
        state.folders_table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, scroll_w - 20, 0))
        col = NSTableColumn.alloc().initWithIdentifier_("path")
        col.setTitle_(L("col_path", "路径"))
        col.setWidth_(scroll_w - 24)
        col.setResizingMask_(NSTableColumnAutoresizingMask)
        state.folders_table.addTableColumn_(col)
        state.folders_table.setDataSource_(state.folders_ds)
        state.folders_table.setDelegate_(state.folders_delegate)
        scroll.setDocumentView_(state.folders_table)

        # 空状态提示
        state.empty_folders_label = NSTextField.alloc().initWithFrame_(NSMakeRect(28, y - scroll_h + 44, scroll_w - 20, 36))
        state.empty_folders_label.setStringValue_(L("empty_folders", "暂无关联文件夹，请点击「添加文件夹」或「添加文件」添加。"))
        state.empty_folders_label.setBezeled_(False)
        state.empty_folders_label.setDrawsBackground_(False)
        state.empty_folders_label.setEditable_(False)
        state.empty_folders_label.setAlignment_(1)  # center
        state.empty_folders_label.setTextColor_(objc.lookUpClass("NSColor").secondaryLabelColor())
        content.addSubview_(state.empty_folders_label)

        # 表格与按钮之间留足间距，避免滚动区域覆盖按钮
        y -= scroll_h + 40

        # Busy label
        # busy 文案放在按钮上方一行
        state.busy_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y + 30, 380, 18))
        state.busy_label.setStringValue_("")
        state.busy_label.setBezeled_(False)
        state.busy_label.setDrawsBackground_(False)
        state.busy_label.setEditable_(False)
        state.busy_label.setHidden_(True)
        content.addSubview_(state.busy_label)

        add_f_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, 100, 26))
        add_f_btn.setTitle_(L("btn_add_folder", "添加文件夹"))
        add_f_btn.setTarget_(state.btn_delegate)
        add_f_btn.setAction_("onAddFolder:")
        state.add_folder_btn = add_f_btn
        content.addSubview_(add_f_btn)
        rem_btn = NSButton.alloc().initWithFrame_(NSMakeRect(128, y, 80, 26))
        rem_btn.setTitle_(L("btn_remove", "移除"))
        rem_btn.setTarget_(state.btn_delegate)
        rem_btn.setAction_("onRemoveFolder:")
        rem_btn.setEnabled_(False)
        state.remove_folder_btn = rem_btn
        content.addSubview_(rem_btn)
        add_file_btn = NSButton.alloc().initWithFrame_(NSMakeRect(216, y, 100, 26))
        add_file_btn.setTitle_(L("btn_add_file", "添加文件"))
        add_file_btn.setTarget_(state.btn_delegate)
        add_file_btn.setAction_("onAddFile:")
        state.add_file_btn = add_file_btn
        content.addSubview_(add_file_btn)
        y -= 34
        reset_btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, 160, 26))
        reset_btn.setTitle_(L("btn_reset_data", "一键清空数据"))
        reset_btn.setTarget_(state.btn_delegate)
        reset_btn.setAction_("onReset:")
        try:
            reset_btn.setBezelColor_(objc.lookUpClass("NSColor").systemRedColor())
        except Exception:
            pass
        content.addSubview_(reset_btn)
        state.settings_reset_btn = reset_btn

        refresh_status()
        load_schedule()
        refresh_folders_list()
        state._refresh_settings_ui = _refresh_settings_ui_strings
        _obs = SettingsLanguageObserver.alloc().init()
        _obs.state = state
        state.settings_lang_observer = _obs
        try:
            objc.lookUpClass("NSNotificationCenter").defaultCenter().addObserver_selector_name_object_(
                _obs, "onLanguageDidChange:", LANGUAGE_DID_CHANGE_NOTIFICATION, None
            )
        except Exception:
            pass
        state.window.center()
        if for_embed:
            return state.window
        state.window.makeKeyAndOrderFront_(None)

    # 打包环境下 NSApp() 可能为 None，改用 NSApplication.sharedApplication() 并必要时 init
    NSApplication = objc.lookUpClass("NSApplication")
    app = NSApplication.sharedApplication()
    if app is None:
        app = NSApplication.alloc().init()
    if app is None:
        run_scheduled_flow()
        return None
    app.setActivationPolicy_(0)  # Regular app，有 Dock 图标方便点设置
    app.activateIgnoringOtherApps_(True)
    build_window()
    if for_embed:
        return state.window
    app.run()
    return None


# ---------- 统一 GUI 入口：今日 + 设置，未关联文件/文件夹时默认进设置 ----------

def run_app_gui():
    """单窗口 + 顶部选项卡：今日复习 | 设置；今日复习为首项并默认突出显示。"""
    if platform.system() != "Darwin":
        raise RuntimeError("GUI only supports macOS (Darwin).")
    try:
        import objc
        from AppKit import (
            NSWindow, NSView, NSSegmentedControl, NSMakeRect,
            NSWindowStyleMaskTitled, NSWindowStyleMaskClosable, NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            NSViewWidthSizable, NSViewHeightSizable, NSViewMinYMargin,
            NSAlert, NSAlertStyleInformational,
        )
        from Foundation import NSObject
        from PyObjCTools import AppHelper
    except ImportError:
        raise RuntimeError("缺少 PyObjC 依赖：请先安装 pyobjc（例如 `pip install pyobjc`）。")

    NSApplication = objc.lookUpClass("NSApplication")
    app = NSApplication.sharedApplication()
    if app is None:
        app = NSApplication.alloc().init()
    if app is None:
        raise RuntimeError("无法初始化 NSApplication，GUI 启动失败。")

    load_library()
    scan_folders_and_add_new()
    lib = get_library()
    folders = lib.get("folders") or []
    files_list = lib.get("files") or []
    has_library_content = bool(folders or files_list)

    if has_library_content:
        show_today_first = True
        if "--show-today" not in sys.argv:
            consume_show_today_flag()
    else:
        show_today_first = False

    app.setActivationPolicy_(0)
    app.activateIgnoringOtherApps_(True)

    # 尽早设置悬浮提示延迟（250ms），否则系统默认约 1s 才出现
    try:
        ttm = objc.lookUpClass("NSToolTipManager").sharedToolTipManager()
        if ttm is not None:
            ttm.setInitialDelay_(0.25)
    except Exception:
        pass

    today_win = run_today_ui(settings_window=None, for_embed=True)
    if today_win is None:
        raise RuntimeError("今日复习窗口创建失败（today_win=None）。")
    settings_win = run_settings_gui(for_embed=True)
    if settings_win is None:
        raise RuntimeError("设置窗口创建失败（settings_win=None）。")

    # 主窗口：一个窗口 + 顶部选项卡
    main_rect = NSMakeRect(0, 0, 620, 460)
    style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
    main_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        main_rect, style, NSBackingStoreBuffered, False
    )
    main_win.setMinSize_((520, 380))
    main_win.setTitle_(L("app_title", "日复一日"))
    main_win.setReleasedWhenClosed_(False)

    content = main_win.contentView()
    cw, ch = content.bounds().size.width, content.bounds().size.height
    seg_h, margin = 32, 12
    seg = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(margin, ch - margin - seg_h, 220, seg_h))
    seg.setSegmentCount_(2)
    seg.setLabel_forSegment_(L("tab_today", "今日复习"), 0)
    seg.setLabel_forSegment_(L("tab_settings", "设置"), 1)
    seg.setSelectedSegment_(0 if show_today_first else 1)
    seg.setAutoresizingMask_(NSViewMinYMargin)

    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, cw, ch - margin * 2 - seg_h))
    container.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

    today_content = today_win.contentView()
    today_content.removeFromSuperview()
    settings_content = settings_win.contentView()
    settings_content.removeFromSuperview()

    container.addSubview_(today_content)
    container.addSubview_(settings_content)
    today_content.setFrame_(container.bounds())
    settings_content.setFrame_(container.bounds())
    today_content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
    settings_content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)

    # 从今日内容里取出表格，用于切换到此 tab 时设为 first responder，保证选中高亮（蓝色）正常
    NSScrollView = objc.lookUpClass("NSScrollView")
    today_table = None
    for v in today_content.subviews():
        if v.isKindOfClass_(NSScrollView):
            doc = v.documentView()
            if doc is not None:
                today_table = doc
            break

    if show_today_first:
        today_content.setHidden_(False)
        settings_content.setHidden_(True)
    else:
        today_content.setHidden_(True)
        settings_content.setHidden_(False)

    class SegmentDelegate(NSObject):
        def init(self):
            self = objc.super(SegmentDelegate, self).init()
            if self is None:
                return None
            self.today_content = None
            self.settings_content = None
            self.main_win = None
            self.today_table = None
            self.main_delegate = None
            return self

        def onSegment_(self, sender):
            sel = sender.selectedSegment()
            if sel == 0:
                self.today_content.setHidden_(False)
                self.settings_content.setHidden_(True)
                if self.today_table is not None and self.main_win is not None:
                    # 切回「今日复习」时用 0 延迟定时器再聚焦一次，恢复选中行蓝色高亮
                    try:
                        self.main_win.makeKeyAndOrderFront_(None)
                    except Exception:
                        pass
                    if self.main_delegate is not None:
                        try:
                            NSTimer = objc.lookUpClass("NSTimer")
                            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                                0.0, self.main_delegate, "timerFocusTable:", None, False
                            )
                        except Exception:
                            self.main_win.makeFirstResponder_(self.today_table)
                    else:
                        self.main_win.makeFirstResponder_(self.today_table)
            else:
                self.today_content.setHidden_(True)
                self.settings_content.setHidden_(False)

    seg_del = SegmentDelegate.alloc().init()
    seg_del.today_content = today_content
    seg_del.settings_content = settings_content
    seg_del.main_win = main_win
    seg_del.today_table = today_table
    seg.setTarget_(seg_del)
    seg.setAction_("onSegment:")

    # app 前台时：每日到点只由 launchd 起新进程发通知（常被系统静默），此处用定时器到点执行同逻辑并弹窗
    last_reminder_at = [None]  # (date, hour, minute) 已弹过则不再弹；按「日期+时分」去重，允许同一天改时间后再到点弹窗

    class MainWindowDelegate(NSObject):
        def init(self):
            self = objc.super(MainWindowDelegate, self).init()
            if self is None:
                return None
            self.today_table = None
            self.today_content = None
            self.main_win = None
            self.seg = None
            return self

        def onLanguageDidChange_(self, notification):
            try:
                if self.main_win:
                    self.main_win.setTitle_(L("app_title", "日复一日"))
                if self.seg:
                    self.seg.setLabel_forSegment_(L("tab_today", "今日复习"), 0)
                    self.seg.setLabel_forSegment_(L("tab_settings", "设置"), 1)
            except Exception:
                pass

        def windowWillClose_(self, notification):
            try:
                app = NSApplication.sharedApplication()
                if app:
                    app.terminate_(None)
            except Exception:
                pass

        def windowDidBecomeKey_(self, notification):
            # 窗口重新激活时，若当前是「今日复习」tab，把焦点给表格，避免列表像冻住一样点不了
            if self.today_table is None or self.today_content is None or self.main_win is None:
                return
            if not self.today_content.isHidden():
                self.main_win.makeFirstResponder_(self.today_table)

        def timerFocusTable_(self, timer):
            # 启动后延迟约 0.4 秒再设 first responder，确保视图层级完全就绪
            if self.today_table is not None and self.main_win is not None:
                if self.today_content is not None and not self.today_content.isHidden():
                    self.main_win.makeFirstResponder_(self.today_table)

        def onScheduleTick_(self, timer):
            # 每日提醒时间到且 app 在前台：执行今日复习逻辑并弹窗「今日复习时间到！」
            if not _is_launch_agent_loaded():
                return
            h, m = get_schedule_from_plist()
            now = datetime.now()
            if now.hour != h or now.minute != m:
                return
            today = now.date()
            if last_reminder_at[0] == (today, h, m):
                return
            last_reminder_at[0] = (today, h, m)

            def done(notif_title, notif_body):
                def _on_main():
                    if notif_title is not None and notif_body is not None:
                        _deliver_notification_native_no_wait(notif_body, title=notif_title)
                    msg = notif_body or L("alert_refreshed", "已刷新今日列表，可到「今日复习」查看。")
                    alert = NSAlert.alloc().init()
                    alert.setMessageText_(L("alert_review_time", "今日复习时间到！"))
                    alert.setInformativeText_(msg)
                    alert.setAlertStyle_(NSAlertStyleInformational)
                    alert.runModal()
                AppHelper.callAfter(_on_main)
            _run_scheduled_in_thread(done)

    NSTimer = objc.lookUpClass("NSTimer")
    main_delegate = MainWindowDelegate.alloc().init()
    main_delegate.today_table = today_table
    main_delegate.today_content = today_content
    main_delegate.main_win = main_win
    main_delegate.seg = seg
    try:
        objc.lookUpClass("NSNotificationCenter").defaultCenter().addObserver_selector_name_object_(
            main_delegate, "onLanguageDidChange:", LANGUAGE_DID_CHANGE_NOTIFICATION, None
        )
    except Exception:
        pass
    # 让 tab 切换可复用 main_delegate 的 timerFocusTable_，恢复蓝色选中高亮
    try:
        seg_del.main_delegate = main_delegate
    except Exception:
        pass
    main_win.setDelegate_(main_delegate)
    content.addSubview_(container)
    content.addSubview_(seg)

    # 「立即运行」发出的通知点击时，由本 delegate 把主窗口前置（与定时通知点击效果一致）
    NSUserNotificationCenter = objc.lookUpClass("NSUserNotificationCenter")

    class NotificationClickDelegate(NSObject):
        def init(self):
            self = objc.super(NotificationClickDelegate, self).init()
            if self is None:
                return None
            self.main_win_ref = None
            return self

        def userNotificationCenter_didActivateNotification_(self, center, notification):
            if self.main_win_ref is not None:
                try:
                    self.main_win_ref.makeKeyAndOrderFront_(None)
                except Exception:
                    pass

    notif_click_delegate = NotificationClickDelegate.alloc().init()
    notif_click_delegate.main_win_ref = main_win
    NSUserNotificationCenter.defaultUserNotificationCenter().setDelegate_(notif_click_delegate)

    # today_win / settings_win 保持在 run_app_gui 局部变量中，避免被回收
    main_win.center()
    if show_today_first and today_table is not None:
        main_win.setInitialFirstResponder_(today_table)
    main_win.makeKeyAndOrderFront_(None)
    if show_today_first and today_table is not None:
        # 用 NSTimer 延迟 0.4 秒再设 first responder，避免刚显示时视图未就绪导致列表“冻住”
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.4, main_delegate, "timerFocusTable:", None, False
        )
    # 每分钟检查是否到设定提醒时间，到点则执行今日复习逻辑并弹窗（app 前台时 launchd 通知常被静默）
    # 使用 10 秒间隔而非 60 秒，避免 60 秒相位导致设定分钟永远碰不到（app 一直开着时到点不弹窗）
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        10, main_delegate, "onScheduleTick:", None, True
    )
    app.run()


# ---------- 入口 ----------

def _log_and_show_error(msg: str):
    """打包后出错时写入日志并尽量弹窗，避免静默退出。"""
    traceback.print_exc()
    log_path = Path("/tmp/learning_review_error.log")
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] {msg}\n")
            traceback.print_exc(file=f)
    except Exception:
        pass
    if platform.system() == "Darwin":
        try:
            from AppKit import NSApp, NSAlert
            app = NSApp()
            app.setActivationPolicy_(1)  # accessory
            alert = NSAlert.alloc().init()
            alert.setMessageText_(L("alert_app_error", "日复一日 启动出错"))
            alert.setInformativeText_(msg[: 500] if len(msg) > 500 else msg)
            alert.setAlertStyle_(3)  # critical
            alert.runModal()
        except Exception:
            pass




def _startup_log(msg: str):
    """启动阶段写日志到文件，便于排查打不开时崩溃位置。"""
    try:
        p = Path.home() / ".learning-review" / "startup.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception:
        pass


if __name__ == "__main__":
    _startup_log("main enter")
    try:
        _startup_log("platform=" + str(platform.system()))
        if "--scheduled" in sys.argv:
            run_scheduled_flow()
            # 用户点击了通知时，在当前进程直接拉起 GUI，保证能打开
            if _notification_clicked_show_today[0]:
                _notification_clicked_show_today[0] = False
                if "--show-today" not in sys.argv:
                    sys.argv.append("--show-today")
                if platform.system() == "Darwin":
                    ensure_launch_agent_on_first_run()
                    run_app_gui()
        elif "--show-today" in sys.argv:
            if platform.system() == "Darwin":
                ensure_launch_agent_on_first_run()
                run_app_gui()  # 内部会根据是否有库内容决定先展示今日还是设置
            else:
                run_scheduled_flow()
        else:
            if platform.system() == "Darwin":
                _startup_log("calling ensure_launch_agent_on_first_run")
                ensure_launch_agent_on_first_run()
                _startup_log("calling run_app_gui")
                run_app_gui()
                _startup_log("run_app_gui returned")
            else:
                run_scheduled_flow()
    except Exception as e:
        _startup_log("exception: " + str(e))
        _log_and_show_error(str(e) + "\n" + traceback.format_exc())
