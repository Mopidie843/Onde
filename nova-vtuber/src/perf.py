"""Anti-bridage : Nova garde toute sa fluidité quand un jeu (CS2, Fortnite, Valorant…) est au premier plan.

Uniquement des moyens officiels de Windows, appliqués à Nova et à ses propres sous-processus (jamais au jeu :
aucun risque avec les anti-triche) :
- processus : priorité « supérieure à la normale » (« haute » pendant un jeu), refus du mode « efficacité »
  (EcoQoS) et du ralentissement des minuteries, priorité mémoire normale, minuterie système à 1 ms ;
- sous-processus de Nova (moteur d'affichage de l'avatar QtWebEngine, suivi des bras) : mêmes réglages ;
  le processus GPU du moteur d'affichage reçoit en plus une priorité GPU plus haute ;
- fils de capture et de suivi : planificateur multimédia de Windows (MMCSS, comme l'audio et les jeux) et refus
  d'EcoQoS au niveau du fil ;
- surveillance toutes les 2 s : détection du jeu au premier plan (mode jeu = réglages renforcés), réapplication
  si Windows ou le gestionnaire des tâches (« mode efficacité ») les a retirés, nouveaux sous-processus couverts.
Le moteur d'affichage (Chromium) ne doit pas non plus se brider quand la fenêtre est recouverte par le jeu : voir
CHROMIUM_FLAGS, appliqués par main.py avant le démarrage de Qt."""
import ctypes
import logging
import os
import sys
import threading

from . import health

log = logging.getLogger("nova.perf")

# Chromium (QtWebEngine) : pas de pause ni de ralentissement quand la fenêtre est cachée ou recouverte par le jeu
CHROMIUM_FLAGS = ["--disable-renderer-backgrounding", "--disable-background-timer-throttling",
                  "--disable-backgrounding-occluded-windows"]
CHROMIUM_DISABLED_FEATURES = ["CalculateNativeWinOcclusion", "IntensiveWakeUpThrottling", "UseEcoQoSForBackgroundProcess"]

NORMAL_PRIORITY_CLASS = 0x20
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
HIGH_PRIORITY_CLASS = 0x80
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_MEMORY_PRIORITY = 0          # PROCESS_INFORMATION_CLASS.ProcessMemoryPriority
PROCESS_POWER_THROTTLING = 4         # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
THREAD_POWER_THROTTLING = 3          # THREAD_INFORMATION_CLASS.ThreadPowerThrottling
MEMORY_PRIORITY_NORMAL = 5
THROTTLE_EXECUTION_SPEED = 0x1
THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4
THREAD_PRIORITY_HIGHEST = 2
AVRT_PRIORITY_HIGH = 1
D3DKMT_PRIORITY = {"normal": 2, "above_normal": 3, "high": 4}

GAMES = {   # exécutables de jeux courants (le plein écran au premier plan est aussi détecté pour les autres)
    "cs2.exe": "Counter-Strike 2", "csgo.exe": "CS:GO", "fortniteclient-win64-shipping.exe": "Fortnite",
    "valorant-win64-shipping.exe": "Valorant", "league of legends.exe": "League of Legends",
    "r5apex.exe": "Apex Legends", "r5apex_dx12.exe": "Apex Legends", "rocketleague.exe": "Rocket League",
    "gta5.exe": "GTA V", "gta5_enhanced.exe": "GTA V", "overwatch.exe": "Overwatch 2", "cod.exe": "Call of Duty",
    "robloxplayerbeta.exe": "Roblox", "rainbowsix.exe": "Rainbow Six Siege", "rainbowsix_be.exe": "Rainbow Six Siege",
    "minecraft.windows.exe": "Minecraft", "javaw.exe": "Minecraft (Java)", "dota2.exe": "Dota 2",
    "eldenring.exe": "Elden Ring", "cyberpunk2077.exe": "Cyberpunk 2077", "pubg.exe": "PUBG",
    "tslgame.exe": "PUBG", "deadbydaylight-win64-shipping.exe": "Dead by Daylight", "rust.exe": "Rust",
    "destiny2.exe": "Destiny 2", "marvel-win64-shipping.exe": "Marvel Rivals", "thefinals.exe": "The Finals",
    "helldivers2.exe": "Helldivers 2", "eafc25.exe": "EA FC", "eafc26.exe": "EA FC", "fifa23.exe": "FIFA",
    "warzone.exe": "Warzone", "bf2042.exe": "Battlefield 2042", "palworld-win64-shipping.exe": "Palworld",
    "genshinimpact.exe": "Genshin Impact", "zenlesszonezero.exe": "Zenless Zone Zero", "starrail.exe": "Honkai Star Rail",
    "lethal company.exe": "Lethal Company", "phasmophobia.exe": "Phasmophobia", "osu!.exe": "osu!",
}
NOT_GAMES = {"explorer.exe", "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
             "applicationframehost.exe", "textinputhost.exe", "lockapp.exe", "obs64.exe", "discord.exe",
             "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "brave.exe", "vlc.exe", "mpc-hc64.exe",
             "powerpnt.exe", "taskmgr.exe", "steamwebhelper.exe", "nvidia app.exe"}
