"""Évite que Windows bride Nova quand un jeu (CS2…) est au premier plan.

- refus du mode « efficacité » (EcoQoS) et du ralentissement des minuteries des processus en arrière-plan
  (API officielle SetProcessInformation / ProcessPowerThrottling, Windows 10 1709+ et 11) ;
- priorité « supérieure à la normale » (le jeu reste prioritaire sur le GPU, Nova garde un cœur de processeur) ;
- minuterie système à 1 ms pour une cadence de tracking régulière."""
import ctypes
import logging
import sys

log = logging.getLogger("nova.perf")
ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
NORMAL_PRIORITY_CLASS = 0x20
PROCESS_POWER_THROTTLING = 4
THROTTLE_EXECUTION_SPEED = 0x1
THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4
_timer_on = False


class _Throttling(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def _kernel32():
    """kernel32 avec des signatures 64 bits correctes (un HANDLE ne tient pas dans un int 32 bits)."""
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.SetPriorityClass.restype = wintypes.BOOL
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.GetPriorityClass.restype = wintypes.DWORD
    k32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.SetProcessInformation.restype = wintypes.BOOL
    return k32


def boost(enabled: bool = True) -> dict:
    """Applique (ou retire) les réglages anti-bridage au processus courant. Retourne ce qui a réussi."""
    global _timer_on
    result = {"priority": False, "no_ecoqos": False, "timer": False}
    if sys.platform != "win32":
        return result
    k32 = _kernel32()
    proc = k32.GetCurrentProcess()
    result["priority"] = bool(k32.SetPriorityClass(proc, ABOVE_NORMAL_PRIORITY_CLASS if enabled else NORMAL_PRIORITY_CLASS))
    # activé : bridage refusé ; désactivé : Windows reprend la main (ControlMask = 0), sans forcer le mode « efficacité »
    st = _Throttling(1, (THROTTLE_EXECUTION_SPEED | THROTTLE_IGNORE_TIMER_RESOLUTION) if enabled else 0, 0)
    try:
        result["no_ecoqos"] = bool(k32.SetProcessInformation(proc, PROCESS_POWER_THROTTLING, ctypes.byref(st),
                                                             ctypes.sizeof(st)))
    except AttributeError:      # Windows trop ancien
        pass
    try:
        winmm = ctypes.windll.winmm
        if enabled and not _timer_on:
            _timer_on = winmm.timeBeginPeriod(1) == 0
        elif not enabled and _timer_on:
            _timer_on = winmm.timeEndPeriod(1) != 0      # chaque timeBeginPeriod doit avoir un seul timeEndPeriod
        result["timer"] = _timer_on == enabled
    except OSError:
        pass
    log.info("Anti-bridage (jeux) %s : %s", "activé" if enabled else "désactivé", result)
    return result


def priority_class() -> int:
    if sys.platform != "win32":
        return 0
    k32 = _kernel32()
    return k32.GetPriorityClass(k32.GetCurrentProcess())
