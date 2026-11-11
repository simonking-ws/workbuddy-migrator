# -*- coding: utf-8 -*-
"""
WorkBuddy 数据迁移工具 (目录联接版)
====================================

一个零依赖的 Windows 桌面程序：把 %USERPROFILE%\\.workbuddy 下体积较大的
纯数据子目录迁移到其它盘（默认 E:\\WorkBuddyData），并在原位置建立 NTFS
目录联接 (junction /J)。应用看到的路径不变，字节实际落在目标盘。

设计原则
--------
1. 绝不删除源数据。源目录只被改名为 "<name>.bak"，随时可回滚。
2. 只迁移「纯数据、无链接」的子目录。数据库、凭据、已有 junction 的
   binaries、插件本体一律留在 C:。
3. 复制用 robocopy /XJ（排除重解析点，避免把已有 junction 实体化）。
4. 建链接优先用 ctypes 直接写 IO_REPARSE_TAG_MOUNT_POINT（纯 Python、
   免管理员、无 cmd 依赖），失败自动回退 mklink /J。
5. 每一步都校验；单项失败跳过并继续其余目录，不产生不一致链接。
6. 删除 .bak 备份需逐项安全校验 + 显式确认，默认走系统回收站（可逆）。

兼容 Python 3.7+ / Windows NTFS。运行：python workbuddy_migrator.py
"""

import os
import sys
import csv
import json
import stat
import struct
import queue
import shutil
import ctypes
import time
import threading
import subprocess
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from ctypes import wintypes
from datetime import datetime

# ----------------------------------------------------------------------------
# 可改配置
# ----------------------------------------------------------------------------

APP_TITLE = "WorkBuddy 数据迁移工具"
APP_SUBTITLE = "把数据挪到别的盘，路径保持不变"

DEFAULT_SOURCE = os.path.join(os.path.expanduser("~"), ".workbuddy")
DEFAULT_TARGET_ROOT = r"E:\WorkBuddyData"

# (源目录下的相对路径, 中文说明, 分组)
MIGRATE_ITEMS = [
    ("workspace\\sessions",      "会话快照(活跃写入)", "会话数据"),
    ("traces",                   "调用追踪日志",   "日志追踪"),
    ("logs",                     "运行日志",       "日志追踪"),
    ("projects",                 "项目数据",       "项目数据"),
    ("file-history",             "文件编辑历史",   "缓存索引"),
    ("blobs",                    "二进制对象",     "缓存索引"),
    ("file-tree-manifests",      "文件树索引",     "缓存索引"),
    ("cache",                    "通用缓存",       "缓存索引"),
]

# 应用运行期间会持续写入、必然被进程占用的目录：默认不勾选。
# Windows 不允许 rename 一个其内部文件仍被打开的目录（WinError 5）。
ACTIVE_ITEMS = {r"workspace\sessions"}

# 删除备份时的每批上限（小批量、逐批校验）
BACKUP_BATCH = 5

# 明确不迁移的内容（用于界面说明）
KEEP_ITEMS = [
    ("顶层所有文件", "workbuddy.db / -wal / -shm、keyblob、settings.json、mcp.json、device-id"),
    ("binaries",     "已有 2 个指向安装目录的 junction，且为高频运行时"),
    ("plugins",      "已安装插件本体，非缓存"),
    ("app",          "Electron 运行时缓存"),
    ("memory / security", "项目记忆与安全数据（体积很小）"),
]

# ----------------------------------------------------------------------------
# 配色（浅色主题）
# ----------------------------------------------------------------------------

BG        = "#F4F6F9"
CARD      = "#FFFFFF"
TEXT      = "#1F2933"
MUTED     = "#6B7785"
BORDER    = "#DDE3EA"
ACCENT    = "#185FA5"
ACCENT_LT = "#E6F1FB"
OK        = "#0F6E56"
OK_LT     = "#E1F5EE"
WARN      = "#BA7517"
WARN_LT   = "#FAEEDA"
ERR       = "#A32D2D"
ERR_LT    = "#FCEBEB"
IDLE      = "#8A94A0"
IDLE_LT   = "#F1EFE8"

FONT_FAMILY = "Microsoft YaHei UI"
FONT = (FONT_FAMILY, 10)
FONT_SM = (FONT_FAMILY, 9)
FONT_MONO = ("Consolas", 9)

# ----------------------------------------------------------------------------
# Win32 / NTFS 底层
# ----------------------------------------------------------------------------

IS_WINDOWS = (os.name == "nt")
CREATE_NO_WINDOW = 0x08000000

FILE_ATTRIBUTE_REPARSE_POINT   = 0x00000400
INVALID_FILE_ATTRIBUTES        = 0xFFFFFFFF
IO_REPARSE_TAG_MOUNT_POINT     = 0xA0000003
FSCTL_SET_REPARSE_POINT        = 0x000900A4
FSCTL_GET_REPARSE_POINT        = 0x000900A8
MAX_REPARSE_BUFFER             = 16 * 1024

FILE_FLAG_BACKUP_SEMANTICS     = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT   = 0x00200000
GENERIC_READ                   = 0x80000000
GENERIC_WRITE                  = 0x40000000
FILE_SHARE_ALL                 = 0x00000007
OPEN_EXISTING                  = 3
INVALID_HANDLE_VALUE           = ctypes.c_void_p(-1).value

if IS_WINDOWS:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.GetFileAttributesW.restype = wintypes.DWORD
    _k32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _k32.CreateDirectoryW.restype = wintypes.BOOL
    _k32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    _k32.RemoveDirectoryW.restype = wintypes.BOOL
    _k32.RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.c_void_p]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
else:
    _k32 = None


def is_reparse_point(path):
    """判断路径是否为 NTFS 重解析点（junction / symlink / mount point）。"""
    if not IS_WINDOWS:
        return os.path.islink(path)
    attrs = _k32.GetFileAttributesW(str(path))
    if attrs == INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def path_exists_any(path):
    """对已损坏的链接，os.path.exists 会误报 False，用属性判断更可靠。"""
    if IS_WINDOWS:
        return _k32.GetFileAttributesW(str(path)) != INVALID_FILE_ATTRIBUTES
    return os.path.lexists(path)


def _open_dir_handle(path, access):
    h = _k32.CreateFileW(str(path), access, FILE_SHARE_ALL, None, OPEN_EXISTING,
                         FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _mount_point_buffer(target):
    """构造 REPARSE_DATA_BUFFER：SubstituteName = \\??\\E:\\..., PrintName = E:\\..."""
    target = os.path.abspath(target)
    sub = ("\\??\\" + target).encode("utf-16-le")
    prt = target.encode("utf-16-le")
    pathbuf = sub + b"\x00\x00" + prt + b"\x00\x00"
    # Tag / DataLength / Reserved / 4 个 WORD 偏移与长度
    header = struct.pack("<IHHHHHH",
                         IO_REPARSE_TAG_MOUNT_POINT,
                         len(pathbuf) + 8,
                         0,
                         0,
                         len(sub),
                         len(sub) + 2,
                         len(prt))
    return header + pathbuf


def create_junction_native(link, target):
    """纯 ctypes 创建 junction，免管理员、不依赖 cmd。"""
    if not IS_WINDOWS:
        raise RuntimeError("目录联接仅支持 Windows")
    link = os.path.abspath(link)
    target = os.path.abspath(target)
    if path_exists_any(link):
        raise RuntimeError("链接路径已存在: " + link)
    if not os.path.isdir(target):
        raise RuntimeError("目标目录不存在: " + target)
    if not _k32.CreateDirectoryW(link, None):
        err = ctypes.get_last_error()
        if err != 183:  # ERROR_ALREADY_EXISTS
            raise ctypes.WinError(err)
    raw = _mount_point_buffer(target)
    inbuf = ctypes.create_string_buffer(raw)
    h = _open_dir_handle(link, GENERIC_WRITE)
    try:
        returned = wintypes.DWORD(0)
        ok = _k32.DeviceIoControl(h, FSCTL_SET_REPARSE_POINT,
                                  ctypes.cast(inbuf, ctypes.c_void_p), len(raw),
                                  None, 0, ctypes.byref(returned), None)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        # 建链接失败，清掉刚创建的空目录，避免留下脏路径
        try:
            _k32.RemoveDirectoryW(link)
        except Exception:
            pass
        raise
    finally:
        _k32.CloseHandle(h)


def create_junction_mklink(link, target):
    """回退方案：调用系统内置 mklink /J（同样免管理员）。"""
    cmd = 'mklink /J "%s" "%s"' % (os.path.abspath(link), os.path.abspath(target))
    r = subprocess.run(["cmd", "/c", cmd], stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, errors="ignore",
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0 or not is_reparse_point(link):
        raise RuntimeError("mklink 失败: " + (r.stdout or "").strip())


def create_junction(link, target):
    """优先 native，失败回退 mklink；返回实际使用的方式。"""
    try:
        create_junction_native(link, target)
        return "native(FSCTL_SET_REPARSE_POINT)"
    except Exception as e:
        native_err = str(e)
        try:
            create_junction_mklink(link, target)
            return "mklink /J (native 回退: %s)" % native_err
        except Exception as e2:
            raise RuntimeError("两种建链方式均失败: %s | %s" % (native_err, e2))


def read_junction_target(link):
    """读回 junction 的 SubstituteName，用于界面「验证链接」。"""
    if not IS_WINDOWS or not is_reparse_point(link):
        return None
    out = ctypes.create_string_buffer(MAX_REPARSE_BUFFER)
    returned = wintypes.DWORD(0)
    h = _open_dir_handle(link, GENERIC_READ)
    try:
        ok = _k32.DeviceIoControl(h, FSCTL_GET_REPARSE_POINT, None, 0,
                                  ctypes.cast(out, ctypes.c_void_p), len(out),
                                  ctypes.byref(returned), None)
        if not ok:
            return None
    finally:
        _k32.CloseHandle(h)
    raw = out.raw
    tag, datalen, _res, suboff, sublen, _poff, _plen = struct.unpack_from("<IHHHHHH", raw, 0)
    if tag != IO_REPARSE_TAG_MOUNT_POINT:
        return None
    sub = raw[16 + suboff: 16 + suboff + sublen].decode("utf-16-le", "ignore")
    return sub[4:] if sub.startswith("\\??\\") else sub


def remove_junction(link):
    """
    只删除链接本身，不碰目标数据。
    注意：RemoveDirectoryW 对 mount point 删除的是链接；绝不能用
    shutil.rmtree / rm -rf / Remove-Item -Recurse，那些会跟随链接删掉目标盘内容。
    """
    if not is_reparse_point(link):
        raise RuntimeError("不是目录联接，拒绝删除: " + link)
    if not _k32.RemoveDirectoryW(str(link)):
        raise ctypes.WinError(ctypes.get_last_error())


# ----------------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------------

def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def scan_dir(path):
    """统计目录体积与文件数；跳过重解析点，避免跟随 junction 重复计数。"""
    total, count = 0, 0
    if not os.path.isdir(path):
        return 0, 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not is_reparse_point(os.path.join(root, d))]
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def disk_usage(path):
    try:
        if IS_WINDOWS:
            drive, _rest = os.path.splitdrive(os.path.abspath(path))
            probe = (drive + os.sep) if drive else os.path.abspath(path)
        else:
            probe = path
        u = shutil.disk_usage(probe)
        return u.total, u.used, u.free
    except Exception:
        return 0, 0, 0


def volume_fs(path):
    """返回卷文件系统名（NTFS / exFAT ...）；查询失败返回空串。

    junction 的目标必须是本地 NTFS 卷，这项在预演阶段就检查，
    避免复制到一半才发现目标盘不支持重解析点。
    """
    if not IS_WINDOWS:
        return ""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return ""
    try:
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_len = wintypes.DWORD()
        flags = wintypes.DWORD()
        ok = _k32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive + os.sep), label, 261,
            ctypes.byref(serial), ctypes.byref(max_len),
            ctypes.byref(flags), fs, 261)
        return fs.value.upper() if ok else ""
    except Exception:
        return ""