OWN_CHILDREN = ("qtwebengineprocess.exe", "python.exe", "pythonw.exe")


# ---------------------------------------------------------------- appels Windows (signatures 64 bits)
class _Throttling(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


class _MemoryPriority(ctypes.Structure):
    _fields_ = [("MemoryPriority", ctypes.c_ulong)]


_api = None


def _win():
    """Fonctions Windows avec des types corrects (un HANDLE ne tient pas dans un int 32 bits). None hors Windows."""
    global _api
    if _api is not None or sys.platform != "win32":
        return _api
    from ctypes import wintypes as w
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sig = {
        "GetCurrentProcess": ([], w.HANDLE), "GetCurrentThread": ([], w.HANDLE),
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE), "CloseHandle": ([w.HANDLE], w.BOOL),
        "SetPriorityClass": ([w.HANDLE, w.DWORD], w.BOOL), "GetPriorityClass": ([w.HANDLE], w.DWORD),
        "SetProcessInformation": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "SetThreadInformation": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
        "SetThreadPriority": ([w.HANDLE, ctypes.c_int], w.BOOL),
    }
    for name, (args, res) in sig.items():
        f = getattr(k32, name, None)
        if f is not None:
            f.argtypes, f.restype = args, res
    api = {"k32": k32}
    try:
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
        avrt.AvSetMmThreadCharacteristicsW.restype = w.HANDLE
        avrt.AvSetMmThreadPriority.argtypes = [w.HANDLE, ctypes.c_int]
        avrt.AvSetMmThreadPriority.restype = w.BOOL
        avrt.AvRevertMmThreadCharacteristics.argtypes = [w.HANDLE]
        avrt.AvRevertMmThreadCharacteristics.restype = w.BOOL
        api["avrt"] = avrt
    except (OSError, AttributeError):
        api["avrt"] = None
    try:
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.D3DKMTSetProcessSchedulingPriorityClass.argtypes = [w.HANDLE, ctypes.c_int]
        gdi.D3DKMTSetProcessSchedulingPriorityClass.restype = ctypes.c_long      # NTSTATUS
        api["gdi"] = gdi
    except (OSError, AttributeError):
        api["gdi"] = None
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetForegroundWindow.restype = w.HWND
        u32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        u32.GetWindowThreadProcessId.restype = w.DWORD
        u32.GetWindowRect.argtypes = [w.HWND, ctypes.c_void_p]
        u32.GetWindowRect.restype = w.BOOL
        u32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
        u32.MonitorFromWindow.restype = w.HMONITOR
        u32.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.c_void_p]
        u32.GetMonitorInfoW.restype = w.BOOL
        u32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        u32.GetClassNameW.restype = ctypes.c_int
        api["u32"] = u32
    except (OSError, AttributeError):
        api["u32"] = None
    try:
        api["winmm"] = ctypes.WinDLL("winmm")
    except OSError:
        api["winmm"] = None
    _api = api
    return api


