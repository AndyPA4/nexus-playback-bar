"""Native Windows notification icon and context menu."""
import ctypes as c
from ctypes import wintypes as w


class IconData(c.Structure):
    _fields_ = [
        ("size", w.DWORD), ("hwnd", w.HWND), ("identifier", w.UINT),
        ("flags", w.UINT), ("callback", w.UINT), ("icon", w.HICON),
        ("tip", w.WCHAR * 128),
    ]


class Tray:
    MESSAGE = 0x8001
    LEFT_RELEASE = 0x0202
    RIGHT_RELEASE = 0x0205
    QUIT = 1

    def __init__(self, window, open_settings, quit_app):
        self.window = window
        self.open_settings = open_settings
        self.quit_app = quit_app
        self.pending = None
        self.poll = None
        self.ready = False
        user = self.user = c.WinDLL("user32", use_last_error=True)
        shell = c.WinDLL("shell32", use_last_error=True)

        self.notify = shell.Shell_NotifyIconW
        self.notify.argtypes = (w.DWORD, c.POINTER(IconData))
        self.notify.restype = w.BOOL
        user.CreateWindowExW.argtypes = (
            w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
            c.c_int, c.c_int, c.c_int, c.c_int,
            w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p,
        )
        user.CreateWindowExW.restype = w.HWND
        user.DestroyWindow.argtypes = (w.HWND,)
        user.DestroyWindow.restype = w.BOOL
        user.LoadIconW.argtypes = (w.HINSTANCE, c.c_void_p)
        user.LoadIconW.restype = w.HICON
        user.RegisterWindowMessageW.argtypes = (w.LPCWSTR,)
        user.RegisterWindowMessageW.restype = w.UINT
        user.GetCursorPos.argtypes = (c.POINTER(w.POINT),)
        user.SetForegroundWindow.argtypes = (w.HWND,)
        user.CreatePopupMenu.restype = w.HMENU
        user.AppendMenuW.argtypes = (w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR)
        user.TrackPopupMenu.argtypes = (
            w.HMENU, w.UINT, c.c_int, c.c_int, c.c_int, w.HWND, c.c_void_p,
        )
        user.TrackPopupMenu.restype = w.UINT
        user.DestroyMenu.argtypes = (w.HMENU,)
        user.PostMessageW.argtypes = (w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        result = c.c_ssize_t
        procedure = c.WINFUNCTYPE(result, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.set_proc = user.SetWindowLongPtrW if c.sizeof(c.c_void_p) == 8 else user.SetWindowLongW
        self.set_proc.argtypes = (w.HWND, c.c_int, c.c_void_p)
        self.set_proc.restype = c.c_void_p
        user.CallWindowProcW.argtypes = (c.c_void_p, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        user.CallWindowProcW.restype = result
        self.taskbar_created = user.RegisterWindowMessageW("TaskbarCreated")

        # Keep the notification target alive independently of Tk's visible window.
        hwnd = user.CreateWindowExW(0, "Static", "NEXUS Playback Bar", 0,
                                    0, 0, 0, 0, None, None, None, None)
        if not hwnd:
            raise c.WinError(c.get_last_error())
        self.data = IconData(c.sizeof(IconData), hwnd, 1, 7, self.MESSAGE,
                             user.LoadIconW(None, 32512), "NEXUS Playback Bar")

        @procedure
        def callback(hwnd, message, wp, lp):
            if message == self.MESSAGE:
                if lp == self.LEFT_RELEASE:
                    self.pending = self.open_settings
                elif lp == self.RIGHT_RELEASE:
                    self.pending = self.show_menu
                return 0
            if self.taskbar_created and message == self.taskbar_created:
                self.ready = bool(self.notify(0, c.byref(self.data)))
            return user.CallWindowProcW(self.previous, hwnd, message, wp, lp)

        self.callback = callback
        self.previous = self.set_proc(hwnd, -4, c.cast(callback, c.c_void_p))
        if not self.previous:
            error = c.get_last_error()
            user.DestroyWindow(hwnd)
            raise c.WinError(error)
        self.ready = bool(self.notify(0, c.byref(self.data)))
        self.poll = window.after(25, self.dispatch)

    def dispatch(self):
        # Run Tk and the popup outside the notification window procedure.
        self.poll = None
        action, self.pending = self.pending, None
        if action:
            action()
        if self.data.hwnd:
            self.poll = self.window.after(25, self.dispatch)

    def show_menu(self):
        user = self.user
        cursor = w.POINT()
        if not user.GetCursorPos(c.byref(cursor)):
            return
        menu = user.CreatePopupMenu()
        if not menu:
            return
        hwnd = self.data.hwnd
        try:
            if not user.AppendMenuW(menu, 0, self.QUIT, "Quit"):
                return
            user.SetForegroundWindow(hwnd)
            # Return the selected command and let either mouse button select it.
            selected = user.TrackPopupMenu(menu, 0x0102, cursor.x, cursor.y, 0, hwnd, None)
            user.PostMessageW(hwnd, 0, 0, 0)
        finally:
            user.DestroyMenu(menu)
        if selected == self.QUIT:
            self.quit_app()

    def close(self):
        if self.poll is not None:
            self.window.after_cancel(self.poll)
            self.poll = None
        hwnd = self.data.hwnd
        if hwnd:
            self.notify(2, c.byref(self.data))
            self.set_proc(hwnd, -4, self.previous)
            self.user.DestroyWindow(hwnd)
            self.data.hwnd = None
            self.ready = False