def norm_for_compare(path):
    """仅用于路径关系判断的归一化：绝对路径 + 去尾斜杠 + Windows 大小写折叠。"""
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def volume_of(path):
    """返回卷标识，例如 C: 或 \\\\server\\share。"""
    return os.path.normcase(os.path.splitdrive(norm_for_compare(path))[0])


def paths_nested(a, b):
    """判断 a、b 是否相同或一方嵌套在另一方（用于阻止递归复制）。

    关键：绝不能直接对 Windows 上不同盘符的两个路径调用 os.path.commonpath，
    它会抛 ValueError("Paths don't have the same drive")。而 C: -> E: 跨卷
    恰恰是本工具的标准场景。跨卷路径天然不可能嵌套，先比卷即可安全短路；
    只有同卷才做 commonpath，并对任何 ValueError 做保守兜底。
    """
    na, nb = norm_for_compare(a), norm_for_compare(b)
    if volume_of(na) != volume_of(nb):
        return False
    if na == nb:
        return True
    try:
        common = os.path.commonpath([na, nb])
    except ValueError:
        # 混合 UNC / 本地、绝对 / 相对等边角情况：保守视为不嵌套
        return False
    return os.path.normcase(common) in (na, nb)


# 可能持有 .workbuddy 目录文件句柄的进程（不含 python/pythonw，
# 避免把迁移工具自己算进去）
APP_PROCESS_KEYWORDS = ("workbuddy", "node", "electron", "portablegit",
                        "git", "bash", "code")


def running_app_processes():
    """粗略检测可能占用目录的进程，用于迁移前告警。"""
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"],
                                      text=True, errors="ignore",
                                      creationflags=CREATE_NO_WINDOW)
        found = set()
        for line in out.splitlines():
            name = line.split('","')[0].strip('"')
            if any(k in name.lower() for k in APP_PROCESS_KEYWORDS):
                found.add(name)
        return sorted(found)
    except Exception:
        return []


def probe_rename(path, attempts=3, delay=0.4):
    """瞬时改名再改回，提前检测目录是否被占用。

    Windows 不允许 rename 一个内部文件仍被打开的目录（WinError 5）。
    第一次改名失败时数据完全不动；成功后必定立刻还原。
    返回 (ok, detail)。
    """
    if not os.path.isdir(path):
        return True, "目录不存在（跳过占用探测）"
    tmp = path + ".__wb_probe__"
    if path_exists_any(tmp):
        return False, "探测临时名已存在: " + tmp
    last, renamed = "", False
    for n in range(attempts):
        try:
            os.rename(path, tmp)
            renamed = True
            break
        except OSError as e:
            last = "WinError %s %s" % (getattr(e, "winerror", e.errno), e.strerror)
        except Exception as e:
            last = str(e)
        if n < attempts - 1:
            time.sleep(delay)
    if not renamed:
        return False, last
    try:
        os.rename(tmp, path)
    except Exception as e:
        return False, "探测后还原失败！目录当前名为 %s：%s" % (tmp, e)
    return True, "可正常改名"


def rename_with_retry(src, dst, attempts=5, delay=0.6):
    """带退避的重命名，应对瞬时文件占用；返回实际尝试次数。"""
    last = None
    for n in range(attempts):
        try:
            os.rename(src, dst)
            return n + 1
        except OSError as e:
            last = e
            if n < attempts - 1:
                time.sleep(delay)
    raise last


def dir_has_reparse(root):
    """返回目录树内第一个重解析点路径；没有则 None。全程不跟随 junction。"""
    for cur, dirs, files in os.walk(root):
        for d in list(dirs):
            p = os.path.join(cur, d)
            if is_reparse_point(p):
                return p
        dirs[:] = [d for d in dirs if not is_reparse_point(os.path.join(cur, d))]
        for f in files:
            p = os.path.join(cur, f)
            if is_reparse_point(p):
                return p
    return None


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _shell_delete(op):
    """隔离 Shell API 调用（便于注入测试），返回 API 返回码。"""
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))


FO_DELETE          = 3
FOF_SILENT         = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO      = 0x0040
FOF_NOERRORUI      = 0x0400


def delete_path(path, permanent=False):
    """用 Shell API 删除目录：默认进回收站（可逆），permanent=True 才不可逆。

    调用方必须先确认目录内没有重解析点，避免 Shell 跟随链接误删目标盘数据。
    """
    if not IS_WINDOWS:
        raise RuntimeError("仅支持 Windows")
    p = os.path.abspath(path)
    buf = ctypes.create_unicode_buffer(p + "\0")   # SHFileOperation 要求双 NUL 结尾
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, wintypes.LPCWSTR)
    op.pTo = None
    flags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_NOERRORUI
    if not permanent:
        flags |= FOF_ALLOWUNDO
    op.fFlags = flags
    op.fAnyOperationsAborted = False
    op.hNameMappings = None
    op.lpszProgressTitle = None
    rc = _shell_delete(op)
    if rc != 0 or op.fAnyOperationsAborted:
        raise RuntimeError("Shell 删除失败 rc=%s aborted=%s" %
                           (rc, bool(op.fAnyOperationsAborted)))
    return "回收站" if not permanent else "永久删除"


def inspect_backups(source_root, target_root, rels):
    """只读扫描 .bak 备份并逐项做删除前安全校验。

    只有「原路径是有效 junction + 链接指向预期目标 + 目标盘副本存在 +
    备份内无重解析点」全部满足，才允许删除备份，避免误删唯一副本。
    """
    out = []
    for rel in rels:
        src = os.path.join(source_root, rel)
        bak = src + ".bak"
        if not path_exists_any(bak):
            continue
        dst = os.path.join(target_root, rel)
        e = {"rel": rel, "src": src, "bak": bak, "dst": dst, "target": "",
             "size": 0, "files": 0, "ok": False, "why": ""}
        if is_reparse_point(bak):
            e["why"] = "备份本身是重解析点，拒绝处理"
            out.append(e)
            continue
        if not os.path.isdir(bak):
            e["why"] = "备份不是普通目录，拒绝处理"
            out.append(e)
            continue
        if not is_reparse_point(src):
            e["why"] = "原路径不是目录联接（迁移未成功或已回滚），删备份会丢失唯一副本"
            out.append(e)
            continue
        tgt = read_junction_target(src) or ""
        e["target"] = tgt
        if os.path.normcase(os.path.abspath(tgt)) != os.path.normcase(os.path.abspath(dst)):
            e["why"] = "链接目标与预期不符，拒绝删除"
            out.append(e)
            continue
        if not os.path.isdir(dst):
            e["why"] = "目标盘副本不存在，拒绝删除"
            out.append(e)
            continue
        rp = dir_has_reparse(bak)
        if rp:
            e["why"] = "备份内含重解析点，删除可能跟随链接：" + rp
            out.append(e)
            continue
        e["size"], e["files"] = scan_dir(bak)
        e["ok"] = True
        out.append(e)
    return out


def now_stamp():
    return datetime.now().strftime("%H:%M:%S")


# ----------------------------------------------------------------------------
# 迁移引擎（后台线程，只通过队列向 UI 发消息）
# ----------------------------------------------------------------------------