def _set_process(handle, priority: int, gpu: str | None) -> dict:
    """Réglages anti-bridage d'un processus (le nôtre ou un de nos sous-processus)."""
    api = _win()
    k32 = api["k32"]
    out = {"priority": bool(k32.SetPriorityClass(handle, priority))}
    if priority == NORMAL_PRIORITY_CLASS:      # désactivation : Windows reprend la main
        st = _Throttling(1, 0, 0)
    else:
        st = _Throttling(1, THROTTLE_EXECUTION_SPEED | THROTTLE_IGNORE_TIMER_RESOLUTION, 0)
    out["no_ecoqos"] = bool(k32.SetProcessInformation(handle, PROCESS_POWER_THROTTLING, ctypes.byref(st),
                                                      ctypes.sizeof(st)))
    mp = _MemoryPriority(MEMORY_PRIORITY_NORMAL)
    out["memory"] = bool(k32.SetProcessInformation(handle, PROCESS_MEMORY_PRIORITY, ctypes.byref(mp),
                                                   ctypes.sizeof(mp)))
    if gpu and api["gdi"] is not None:
        out["gpu"] = ""
        for level in _gpu_levels(gpu):
            if api["gdi"].D3DKMTSetProcessSchedulingPriorityClass(handle, D3DKMT_PRIORITY[level]) == 0:
                out["gpu"] = level
                break
    return out


def _gpu_levels(wanted: str) -> list[str]:
    order = ["high", "above_normal", "normal"]
    return order[order.index(wanted):] if wanted in order else ["normal"]


# ---------------------------------------------------------------- fils (capture, suivi)
_mmcss_ok = True


def boost_thread(task: str = "Games"):
    """À appeler DANS le fil à accélérer (capture webcam, suivi). Retourne un jeton pour unboost_thread()."""
    global _mmcss_ok
    api = _win()
    if api is None or not _enabled:
        return None
    k32 = api["k32"]
    me = k32.GetCurrentThread()
    st = _Throttling(1, THROTTLE_EXECUTION_SPEED, 0)
    k32.SetThreadInformation(me, THREAD_POWER_THROTTLING, ctypes.byref(st), ctypes.sizeof(st))
    token = None
    if api["avrt"] is not None and _mmcss_ok:
        idx = ctypes.c_ulong(0)
        token = api["avrt"].AvSetMmThreadCharacteristicsW(task, ctypes.byref(idx))
        if token:
            api["avrt"].AvSetMmThreadPriority(token, AVRT_PRIORITY_HIGH)
        else:
            _mmcss_ok = False        # service multimédia indisponible : priorité de fil classique désormais
    if not token:
        k32.SetThreadPriority(me, THREAD_PRIORITY_HIGHEST)
    return token


def unboost_thread(token) -> None:
    api = _win()
    if api and token and api["avrt"] is not None:
        api["avrt"].AvRevertMmThreadCharacteristics(token)


# ---------------------------------------------------------------- jeu au premier plan
class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _Rect), ("rcWork", _Rect), ("dwFlags", ctypes.c_ulong)]


