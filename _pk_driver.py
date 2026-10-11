import ctypes, ctypes.wintypes as wt
import sys, time

t0 = time.perf_counter()
sys.path.insert(0, r"C:\Users\pc\Desktop\随手文件\MiniReader")
from PyQt5.QtWidgets import QApplication

t_qt = time.perf_counter()
app = QApplication([])
t_app = time.perf_counter()
from minireader.storage import Library, ProgressStore, Settings
from minireader.ui.window import MiniWindow

t_imp = time.perf_counter()
w = MiniWindow(Settings.load(), ProgressStore.load(), Library.load())
t_win = time.perf_counter()
w.resize(480, 360)
w.show()
app.processEvents()
t_blank = time.perf_counter()
w.load_path(r"C:\Users\pc\Downloads\[sxsy.org]这个装备栏太棒了.txt")
for _ in range(4):
    app.processEvents()
t_book = time.perf_counter()

for label, t in (("QT_IMPORT", t_qt), ("QAPP_CTOR", t_app), ("APP_IMPORT", t_imp),
                 ("WINDOW_CTOR", t_win), ("BLANK_SHOWN", t_blank), ("BOOK_SHOWN", t_book)):
    print("%s %.1f" % (label, (t - t0) * 1000))


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


c = PMC()
c.cb = ctypes.sizeof(PMC)
h = ctypes.windll.kernel32.GetCurrentProcess()
ok = 0
for dll, fn in (("kernel32", "K32GetProcessMemoryInfo"), ("psapi", "GetProcessMemoryInfo")):
    try:
        f = getattr(getattr(ctypes.windll, dll), fn)
        f.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
        f.restype = wt.BOOL
        ok = f(h, ctypes.byref(c), c.cb)
        if ok:
            print("MEM_VIA %s.%s" % (dll, fn))
            break
    except Exception as e:
        print("MEM_ERR %s.%s %s" % (dll, fn, e))
if ok:
    print("WORKING_SET %.1f" % (c.WorkingSetSize / 1048576))
    print("PEAK_WS %.1f" % (c.PeakWorkingSetSize / 1048576))
    print("PRIVATE %.1f" % (c.PagefileUsage / 1048576))
else:
    print("MEM_FAILED err=%d" % ctypes.get_last_error())