class MigrationEngine(object):
    def __init__(self, source, target_root, items, emit):
        self.source = source
        self.target_root = target_root
        self.items = items          # [(rel, desc, group, size, files), ...]
        self.emit = emit
        self.cancel = threading.Event()

    def log(self, level, text):
        self.emit(("log", level, "[%s] %s" % (now_stamp(), text)))

    def _target_of(self, rel):
        return os.path.join(self.target_root, rel)

    def _check_paths(self):
        """只读前置检查。返回 (problems, need_bytes, free_bytes)。

        这里刻意不使用 commonpath 直接比较源/目标：跨盘符是正常场景，
        必须先在预演阶段就验证通过，不能等到点「开始迁移」才报错。
        """
        problems = []
        need = 0
        for item in self.items:
            rel = item[0]
            src = os.path.join(self.source, rel)
            dst = self._target_of(rel)
            if paths_nested(src, dst):
                problems.append("%s：源与目标相同或互相嵌套 -> %s" % (rel, dst))
            need += max(0, item[3] or 0)

        _total, _used, free = disk_usage(self.target_root)
        if free and need > free:
            problems.append("目标盘可用空间不足：需 %s，可用 %s" %
                            (human_size(need), human_size(free)))

        fs = volume_fs(self.target_root)
        if fs and "NTFS" not in fs:
            problems.append("目标卷文件系统为 %s，目录联接 (junction) 需要 NTFS" % fs)
        elif fs:
            self.log("OK", "目标卷文件系统 %s，支持目录联接" % fs)

        src_vols = sorted(set(volume_of(os.path.join(self.source, it[0]))
                              for it in self.items))
        dst_vols = sorted(set(volume_of(self._target_of(it[0]))
                              for it in self.items))
        if src_vols == dst_vols:
            self.log("WARN", "源与目标位于同一卷 %s：junction 可用，但不会释放该盘空间"
                     % (", ".join(src_vols) or "?"))
        else:
            self.log("OK", "跨卷迁移 %s -> %s（junction 原生支持，属正常场景）"
                     % (", ".join(src_vols), ", ".join(dst_vols)))
        return problems, need, free

    def preflight(self):
        """迁移前的统一检查；任一问题都直接中止，避免复制到一半才失败。"""
        problems, need, free = self._check_paths()
        for p in problems:
            self.log("ERR", "前置检查: " + p)
        if problems:
            raise RuntimeError("前置检查未通过（%d 项），详见执行日志" % len(problems))
        self.log("OK", "前置检查通过：预计写入 %s，目标盘可用 %s" %
                 (human_size(need), human_size(free)))

    def dry_run(self):
        self.log("INFO", "预演开始：只读取、不写入任何文件")
        try:
            report = []
            for i, item in enumerate(self.items):
                rel, desc, group, _s, _f = item
                src = os.path.join(self.source, rel)
                dst = self._target_of(rel)
                size, files = scan_dir(src)
                rp = is_reparse_point(src)
                exists_target = os.path.isdir(dst)
                report.append({
                    "rel": rel, "desc": desc, "group": group,
                    "src": src, "dst": dst,
                    "size": size, "files": files,
                    "is_junction": rp, "target_exists": exists_target,
                })
                self.emit(("scan_item", i, size, files))
                self.log("OK" if not rp else "WARN",
                         "%-26s %8s / %6d 文件 -> %s%s" %
                         (rel, human_size(size), files, dst,
                          "  [已是链接，将跳过]" if rp else ""))
                self.emit(("progress", (i + 1) * 100.0 / max(1, len(self.items)),
                           "预演 %d/%d" % (i + 1, len(self.items))))
            total = sum(r["size"] for r in report)

            # 预演阶段就做路径/空间检查，问题提前暴露，不必等到真正迁移
            problems, need, free = self._check_paths()
            for p in problems:
                self.log("WARN", "预演提示: " + p)
            if not problems:
                self.log("OK", "路径检查通过：预计写入 %s，目标盘可用 %s" %
                         (human_size(need), human_size(free)))

            self.emit(("dry_done", report, total))
            self.log("OK", "预演完成：共 %d 项，合计 %s" %
                     (len(report), human_size(total)))
        except Exception as e:
            self.log("ERR", "预演失败: %s" % e)
            self.log("ERR", traceback.format_exc().strip().splitlines()[-1])
            self.emit(("dry_failed", str(e)))
        finally:
            self.emit(("busy", False))

    def run(self):
        self.log("INFO", "迁移开始。源数据只改名保留为 .bak，不会删除")
        done, failed = [], []
        try:
            self.preflight()
            for i, item in enumerate(self.items):
                if self.cancel.is_set():
                    self.log("WARN", "已收到停止请求，中止后续项目")
                    break
                rel, desc, group, _s, _f = item
                src = os.path.join(self.source, rel)
                dst = self._target_of(rel)
                self.emit(("item_state", i, "进行中", ""))
                self.emit(("progress", i * 100.0 / max(1, len(self.items)),
                           "正在处理 %s" % rel))
                try:
                    if is_reparse_point(src):
                        self.emit(("item_state", i, "跳过", "源已是目录联接"))
                        self.log("WARN", "%s 已是 junction，跳过" % rel)
                        continue
                    if not os.path.isdir(src):
                        self.emit(("item_state", i, "跳过", "源目录不存在"))
                        self.log("WARN", "%s 不存在，跳过" % rel)
                        continue

                    # 跨卷路径不能用 os.path.commonpath 比较（会抛
                    # ValueError: Paths don't have the same drive），改用卷感知判断
                    if paths_nested(src, dst):
                        raise RuntimeError("目标目录与源目录相同或互相嵌套: " + dst)

                    # 0) 占用预检：在复制几 GB 之前先确认目录能改名。
                    #    Windows 不允许 rename 内部文件仍被打开的目录（WinError 5）。
                    self.log("INFO", "[%s] 占用探测（瞬时改名后立即还原）" % rel)
                    ok, detail = probe_rename(src)
                    if not ok:
                        raise RuntimeError(
                            "目录正被占用，无法改名（%s）。通常是 WorkBuddy 或后台 "
                            "node / git 进程仍在运行；应用运行中 workspace\\sessions "
                            "会持续写入，建议取消勾选该项，先迁移其它目录。" % detail)
                    self.log("OK", "[%s] 目录可改名" % rel)

                    if not os.path.isdir(dst):
                        os.makedirs(dst)

                    # 1) 复制
                    self.log("INFO", "[%s] 复制中 -> %s" % (rel, dst))
                    cmd = ["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT",
                           "/XJ", "/R:1", "/W:1", "/NFL", "/NDL", "/NP"]
                    r = subprocess.run(cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True,
                                       errors="ignore", creationflags=CREATE_NO_WINDOW)
                    if r.returncode >= 8:
                        raise RuntimeError("robocopy 失败(退出码 %d): %s" %
                                           (r.returncode, (r.stdout or "")[-400:]))
                    self.log("OK", "[%s] 复制完成 (robocopy rc=%d)" % (rel, r.returncode))

                    # 2) 校验
                    s_size, s_files = scan_dir(src)
                    d_size, d_files = scan_dir(dst)
                    if s_size != d_size or s_files != d_files:
                        raise RuntimeError("校验不一致: 源 %s/%d，目标 %s/%d" %
                                           (human_size(s_size), s_files,
                                            human_size(d_size), d_files))
                    self.log("OK", "[%s] 校验通过 %s / %d 文件" %
                             (rel, human_size(s_size), s_files))

                    # 3) 源改名保留（带重试，应对探测之后新出现的瞬时占用）
                    bak = src + ".bak"
                    if path_exists_any(bak):
                        raise RuntimeError("备份目录已存在，为避免覆盖停止: " + bak)
                    tries = rename_with_retry(src, bak)
                    self.log("OK", "[%s] 源已保留为 %s%s" %
                             (rel, os.path.basename(bak),
                              "（重试 %d 次）" % (tries - 1) if tries > 1 else ""))

                    # 4) 建链接
                    try:
                        how = create_junction(src, dst)
                        self.log("OK", "[%s] 已建立 junction (%s)" % (rel, how))
                    except Exception:
                        try:
                            rename_with_retry(bak, src)
                            self.log("ERR", "[%s] 建链接失败，源目录已自动还原" % rel)
                        except Exception as re:
                            self.log("ERR", "[%s] 建链接失败且还原失败！请手动把 %s 改回 %s：%s" %
                                     (rel, bak, src, re))
                        raise

                    # 5) 写入探针验证落盘位置
                    probe = os.path.join(src, "_migrate_probe.tmp")
                    try:
                        with open(probe, "w") as fp:
                            fp.write("probe")
                        landed = os.path.isfile(os.path.join(dst, "_migrate_probe.tmp"))
                        os.remove(os.path.join(dst, "_migrate_probe.tmp")) if landed else None
                        if not landed:
                            raise RuntimeError("探针未落在目标盘，链接可能无效")
                    except Exception:
                        try:
                            os.remove(probe)
                        except OSError:
                            pass
                        raise
                    self.log("OK", "[%s] 写入探针验证通过：新数据落到 %s 侧" %
                             (rel, volume_of(dst) or "目标盘"))

                    self.emit(("item_state", i, "已迁移", human_size(s_size)))
                    done.append({"rel": rel, "desc": desc, "src": src, "dst": dst,
                                 "size": s_size, "files": s_files, "bak": bak})
                except Exception as e:
                    # 单项失败不再中断整批：其它目录继续迁移
                    failed.append({"rel": rel, "src": src, "dst": dst,
                                   "error": "%s: %s" % (type(e).__name__, e)})
                    self.emit(("item_state", i, "失败", str(e)[:56]))
                    self.log("ERR", "[%s] 失败：%s" % (rel, e))
                    self.log("WARN", "[%s] 已跳过并继续其它目录；源目录保持原样" % rel)
                    self.log("WARN", "目标盘若已有副本，是可安全保留的冗余数据，"
                             "重跑时 robocopy 会增量同步")
                    procs = running_app_processes()
                    if procs:
                        self.log("WARN", "可能占用目录的进程：%s" % ", ".join(procs[:8]))
                finally:
                    self.emit(("progress", (i + 1) * 100.0 / max(1, len(self.items)),
                               "已处理 %d/%d" % (i + 1, len(self.items))))

            self.emit(("done", done, failed))
            self.log("OK" if not failed else "WARN",
                     "迁移结束：成功 %d 项 / 失败 %d 项，成功合计 %s" %
                     (len(done), len(failed),
                      human_size(sum(d["size"] for d in done))))
        except Exception as e:
            self.log("ERR", "迁移中断: %s" % e)
            self.log("ERR", traceback.format_exc().strip().splitlines()[-1])
            self.emit(("failed", str(e), done))
        finally:
            self.emit(("busy", False))

    @staticmethod
    def _makedirs(path):
        if not os.path.isdir(path):
            os.makedirs(path)

    def verify(self):
        self.log("INFO", "验证现有链接")
        for i, item in enumerate(self.items):
            rel = item[0]
            src = os.path.join(self.source, rel)
            if is_reparse_point(src):
                tgt = read_junction_target(src)
                self.emit(("item_state", i, "已联接", (tgt or "目标未知")))
                self.log("OK", "%-26s -> %s" % (rel, tgt or "读取失败"))
            else:
                self.emit(("item_state", i, "未迁移", ""))
        self.emit(("busy", False))

    def rollback(self):
        """删除 junction，把 .bak 改回原名；目标盘副本保留不动。

        若 .bak 已被「删除备份」清理，必须拒绝回滚并保留链接，
        否则删掉链接后原路径会凭空消失（数据虽在目标盘，但应用找不到路径）。
        """
        self.log("WARN", "开始回滚（只恢复源路径，%s 侧数据保留）" %
                 (volume_of(self.target_root) or "目标盘"))
        n, blocked = 0, 0
        for i, item in enumerate(self.items):
            rel = item[0]
            src = os.path.join(self.source, rel)
            bak = src + ".bak"
            if not is_reparse_point(src):
                continue
            if (not path_exists_any(bak) or not os.path.isdir(bak)
                    or is_reparse_point(bak)):
                self.emit(("item_state", i, "无法回滚", "备份已删除"))
                self.log("ERR", "%s 的 .bak 已不存在：为保护数据保留链接，跳过回滚" % rel)
                blocked += 1
                continue
            remove_junction(src)
            os.rename(bak, src)
            self.emit(("item_state", i, "已回滚", ""))
            self.log("OK", "%s 已回滚" % rel)
            n += 1
        self.log("OK" if not blocked else "WARN",
                 "回滚完成：成功 %d 项，因备份已删除而保留链接 %d 项" %
                 (n, blocked))
        self.emit(("busy", False))
        self.emit(("rolled_back", n))