def foreground_game() -> str:
    """Nom du jeu au premier plan (exécutable connu, ou n'importe quelle fenêtre plein écran), sinon ""."""
    api = _win()
    if api is None or api["u32"] is None:
        return ""
    u32 = api["u32"]
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.c_ulong(0)
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value or pid.value == os.getpid():
        return ""
    try:
        import psutil
        name = psutil.Process(pid.value).name().lower()
    except Exception:  # noqa: BLE001
        return ""
    if name in GAMES:
        return GAMES[name]
    if name in NOT_GAMES or name in OWN_CHILDREN:
        return ""
    cls = ctypes.create_unicode_buffer(64)
    u32.GetClassNameW(hwnd, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return ""
    rect = _Rect()
    mi = _MonitorInfo()
    mi.cbSize = ctypes.sizeof(mi)
    mon = u32.MonitorFromWindow(hwnd, 2)          # MONITOR_DEFAULTTONEAREST
    if not (u32.GetWindowRect(hwnd, ctypes.byref(rect)) and mon and u32.GetMonitorInfoW(mon, ctypes.byref(mi))):
        return ""
    m = mi.rcMonitor
    covers = rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom
    return os.path.splitext(name)[0] if covers else ""


# ---------------------------------------------------------------- surveillance
_enabled = False
_lock = threading.Lock()
_stop = threading.Event()
_thread = None
_timer_on = False
_state = {"game": "", "level": "", "children": 0, "gpu": ""}
CONFIG = {"game_priority": "high", "gpu_priority": "high"}   # "above_normal" pour un réglage plus doux


def game_active() -> bool:
    return bool(_state["game"])


def _own_children():
    try:
        import psutil
        for c in psutil.Process().children(recursive=True):
            try:
                name = c.name().lower()
                if name in OWN_CHILDREN or name == os.path.basename(sys.executable).lower():
                    yield c, " ".join(c.cmdline()).lower()
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return


def _apply(priority: int, gpu: str | None) -> dict:
    api = _win()
    k32 = api["k32"]
    res = _set_process(k32.GetCurrentProcess(), priority, None)
    n, gpu_done = 0, ""
    for child, cmd in _own_children():
        h = k32.OpenProcess(PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION, False, child.pid)
        if not h:
            continue
        try:
            is_gpu = "--type=gpu-process" in cmd
            r = _set_process(h, priority, gpu if is_gpu else None)
            n += r["priority"]
            if is_gpu and r.get("gpu"):
                gpu_done = r["gpu"]
        finally:
            k32.CloseHandle(h)
    res.update(children=n, gpu=gpu_done)
    return res


def _tick() -> None:
    game = foreground_game()
    level = CONFIG["game_priority"] if game else "above_normal"
    prio = HIGH_PRIORITY_CLASS if level == "high" else ABOVE_NORMAL_PRIORITY_CLASS
    gpu = CONFIG["gpu_priority"] if game else "above_normal"
    res = _apply(prio, gpu)
    if game != _state["game"]:
        log.info("Jeu au premier plan : %s -> anti-bridage %s", game or "aucun", "renforcé" if game else "normal")
    _state.update(game=game, level=level, children=res["children"], gpu=res["gpu"] or _state["gpu"])
    health.report("perf", state="game" if game else "on", game=game, priority=level, children=res["children"],
                  gpu=res["gpu"], ecoqos=res["no_ecoqos"],
                  message=(f"Jeu détecté ({game}) : anti-bridage renforcé" if game else "Anti-bridage actif"))


def _run() -> None:
    while not _stop.is_set():
        try:
            _tick()
        except Exception as e:  # noqa: BLE001
            log.warning("Anti-bridage : %s", e)
        _stop.wait(2.0)


def boost(enabled: bool = True) -> dict:
    """Active (ou retire) l'anti-bridage : réglages immédiats + surveillance continue. Retourne ce qui a réussi."""
    global _enabled, _thread, _timer_on
    result = {"priority": False, "no_ecoqos": False, "memory": False, "timer": False}
    api = _win()
    if api is None:
        return result
    with _lock:
        _enabled = enabled
        if enabled:
            result.update(_apply(ABOVE_NORMAL_PRIORITY_CLASS, "above_normal"))
            if _thread is None or not _thread.is_alive():
                _stop.clear()
                _thread = threading.Thread(target=_run, daemon=True, name="nova-antibridage")
                _thread.start()
        else:
            _stop.set()
            result.update(_apply(NORMAL_PRIORITY_CLASS, "normal"))
            _state.update(game="", level="")
            health.report("perf", state="off", message="Anti-bridage désactivé", game="")
        winmm = api["winmm"]
        if winmm is not None:
            if enabled and not _timer_on:
                _timer_on = winmm.timeBeginPeriod(1) == 0
            elif not enabled and _timer_on:
                _timer_on = winmm.timeEndPeriod(1) != 0      # un seul timeEndPeriod par timeBeginPeriod
            result["timer"] = _timer_on == enabled
    log.info("Anti-bridage (jeux) %s : %s", "activé" if enabled else "désactivé", result)
    return result


def priority_class() -> int:
    api = _win()
    if api is None:
        return 0
    return api["k32"].GetPriorityClass(api["k32"].GetCurrentProcess())


def chromium_flags(existing: str = "") -> str:
    """Drapeaux QtWebEngine : ceux déjà présents + anti-bridage (fusionne --disable-features)."""
    parts = existing.split()
    disabled = list(CHROMIUM_DISABLED_FEATURES)
    keep = []
    for p in parts:
        if p.startswith("--disable-features="):
            disabled = [f for f in p.split("=", 1)[1].split(",") if f] + [f for f in disabled if f not in p]
        else:
            keep.append(p)
    for f in CHROMIUM_FLAGS:
        if f not in keep:
            keep.append(f)
    return " ".join(keep + ["--disable-features=" + ",".join(dict.fromkeys(disabled))])