# ----------------------------------------------------------------------------
# 图形界面
# ----------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title(APP_TITLE)
        self.geometry("1020x720")
        self.minsize(900, 620)
        self.configure(bg=BG)

        self.source_var = tk.StringVar(value=DEFAULT_SOURCE)
        self.target_var = tk.StringVar(value=DEFAULT_TARGET_ROOT)
        self.confirm_var = tk.IntVar(value=0)
        self.status_var = tk.StringVar(value="就绪 · 待扫描")
        self.summary_var = tk.StringVar(value="尚未扫描")
        self.busy = False
        self.dry_run_done = False
        self.dry_report = []
        self.rows = []            # 与 MIGRATE_ITEMS 对齐的显示数据
        self.q = queue.Queue()

        self._init_style()
        self._build_ui()
        self.bind("<F5>", lambda e: self._scan())
        self._update_go_state()              # 初始禁用「开始迁移」
        self.after(80, self._poll)
        self._log("INFO", "工具已启动。仅做只读扫描，不会自动迁移任何文件。")
        self._log("INFO", "按 F5 或点「扫描 / 刷新」开始只读扫描。")
        if not IS_WINDOWS:
            self._log("ERR", "当前不是 Windows，目录联接功能不可用。")
        self._refresh_disks()
        self.after(180, self._refresh_disks)  # Configure 后按真实画布宽度重画

    # ---------------- 样式 ----------------
    def _init_style(self):
        self.option_add("*Font", FONT)
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=27, borderwidth=0)
        st.configure("Treeview.Heading", background="#EAEFF5", foreground=TEXT,
                     font=(FONT_FAMILY, 9, "bold"), borderwidth=0)
        st.map("Treeview", background=[("selected", "#D6E8FA")],
               foreground=[("selected", TEXT)])
        st.configure("TProgressbar", troughcolor="#E7ECF2", background=ACCENT,
                     borderwidth=0, thickness=16)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(16, 7), background="#E9EDF3",
                     foreground=MUTED)
        st.map("TNotebook.Tab", background=[("selected", CARD)],
               foreground=[("selected", ACCENT)])
        st.configure("TFrame", background=BG)
        st.configure("TLabelframe", background=BG, bordercolor=BORDER)
        st.configure("TLabelframe.Label", background=BG, foreground=MUTED)

    # ---------------- 布局 ----------------
    def _build_ui(self):
        # 顶部标题
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(head, text=APP_TITLE, bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 16, "bold")).pack(side="left")
        tk.Label(head, text="  " + APP_SUBTITLE, bg=BG, fg=MUTED,
                 font=FONT_SM).pack(side="left", pady=(6, 0))
        self.badge = tk.Label(head, text=" 未连接 ", bg=IDLE_LT, fg=IDLE,
                              font=(FONT_FAMILY, 9, "bold"), padx=10, pady=3)
        self.badge.pack(side="right")

        # 路径区
        pathbox = tk.LabelFrame(self, text=" 路径设置 ", bg=BG, fg=MUTED,
                                font=FONT_SM, padx=12, pady=8)
        pathbox.pack(fill="x", padx=18, pady=(0, 8))
        tk.Label(pathbox, text="源目录", bg=BG, fg=TEXT, width=9,
                 anchor="w").grid(row=0, column=0, sticky="w", pady=3)
        tk.Entry(pathbox, textvariable=self.source_var, state="readonly",
                 readonlybackground="#F7F9FC", relief="solid", bd=1,
                 font=FONT_MONO).grid(row=0, column=1, sticky="ew", ipady=4)
        tk.Label(pathbox, text="目标根目录", bg=BG, fg=TEXT, width=9,
                 anchor="w").grid(row=1, column=0, sticky="w", pady=3)
        tk.Entry(pathbox, textvariable=self.target_var, relief="solid", bd=1,
                 font=FONT_MONO).grid(row=1, column=1, sticky="ew", ipady=4)
        self._btn(pathbox, "浏览", self._choose_target, IDLE_LT, MUTED).grid(
            row=1, column=2, padx=(8, 0))
        pathbox.columnconfigure(1, weight=1)

        # 磁盘卡片
        disks = tk.Frame(self, bg=BG)
        disks.pack(fill="x", padx=18, pady=(0, 8))
        self.disk_canvas = tk.Canvas(disks, bg=CARD, height=100, highlightthickness=1,
                                     highlightbackground=BORDER)
        self.disk_canvas.pack(side="left", fill="x", expand=True)
        self.disk_canvas.bind("<Configure>", lambda e: self._refresh_disks())
        note = tk.Frame(disks, bg=WARN_LT, width=270, height=100)
        note.pack(side="right", padx=(10, 0), fill="y")
        note.pack_propagate(False)
        tk.Label(note, text="迁移只改名、不删除", bg=WARN_LT, fg=WARN,
                 font=(FONT_FAMILY, 10, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
        tk.Label(note, text="源目录保留为 .bak，随时可回滚", bg=WARN_LT,
                 fg=MUTED, font=FONT_SM, wraplength=240,
                 justify="left").pack(anchor="w", padx=12)

        # 标签页
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self.tab_list = tk.Frame(self.nb, bg=BG)
        self.tab_map = tk.Frame(self.nb, bg=BG)
        self.tab_log = tk.Frame(self.nb, bg=BG)
        self.tab_help = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_list, text=" 迁移清单 ")
        self.nb.add(self.tab_map, text=" 映射视图 ")
        self.nb.add(self.tab_log, text=" 执行日志 ")
        self.nb.add(self.tab_help, text=" 使用说明 ")
        self._build_tab_list()
        self._build_tab_map()
        self._build_tab_log()
        self._build_tab_help()

        # 底部进度 + 按钮
        # 关键：底部固定区必须在 pack 顺序上早于 expand 的 Notebook，
        # 否则 Notebook 会吃掉全部剩余空间，这一整块都不会被映射（按钮 1x1 不可见）。
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill="x", side="bottom", padx=18, pady=(2, 6), before=self.nb)
        pr = tk.Frame(bottom, bg=BG)
        pr.pack(fill="x")
        self.progress_label = tk.Label(pr, text="等待操作", bg=BG, fg=MUTED,
                                       font=FONT_SM, anchor="w")
        self.progress_label.pack(side="left")
        self.progress = ttk.Progressbar(pr, mode="determinate", maximum=100)
        self.progress.pack(side="right", fill="x", expand=True, padx=(10, 0))

        # 安全确认独占一行，不与按钮争夺水平空间
        safety = tk.Frame(bottom, bg=BG)
        safety.pack(fill="x", pady=(8, 2))
        self.chk = tk.Checkbutton(safety, text="我已完全退出 WorkBuddy 及后台进程",
                                  variable=self.confirm_var, bg=BG, fg=TEXT,
                                  activebackground=BG, font=FONT_SM,
                                  command=self._update_go_state)
        self.chk.pack(side="left")
        tk.Label(safety, text="未勾选时「开始迁移」保持禁用", bg=BG, fg=MUTED,
                 font=FONT_SM).pack(side="left", padx=12)

        # 按钮行用 grid：左侧操作组固定，中间弹簧，右侧主按钮永远靠右
        ctrl = tk.Frame(bottom, bg=BG)
        ctrl.pack(fill="x", pady=(4, 0))
        ops = [("扫描 / 刷新  F5", self._scan, ACCENT, "#FFFFFF"),
               ("预演 (Dry-run)", self._dry_run, ACCENT_LT, ACCENT),
               ("导出清单", self._export, IDLE_LT, TEXT),
               ("验证链接", self._verify, IDLE_LT, TEXT),
               ("回滚", self._rollback, WARN_LT, WARN),
               ("删除备份", self._delete_backups, ERR_LT, ERR)]
        for i, (text, cmd, bg, fg) in enumerate(ops):
            self._btn(ctrl, text, cmd, bg, fg).grid(row=0, column=i,
                                                    padx=(0, 6), sticky="w")
        ctrl.columnconfigure(len(ops), weight=1)
        self.btn_go = self._btn(ctrl, "开始迁移", self._start, OK, "#FFFFFF")
        self.btn_go.grid(row=0, column=len(ops) + 1, sticky="e", padx=(12, 0))

        # 状态栏
        bar = tk.Frame(self, bg="#EAEFF5", height=28)
        bar.pack(fill="x", side="bottom", before=self.nb)
        bar.pack_propagate(False)
        tk.Label(bar, textvariable=self.status_var, bg="#EAEFF5", fg=MUTED,
                 font=FONT_SM, anchor="w").pack(side="left", padx=14)
        tk.Label(bar, text="NTFS junction /J  |  robocopy /XJ  |  源数据不删除",
                 bg="#EAEFF5", fg=IDLE, font=FONT_SM).pack(side="right", padx=14)

    def _btn(self, parent, text, cmd, bg, fg):
        return tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                         activebackground=bg, activeforeground=fg,
                         relief="solid", bd=1, highlightthickness=0,
                         padx=14, pady=6,
                         font=(FONT_FAMILY, 9, "bold"), cursor="hand2",
                         disabledforeground=IDLE)

    # ---------------- Tab 1：清单 ----------------
    def _build_tab_list(self):
        frm = tk.Frame(self.tab_list, bg=BG)
        frm.pack(fill="both", expand=True, padx=6, pady=6)
        cols = ("sel", "name", "kind", "size", "files", "state", "target")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", selectmode="none")
        heads = [("sel", "选", 38, "center"), ("name", "源目录（相对）", 210, "w"),
                 ("kind", "内容", 110, "w"), ("size", "体积", 90, "e"),
                 ("files", "文件数", 78, "e"), ("state", "状态", 100, "center"),
                 ("target", "迁移后位置", 260, "w")]
        for key, label, width, anchor in heads:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("name", "target")))
        vs = ttk.Scrollbar(frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        for tag, color in (("pending", TEXT), ("running", ACCENT), ("ok", OK),
                           ("skip", MUTED), ("err", ERR), ("rollback", WARN)):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.tag_configure("ok", foreground=OK, background="#F3FBF7")

        bar = tk.Frame(self.tab_list, bg=BG)
        bar.pack(fill="x", padx=8, pady=(2, 8))
        tk.Label(bar, textvariable=self.summary_var, bg=BG, fg=ACCENT,
                 font=(FONT_FAMILY, 10, "bold"), anchor="w").pack(side="left")
        tk.Label(bar, text="点击第一列可勾选 / 取消", bg=BG, fg=MUTED,
                 font=FONT_SM).pack(side="right")

    def _build_tab_map(self):
        top = tk.Frame(self.tab_map, bg=BG)
        top.pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(top, text="C: 逻辑路径（保持不变）    →    NTFS junction    →    E: 实体数据（持续增长）",
                 bg=BG, fg=MUTED, font=FONT_SM).pack(anchor="w")
        self.map_canvas = tk.Canvas(self.tab_map, bg=CARD, highlightthickness=1,
                                    highlightbackground=BORDER)
        self.map_canvas.pack(fill="both", expand=True, padx=10, pady=8)
        self.map_canvas.bind("<Configure>", lambda e: self._draw_map())

    def _build_tab_log(self):
        frm = tk.Frame(self.tab_log, bg=BG)
        frm.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_text = scrolledtext.ScrolledText(frm, bg="#FBFCFE", fg=TEXT,
                                                  font=FONT_MONO, relief="solid", bd=1,
                                                  wrap="word")
        self.log_text.pack(fill="both", expand=True)
        for lvl, color in (("INFO", TEXT), ("OK", OK), ("WARN", WARN), ("ERR", ERR)):
            self.log_text.tag_configure(lvl, foreground=color)
        tk.Button(frm, text="清空日志", command=lambda: self.log_text.delete("1.0", "end"),
                  bg=IDLE_LT, fg=MUTED, relief="flat", bd=0, padx=12, pady=4,
                  font=FONT_SM).pack(anchor="e", pady=(6, 0))

    def _build_tab_help(self):
        frm = tk.Frame(self.tab_help, bg=BG)
        frm.pack(fill="both", expand=True, padx=10, pady=8)
        txt = scrolledtext.ScrolledText(frm, bg=CARD, fg=TEXT, font=FONT,
                                        relief="solid", bd=1, wrap="word")
        txt.pack(fill="both", expand=True)
        body = """
【这个工具做什么】

把 %USERPROFILE%\\.workbuddy 下体积较大的纯数据目录复制到目标盘，
然后在原位置建立 NTFS 目录联接 (junction /J)。应用继续按原路径读写，
字节实际落在目标盘；源盘侧只剩几百字节的重解析点，不会再增长。

【迁移后新数据写在哪里】

做了 junction 的目录 → 目标盘侧。
没做 junction 的目录（数据库、binaries、plugins、app 等）→ 仍在源盘。
应用日志、配置、数据库里记录的路径字符串始终不变，无需改任何配置。

【跨盘符说明】

C: -> E: 这类跨卷迁移是正常且推荐的场景，junction 原生支持。
路径关系判断已做卷感知处理：不同卷直接判定为「不可能嵌套」，
不会再因 Windows 的 "Paths don't have the same drive" 报错中断。
预演阶段会输出源卷 / 目标卷并校验目标盘剩余空间；若两者被设成
同一卷，会给出警告（链接仍可建立，但不会释放磁盘空间）。
目标盘必须是本地 NTFS 卷，不能是 UNC 网络路径。

【被占用的目录 / WinError 5】

Windows 不允许重命名内部文件仍被打开的目录，会报 WinError 5 拒绝访问。
workspace\\sessions 在应用运行期间会持续写入，必然被占用，因此默认不勾选；
只要当前 WorkBuddy 会话还在，这个目录就无法迁移，建议保持跳过。
每项迁移前会先做「瞬时改名再立刻改回」的占用探测，在复制几 GB 之前
就发现问题；探测通过后改名还会自动重试 5 次。单项失败不再中断整批，
其余目录照常迁移，报告里单独列出失败项及原因。

【明确不迁移的内容】

"""
        for name, why in KEEP_ITEMS:
            body += "  - %-16s %s\n" % (name, why)
        body += """
【操作步骤】

1. 完全退出 WorkBuddy（含托盘图标与后台 node / PortableGit 子进程）。
2. 点「扫描 / 刷新」→「预演 (Dry-run)」，核对清单与体积。
   workspace\\sessions 默认不勾选（应用运行中必然被占用），保持跳过即可。
3. 勾选「我已完全退出…」，点「开始迁移」。
4. 完成后点「验证链接」，确认每项指向目标盘目录。
5. 重启应用正常使用；稳定运行几天后，可自行决定是否处理 .bak。

【每一步做了什么】

  robocopy /E /COPY:DAT /DCOPY:DAT /XJ   复制并保留时间戳，排除重解析点
  校验 源 / 目标 的文件数与总字节数      不一致立即停止
  rename 源 → 源.bak                     保留原数据，绝不删除
  FSCTL_SET_REPARSE_POINT                写 mount point 重解析点（免管理员）
  写入探针文件                           验证新数据确实落到目标盘侧

【风险与注意】

* 删 junction 时绝对不要用 rm -rf / Remove-Item -Recurse / shutil.rmtree，
  它们可能跟随链接把目标盘真实数据一起删掉。本工具用 RemoveDirectoryW
  只删链接本身。
* 应用升级或修复安装若删除重建了子目录，链接会断，新数据会回流到源盘。
  升级后请点「验证链接」复查，必要时重建。
* 删除 junction 目录下的文件，回收站位于目标卷的 $RECYCLE.BIN。
* 目录联接只能指向本地 NTFS 卷，不能指向 UNC 网络路径。
* 用 du / Get-ChildItem 从源盘逻辑路径统计仍会显示体积「在源盘」，
  但源盘可用空间不会减少。判断实际占用请看卷剩余空间。
* workspace\\sessions 是高频写入目录，应用会自行创建 / 清理会话快照。
  实测几十秒内体积就会明显变化，预演数字随时过期。务必在完全退出应用后
  重新点「扫描 / 刷新」，以最后一次预演为准；若复制期间源发生变化，
  体积 / 文件数校验会失败并自动停止，不会静默产生不一致的副本。
* 回滚只恢复源盘侧路径，目标盘副本保留不动（由你自行决定如何处理）。

【异常处理】

单项失败会跳过并继续其它目录；只有整体前置检查失败才会中止，
并把已改名的源目录自动还原。
可用「回滚」把已建立的链接全部撤销，恢复迁移前的目录结构。

【删除备份（释放源盘空间）】

.bak 是源目录的原样副本，只有迁移成功并确认新数据正常后才该删除。
点「删除备份」会先只读扫描并逐项做安全校验：
  原路径必须是有效 junction、链接目标必须与预期一致、
  目标盘副本必须存在、备份内部不得含重解析点
任何一项不满足都会拒绝删除，避免误删唯一副本。
预览窗口列出每个受影响目录的完整路径、体积、文件数与校验结论，
需勾选确认；默认「移到回收站」（可逆），永久删除需再确认两次。
每批最多 5 项、逐批重新复核，删除后还会确认路径已消失。
注意：移到回收站不会立即释放磁盘空间，需要再清空回收站。
删除 .bak 后「回滚」将不再可用；此时原路径的链接保持有效，
数据完整保存在目标盘，只是不能再一键恢复成迁移前的目录结构。
"""
        txt.insert("1.0", body.strip())
        txt.configure(state="disabled")

    # ---------------- 交互 ----------------
    def _choose_target(self):
        d = filedialog.askdirectory(title="选择目标根目录", initialdir="E:/")
        if d:
            self.target_var.set(os.path.normpath(d))
            self._refresh_disks()
            self._rebuild_rows_target()

    def _on_tree_click(self, event):
        if self.busy:
            return
        region = self.tree.identify("region", event.x, event.y)
        col = self.tree.identify_column(event.x)
        if region != "cell" or col != "#1":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        idx = int(iid)
        self.rows[idx]["checked"] = not self.rows[idx]["checked"]
        self._set_row(idx)
        self._update_summary()
        self._update_go_state()

    def _set_row(self, idx):
        r = self.rows[idx]
        mark = "[v]" if r["checked"] else "[ ]"
        size = human_size(r["size"]) if r["size"] >= 0 else "-"
        files = "{:,}".format(r["files"]) if r["files"] >= 0 else "-"
        state = r["state"]
        tag = {"待迁移": "pending", "进行中": "running", "已迁移": "ok",
               "已联接": "ok", "跳过": "skip", "失败": "err",
               "已回滚": "rollback", "未迁移": "skip"}.get(state, "pending")
        self.tree.item(str(idx), values=(mark, r["rel"], r["desc"], size, files,
                                         state, r["target"]), tags=(tag,))

    def _rebuild_rows_target(self):
        for i, r in enumerate(self.rows):
            r["target"] = os.path.join(self.target_var.get(), r["rel"])
            self._set_row(i)
        self._draw_map()

    def _init_rows(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.rows = []
        for i, (rel, desc, group) in enumerate(MIGRATE_ITEMS):
            row = {"rel": rel, "desc": desc, "group": group,
                   "checked": rel not in ACTIVE_ITEMS, "size": -1, "files": -1,
                   "state": "不建议迁移" if rel in ACTIVE_ITEMS else "待迁移",
                   "target": os.path.join(self.target_var.get(), rel)}
            self.rows.append(row)
            self.tree.insert("", "end", iid=str(i))
            self._set_row(i)
        self._update_summary()

    def _update_summary(self):
        sel = [r for r in self.rows if r["checked"]]
        known = sum(r["size"] for r in sel if r["size"] > 0)
        self.summary_var.set("已选 %d / %d 项  ·  预计迁移 %s  ·  %s 预计释放约 %s" %
                             (len(sel), len(self.rows), human_size(known),
                              self._vol_label(self.source_var.get()), human_size(known)))

    def _update_go_state(self):
        ok = (not self.busy and self.confirm_var.get() == 1 and
              self.dry_run_done and any(r["checked"] for r in self.rows))
        self.btn_go.configure(state="normal" if ok else "disabled",
                              bg=OK if ok else "#B9C7D6")

    def _set_busy(self, value):
        self.busy = value
        for w in (self.chk,):
            w.configure(state="disabled" if value else "normal")
        self._update_go_state()

    def _log(self, level, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_disks(self):
        c = self.disk_canvas
        c.delete("all")
        w = c.winfo_width()
        if w < 340:
            return                      # 布局尚未完成，等 <Configure> 回调重画
        rows = [("C: 系统盘", self.source_var.get(), ACCENT),
                ("E: 目标盘", self.target_var.get(), OK)]
        x0, x1 = 112, w - 222
        for i, (label, drive, color) in enumerate(rows):
            y = 12 + i * 46
            total, used, free = disk_usage(drive)
            c.create_text(14, y + 14, text=label, anchor="w", fill=TEXT,
                          font=(FONT_FAMILY, 9, "bold"))
            c.create_rectangle(x0, y + 6, x1, y + 26, fill="#EDF1F6", outline="")
            if total:
                frac = min(1.0, used / float(total))
                c.create_rectangle(x0, y + 6, x0 + (x1 - x0) * frac, y + 26,
                                   fill=color, outline="")
                c.create_text(x1 - 8, y + 16, anchor="e",
                              text="%.0f%%" % (frac * 100),
                              fill="#FFFFFF" if frac > 0.18 else MUTED,
                              font=(FONT_FAMILY, 8, "bold"))
            # 右侧信息统一右对齐到画布内边缘，完整显示且不会被裁掉
            c.create_text(w - 12, y + 8, anchor="e",
                          text="可用 %s / 共 %s" %
                               (human_size(free), human_size(total)),
                          fill=TEXT, font=(FONT_FAMILY, 9, "bold"))
            c.create_text(w - 12, y + 26, anchor="e",
                          text="已用 %s" % human_size(used),
                          fill=MUTED, font=FONT_SM)

    def _draw_map(self):
        c = self.map_canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 700), max(c.winfo_height(), 340)
        if not self.rows:
            return
        c.create_text(24, 20, text="C: 源目录（体积比例）", anchor="w",
                      fill=MUTED, font=FONT_SM)
        c.create_text(w - 24, 20, text="E: 目标目录", anchor="e",
                      fill=MUTED, font=FONT_SM)
        sizes = [r["size"] if r["size"] > 0 else 0 for r in self.rows]
        mx = max(sizes) if max(sizes) > 0 else 1
        top, step = 44, min(38, max(28, (h - 90) / max(1, len(self.rows))))
        left_x, right_x = 24, w - 24
        bar_w = 200
        mid = w / 2
        for i, r in enumerate(self.rows):
            y = top + i * step
            state = r["state"]
            color = {"已迁移": OK, "已联接": OK, "进行中": ACCENT,
                     "失败": ERR, "跳过": IDLE, "已回滚": WARN}.get(state, "#C7D2DE")
            fill = {"已迁移": OK_LT, "已联接": OK_LT, "进行中": ACCENT_LT,
                    "失败": ERR_LT, "跳过": IDLE_LT, "已回滚": WARN_LT}.get(state, "#F3F6FA")
            bw = max(4, bar_w * (sizes[i] / mx)) if sizes[i] else 4
            c.create_rectangle(left_x, y, left_x + bw, y + 20, fill=fill,
                               outline=color, width=1)
            c.create_text(left_x + 6, y + 10, anchor="w", text=r["rel"],
                          fill=TEXT, font=FONT_MONO)
            if sizes[i]:
                c.create_text(left_x + bw + 8, y + 10, anchor="w",
                              text=human_size(sizes[i]), fill=MUTED, font=FONT_SM)
            c.create_line(left_x + bar_w + 12, y + 10, mid - 6, y + 10,
                          fill="#CFD8E3", arrow="last")
            c.create_text(mid, y + 10, text="junction", fill=IDLE, font=FONT_SM)
            c.create_line(mid + 44, y + 10, right_x - bar_w - 12, y + 10,
                          fill="#CFD8E3", arrow="last")
            c.create_rectangle(right_x - bar_w, y, right_x, y + 20,
                               fill=fill, outline=color, width=1)
            c.create_text(right_x - bar_w + 6, y + 10, anchor="w",
                          text=os.path.basename(r["target"].rstrip("\\")),
                          fill=TEXT, font=FONT_MONO)
            c.create_text(right_x - 6, y + 10, anchor="e", text=state,
                          fill=color, font=(FONT_FAMILY, 8, "bold"))
        total = sum(s for s in sizes)
        c.create_text(24, h - 20, anchor="w",
                      text="合计 %s  ·  已迁移项的新数据写入 %s 侧" %
                      (human_size(total), self._vol_label()),
                      fill=ACCENT, font=(FONT_FAMILY, 9, "bold"))

    # ---------------- 动作 ----------------
    def _vol_label(self, path=None):
        """目标卷的显示名，避免界面把 E: 写死（目标盘可在界面里改）。"""
        d = os.path.splitdrive(os.path.abspath(path or self.target_var.get()))[0]
        return d or "目标盘"

    def _engine(self, use_all=False):
        # 验证 / 回滚必须覆盖全部目录，不受勾选状态影响
        if use_all:
            picked = list(range(len(self.rows)))
        else:
            picked = [i for i, r in enumerate(self.rows) if r["checked"]]
        items = [(self.rows[i]["rel"], self.rows[i]["desc"], self.rows[i]["group"],
                  self.rows[i]["size"], self.rows[i]["files"]) for i in picked]
        # 引擎回传的是子集索引，需要映射回完整 rows
        self._active_indices = picked
        return MigrationEngine(self.source_var.get(), self.target_var.get(),
                               items, self.q.put)

    def _run_async(self, method_name, mark_states=False, use_all=False):
        if self.busy:
            return
        if not self.rows:
            self._init_rows()
        if mark_states:
            for i, r in enumerate(self.rows):
                if r["checked"]:
                    r["state"] = "进行中"
                    self._set_row(i)
        self._set_busy(True)
        labels = {"dry_run": "扫描 / 预演", "run": "迁移",
                  "verify": "验证链接", "rollback": "回滚"}
        self.status_var.set("运行中 · %s" % labels.get(method_name, method_name))
        eng = self._engine(use_all)

        def worker():
            getattr(eng, method_name)()

        threading.Thread(target=worker, daemon=True).start()

    def _scan(self):
        if self.busy:
            return
        self._init_rows()
        self.dry_run_done = False
        self._log("INFO", "开始扫描源目录体积（只读）")
        self._run_async("dry_run")

    def _dry_run(self):
        if self.busy:
            return
        self.dry_run_done = False
        self._log("INFO", "预演：列出将要迁移的内容，不写入任何文件")
        self._run_async("dry_run")

    def _start(self):
        if self.busy or not self.dry_run_done or self.confirm_var.get() != 1:
            return
        procs = running_app_processes()
        if procs:
            if not messagebox.askyesno(
                    "检测到应用仍在运行",
                    "检测到可能占用目录的进程：\n%s\n\n"
                    "运行中迁移会遇到 WinError 5（拒绝访问）：Windows 不允许重命名"
                    "内部文件仍被打开的目录。\n"
                    "注意：当前正在使用的 WorkBuddy 会话本身就会持续写入 "
                    "workspace\\sessions，因此该项已默认不勾选。\n\n"
                    "建议保持 sessions 不勾选，只迁移其它静态目录。\n\n"
                    "仍要继续吗？" % ", ".join(procs[:12]),
                    default=messagebox.NO):
                return
        sel = [r for r in self.rows if r["checked"]]
        total = sum(r["size"] for r in sel if r["size"] > 0)
        msg = ("即将迁移 %d 项，合计 %s。\n"
               "源卷 %s  ->  目标卷 %s（跨卷属正常场景）\n\n"
               "源目录只会被改名为 .bak，不会删除；\n"
               "迁移后原路径变为目录联接，新数据写入 %s 侧。\n\n"
               "确认继续？" % (len(sel), human_size(total),
                             self._vol_label(self.source_var.get()),
                             self._vol_label(), self._vol_label()))
        if not messagebox.askyesno("确认迁移", msg):
            return
        self._log("INFO", "用户已确认，开始迁移")
        self._run_async("run", mark_states=True)

    def _verify(self):
        if self.busy:
            return
        self._log("INFO", "读取现有重解析点目标（覆盖全部目录，不受勾选影响）")
        self._run_async("verify", use_all=True)

    def _rollback(self):
        if self.busy:
            return
        if not messagebox.askyesno(
                "确认回滚",
                "将删除已建立的目录联接，并把 .bak 改回原名。\n"
                "%s 侧的副本会保留，不会删除。\n\n确认回滚？" % self._vol_label()):
            return
        self._run_async("rollback", use_all=True)

    # ---------------- 备份清理 ----------------
    def _delete_backups(self):
        """一键删除 .bak：先只读扫描 + 安全校验，再逐项核对确认，最后分批处理。"""
        if self.busy:
            return
        source = self.source_var.get()
        target = self.target_var.get()
        rels = ([r["rel"] for r in self.rows] if self.rows
                else [x[0] for x in MIGRATE_ITEMS])
        self._log("INFO", "扫描 .bak 备份目录（只读）")
        try:
            entries = inspect_backups(source, target, rels)
        except Exception as e:
            self._log("ERR", "扫描备份失败: %s" % e)
            messagebox.showerror("删除备份", "扫描失败：%s" % e)
            return
        if not entries:
            messagebox.showinfo(
                "删除备份",
                "未发现任何 .bak 备份目录。\n\n"
                "只有迁移成功、原路径已变成目录联接之后才会产生 .bak。\n"
                "若之前迁移中断，目标盘可能留有冗余副本，它不在本功能范围内，"
                "请手动确认后再处理。")
            self._log("INFO", "未发现 .bak 备份，无需清理")
            return
        ok = [e for e in entries if e["ok"]]
        blocked = [e for e in entries if not e["ok"]]
        self._log("INFO", "发现 %d 个备份：可删除 %d，安全校验拒绝 %d" %
                  (len(entries), len(ok), len(blocked)))

        choice = self._backup_dialog(entries)
        if choice is None:
            self._log("INFO", "用户取消删除备份")
            return
        permanent = choice
        if not ok:
            messagebox.showwarning("删除备份",
                                   "所有备份都未通过安全校验，没有可删除的项目。")
            return
        noun = "永久删除（不可恢复）" if permanent else "移到回收站（可恢复）"
        if not messagebox.askyesno(
                "二次确认",
                "即将对 %d 个备份执行：%s\n\n共 %s。\n\n确认继续？" %
                (len(ok), noun, human_size(sum(e["size"] for e in ok))),
                default=messagebox.NO):
            return
        if permanent and not messagebox.askyesno(
                "永久删除 · 最后确认",
                "永久删除不会进入回收站，无法撤销。\n\n"
                "请再次确认已验证所有迁移后的目录联接与新数据均正常。\n\n"
                "真的要永久删除吗？", default=messagebox.NO):
            return
        self._log("WARN", "用户确认：%s，共 %d 项" % (noun, len(ok)))
        self._run_delete(ok, permanent, source, target)

    def _backup_dialog(self, entries):
        """列出每个受影响文件的完整路径、体积与安全结论，要求逐项核对。"""
        win = tk.Toplevel(self)
        win.title("删除备份 · 逐项核对")
        win.geometry("860x560")
        win.configure(bg=BG)
        win.transient(self)
        win.grab_set()
        result = {"choice": None}

        tk.Label(win, text="删除备份前请逐项核对", bg=BG, fg=TEXT,
                 font=(FONT_FAMILY, 14, "bold")).pack(anchor="w", padx=18, pady=(14, 2))
        tk.Label(win, text="⚠ 此操作非常危险，可能导致不可逆的数据丢失！",
                 bg=ERR_LT, fg=ERR, font=(FONT_FAMILY, 11, "bold"),
                 anchor="w", padx=10, pady=8).pack(fill="x", padx=18, pady=(6, 4))
        tk.Label(win, text="只有标记「可删除」的项目会被处理；安全校验未通过的一律跳过。",
                 bg=BG, fg=MUTED, font=FONT_SM, anchor="w").pack(fill="x", padx=18)

        txt = scrolledtext.ScrolledText(win, bg=CARD, fg=TEXT, font=FONT_MONO,
                                        relief="solid", bd=1, wrap="word")
        txt.pack(fill="both", expand=True, padx=18, pady=(8, 6))
        for e in entries:
            txt.insert("end", "[%s] %s\n" %
                       ("可删除" if e["ok"] else "已拒绝", e["bak"]))
            txt.insert("end", "    体积 %s / %d 文件\n" %
                       (human_size(e["size"]), e["files"]))
            txt.insert("end", "    源路径(应为链接) : %s\n" % e["src"])
            txt.insert("end", "    链接目标          : %s\n" %
                       (e["target"] or "读取失败"))
            if e["why"]:
                txt.insert("end", "    拒绝原因          : %s\n" % e["why"])
            txt.insert("end", "\n")
        txt.configure(state="disabled")

        mode = tk.IntVar(value=0)
        cf = tk.BooleanVar(value=False)
        mf = tk.Frame(win, bg=BG)
        mf.pack(fill="x", padx=18)
        tk.Radiobutton(mf, text="移到回收站（推荐，可恢复）", variable=mode, value=0,
                       bg=BG, fg=TEXT, activebackground=BG, font=FONT_SM,
                       selectcolor=CARD).pack(anchor="w")
        tk.Radiobutton(mf, text="永久删除（不可恢复，需二次确认）", variable=mode, value=1,
                       bg=BG, fg=ERR, activebackground=BG, font=FONT_SM,
                       selectcolor=CARD).pack(anchor="w")
        tk.Label(win,
                 text="提示：移到回收站不会立即释放磁盘空间，需要再清空回收站；\n"
                      "删除备份后「回滚」将不可用（数据仍在目标盘，链接保持有效）。",
                 bg=BG, fg=MUTED, font=FONT_SM, justify="left",
                 anchor="w").pack(fill="x", padx=18, pady=(4, 0))
        tk.Checkbutton(win,
                       text="我已逐项核对上述完整路径，并确认对应的目录联接与新数据均正常，且不再需要回滚",
                       variable=cf, bg=BG, fg=TEXT, activebackground=BG,
                       font=(FONT_FAMILY, 9, "bold")).pack(anchor="w", padx=18, pady=(8, 0))

        bf = tk.Frame(win, bg=BG)
        bf.pack(fill="x", padx=18, pady=(8, 14))

        def confirm():
            if not cf.get():
                messagebox.showwarning("需要确认", "请先勾选确认框。", parent=win)
                return
            result["choice"] = bool(mode.get())
            win.destroy()

        self._btn(bf, "继续", confirm, ERR, "#FFFFFF").pack(side="right")
        self._btn(bf, "取消", win.destroy, IDLE_LT, TEXT).pack(side="right", padx=8)
        self.wait_window(win)
        return result["choice"]

    def _run_delete(self, entries, permanent, source, target):
        """后台分批删除；每批前重新校验，删除后逐项确认路径已消失。"""
        self._set_busy(True)
        self.status_var.set("运行中 · 删除备份")
        self.progress["value"] = 0

        def worker():
            n_ok, n_skip, results = 0, 0, []
            try:
                batches = [entries[i:i + BACKUP_BATCH]
                           for i in range(0, len(entries), BACKUP_BATCH)]
                for bi, batch in enumerate(batches):
                    self.q.put(("log", "INFO", "第 %d/%d 批，共 %d 项" %
                                (bi + 1, len(batches), len(batch))))
                    for e in batch:
                        fresh = inspect_backups(source, target, [e["rel"]])
                        cur = fresh[0] if fresh else None
                        if not cur or not cur["ok"]:
                            why = cur["why"] if cur else "备份已不存在"
                            self.q.put(("log", "WARN",
                                        "跳过 %s：%s" % (e["rel"], why)))
                            n_skip += 1
                            continue
                        how = delete_path(cur["bak"], permanent=permanent)
                        if path_exists_any(cur["bak"]):
                            raise RuntimeError("删除后路径仍存在，已中止后续批次：" +
                                               cur["bak"])
                        self.q.put(("log", "OK", "%s 已%s（%s / %d 文件）" %
                                    (e["rel"], how, human_size(cur["size"]),
                                     cur["files"])))
                        n_ok += 1
                        results.append({"rel": e["rel"], "bak": cur["bak"],
                                        "size": cur["size"], "how": how})
                    self.q.put(("progress",
                                (bi + 1) * 100.0 / max(1, len(batches)),
                                "已处理 %d/%d 批" % (bi + 1, len(batches))))
                self.q.put(("backups_deleted", n_ok, n_skip, results, permanent))
            except Exception as ex:
                self.q.put(("log", "ERR", "删除备份中断: %s" % ex))
                self.q.put(("log", "ERR",
                            traceback.format_exc().strip().splitlines()[-1]))
                self.q.put(("backup_failed", str(ex)))
            finally:
                self.q.put(("busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def _export(self):
        if not self.rows:
            self._init_rows()
        path = filedialog.asksaveasfilename(
            title="导出迁移清单", defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")],
            initialfile="workbuddy_migration_plan.csv")
        if not path:
            return
        data = [{"source": os.path.join(self.source_var.get(), r["rel"]),
                 "target": r["target"], "desc": r["desc"],
                 "checked": r["checked"], "bytes": r["size"],
                 "files": r["files"], "state": r["state"]} for r in self.rows]
        try:
            if path.lower().endswith(".json"):
                with open(path, "w", encoding="utf-8") as fp:
                    json.dump({"source_root": self.source_var.get(),
                               "target_root": self.target_var.get(),
                               "keep": [k[0] for k in KEEP_ITEMS],
                               "items": data}, fp, ensure_ascii=False, indent=2)
            else:
                with open(path, "w", encoding="utf-8-sig", newline="") as fp:
                    w = csv.writer(fp)
                    w.writerow(["源路径", "目标路径", "内容", "是否选中",
                                "字节数", "文件数", "状态"])
                    for d in data:
                        w.writerow([d["source"], d["target"], d["desc"],
                                    d["checked"], d["bytes"], d["files"], d["state"]])
            self._log("OK", "清单已导出: " + path)
        except Exception as e:
            self._log("ERR", "导出失败: %s" % e)

    # ---------------- 队列消费 ----------------
    def _active_row_index(self, i):
        """引擎选中子集的第 i 项 → 完整 rows 的索引。"""
        active = getattr(self, "_active_indices", None)
        if active and i < len(active):
            return active[i]
        return i

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1], msg[2])
                elif kind == "progress":
                    self.progress["value"] = msg[1]
                    self.progress_label.configure(text=msg[2])
                elif kind == "scan_item":
                    idx = self._active_row_index(msg[1])
                    self.rows[idx]["size"] = msg[2]
                    self.rows[idx]["files"] = msg[3]
                    self._set_row(idx)
                    self._update_summary()
                    self._draw_map()
                elif kind == "item_state":
                    idx = self._active_row_index(msg[1])
                    self.rows[idx]["state"] = msg[2]
                    detail = msg[3] if len(msg) > 3 else ""
                    if detail:
                        self._log("INFO", "%s -> %s" % (self.rows[idx]["rel"], detail))
                    self._set_row(idx)
                    self._draw_map()
                elif kind == "dry_done":
                    self.dry_report = msg[1]
                    self.dry_run_done = True
                    self.status_var.set("预演完成 · 可核对清单")
                    self.badge.configure(text=" 预演完成 ", bg=ACCENT_LT, fg=ACCENT)
                    self.progress["value"] = 100
                    self._update_go_state()
                    self._refresh_disks()
                elif kind == "done":
                    failed = msg[2] if len(msg) > 2 else []
                    if failed:
                        self.status_var.set("部分完成 · %d 项失败，请查看日志" % len(failed))
                        self.badge.configure(text=" 部分完成 ", bg=WARN_LT, fg=WARN)
                        self.progress_label.configure(text="完成（有失败项）")
                    else:
                        self.status_var.set("迁移完成 · 请验证链接")
                        self.badge.configure(text=" 已迁移 ", bg=OK_LT, fg=OK)
                        self.progress_label.configure(text="全部完成")
                    self._show_report(msg[1], failed)
                    self._refresh_disks()
                elif kind == "dry_failed":
                    self.status_var.set("预演失败 · 请查看日志")
                    self.badge.configure(text=" 预演失败 ", bg=ERR_LT, fg=ERR)
                    messagebox.showerror("预演失败", msg[1])
                elif kind == "failed":
                    self.status_var.set("迁移中断 · 请查看日志")
                    self.badge.configure(text=" 已中断 ", bg=ERR_LT, fg=ERR)
                    messagebox.showerror("迁移中断", msg[1])
                elif kind == "backups_deleted":
                    n_ok, n_skip, results, permanent = msg[1], msg[2], msg[3], msg[4]
                    self.status_var.set("备份已清理")
                    self.badge.configure(text=" 备份已删 ", bg=OK_LT, fg=OK)
                    self.progress_label.configure(text="删除完成 %d 项" % n_ok)
                    self._refresh_disks()
                    extra = ("\n\n文件在回收站中，需清空回收站才会真正释放磁盘空间。"
                             if not permanent else "\n\n已永久删除，无法恢复。")
                    messagebox.showinfo(
                        "删除备份完成",
                        "已处理 %d 项，跳过 %d 项，涉及 %s。%s" %
                        (n_ok, n_skip,
                         human_size(sum(r["size"] for r in results)), extra))
                elif kind == "backup_failed":
                    self.status_var.set("删除备份中断 · 请查看日志")
                    self.badge.configure(text=" 删除中断 ", bg=ERR_LT, fg=ERR)
                    messagebox.showerror("删除备份中断", msg[1])
                elif kind == "rolled_back":
                    self.status_var.set("回滚完成")
                    self.badge.configure(text=" 已回滚 ", bg=WARN_LT, fg=WARN)
                    self.dry_run_done = False
                elif kind == "busy":
                    self._set_busy(msg[1])
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _show_report(self, done, failed=None):
        failed = failed or []
        win = tk.Toplevel(self)
        win.title("迁移报告%s" % ("（部分失败）" if failed else ""))
        win.geometry("760x460")
        win.configure(bg=BG)
        tk.Label(win,
                 text=("部分完成：%d 项成功 / %d 项失败" % (len(done), len(failed))
                       if failed else "迁移完成"),
                 bg=BG, fg=WARN if failed else OK,
                 font=(FONT_FAMILY, 15, "bold")).pack(anchor="w", padx=18, pady=(14, 2))
        total = sum(d["size"] for d in done)
        src_vol = self._vol_label(self.source_var.get())
        dst_vol = self._vol_label()
        tk.Label(win, text="共 %d 项成功，%s 已从 %s 转移到 %s 侧；源数据保留为 .bak" %
                 (len(done), human_size(total), src_vol, dst_vol),
                 bg=BG, fg=MUTED,
                 font=FONT_SM).pack(anchor="w", padx=18, pady=(0, 10))
        txt = scrolledtext.ScrolledText(win, bg=CARD, fg=TEXT, font=FONT_MONO,
                                        relief="solid", bd=1)
        txt.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        for d in done:
            txt.insert("end", "[OK] %s\n" % d["rel"])
            txt.insert("end", "     源(现为链接) : %s\n" % d["src"])
            txt.insert("end", "     实体数据      : %s\n" % d["dst"])
            txt.insert("end", "     源备份        : %s\n" % d["bak"])
            txt.insert("end", "     体积 / 文件   : %s / %d\n\n" %
                       (human_size(d["size"]), d["files"]))
        if failed:
            txt.insert("end", "\n===== 失败项（源目录未改动，已自动跳过） =====\n\n")
            for f in failed:
                txt.insert("end", "[失败] %s\n" % f.get("rel", "?"))
                txt.insert("end", "     源路径 : %s\n" % f.get("src", ""))
                txt.insert("end", "     原因   : %s\n\n" % f.get("error", ""))
            txt.insert("end", "处理建议：取消勾选被占用的目录（如 workspace\\sessions），\n")
            txt.insert("end", "重新扫描并迁移其余项目；失败项不会产生不一致的链接。\n\n")
        txt.insert("end", "提示：新产生的数据写入 %s 侧；未迁移的目录仍在 %s。\n" %
                   (dst_vol, src_vol))
        txt.insert("end", "确认稳定后可用「删除备份」清理 .bak，释放 %s 空间。\n" % src_vol)
        txt.insert("end", "升级应用后请点「验证链接」复查。\n")
        txt.configure(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy, bg=ACCENT, fg="#FFFFFF",
                  relief="flat", bd=0, padx=20, pady=6).pack(pady=(0, 14))


def main():
    if not IS_WINDOWS:
        print("本工具仅支持 Windows。")
        return
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
