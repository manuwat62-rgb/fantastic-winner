"""Number Check — Windows multi-monitor, rotated-number OCR."""
import ctypes
import os
import queue
import re
import sys
import threading
import time
import traceback
import tkinter as tk
from collections import Counter
from decimal import Decimal, InvalidOperation
from tkinter import messagebox, ttk

from PIL import Image, ImageEnhance, ImageGrab, ImageOps, ImageTk
import pytesseract

if os.name == 'nt':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

# Locate the OCR engine bundled next to NumberCheck.exe in portable builds.
# Development mode can still use an existing local installation.
if getattr(sys, 'frozen', False):
    base_dir = os.path.dirname(sys.executable)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))
local_tesseract = os.path.join(base_dir, 'Tesseract-OCR', 'tesseract.exe')
installed_tesseract = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
if os.path.isfile(local_tesseract):
    pytesseract.pytesseract.tesseract_cmd = local_tesseract
elif os.path.isfile(installed_tesseract):
    pytesseract.pytesseract.tesseract_cmd = installed_tesseract
else:
    pytesseract.pytesseract.tesseract_cmd = local_tesseract

# Windows native hotkeys (global, independent of which app has focus).
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
HOTKEY_CTRL_ALT_Q = 1001
HOTKEY_F8 = 1002


class WindowsHotkeys:
    """RegisterHotKey + PeekMessage on the Tk main thread (no listener thread)."""
    def __init__(self):
        self.active = {}
        self.failed = []
        if os.name != 'nt':
            self.failed = ['ต้องใช้ Windows เพื่อใช้คีย์ลัดทั่วระบบ']
            return
        from ctypes import wintypes
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self.user32.RegisterHotKey.restype = wintypes.BOOL
        self.user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.UnregisterHotKey.restype = wintypes.BOOL
        self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        self.user32.PeekMessageW.restype = wintypes.BOOL
        self.MSG = wintypes.MSG
        for identifier, name, modifiers, key in (
            (HOTKEY_CTRL_ALT_Q, 'Ctrl + Alt + Q', MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord('Q')),
            (HOTKEY_F8, 'F8', MOD_NOREPEAT, 0x77),
        ):
            if self.user32.RegisterHotKey(None, identifier, modifiers, key):
                self.active[identifier] = name
            else:
                error_code = ctypes.get_last_error()
                self.failed.append(f'{name} (Windows error {error_code}; อาจมีโปรแกรมอื่นใช้ปุ่มนี้)')

    def pop_hotkeys(self):
        if os.name != 'nt':
            return []
        messages = []
        message = self.MSG()
        while self.user32.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            if message.wParam in self.active:
                messages.append(self.active[message.wParam])
        return messages

    def close(self):
        if os.name == 'nt':
            for identifier in self.active:
                self.user32.UnregisterHotKey(None, identifier)
            self.active.clear()


def parse_number(text):
    s = str(text).strip().replace('\u00a0', '').replace(' ', '').replace('−', '-')
    if not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', s):
        return None
    try:
        return Decimal(s.replace(',', ''))
    except InvalidOperation:
        return None


def checksum_ok(number):
    if not re.fullmatch(r'\d{13}', number):
        return False
    check = (11 - (sum(int(d) * (13 - i) for i, d in enumerate(number[:12])) % 11)) % 10
    return check == int(number[-1])


def id_candidates(raw):
    """Read 13-digit groups from OCR lines without joining unrelated card fields."""
    found = []
    for line in raw.splitlines():
        # Thai cards sometimes separate the groups as 1 1234 12345 12 3.
        for match in re.finditer(r'(?<!\d)(?:[0-9][ \t-]*){13}(?![ \t-]*[0-9])', line):
            digits = re.sub(r'\D', '', match.group())
            if len(digits) == 13:
                found.append(digits)
    return found


def ocr_number(image):
    """Scan an entire Thai ID card, including sideways/upside-down cards."""
    from concurrent.futures import ThreadPoolExecutor
    counts = Counter()
    readable = []
    base = ImageOps.grayscale(image)
    # Work from a card-sized image. Avoid OCR on 4K screenshots at full size.
    scale = min(3.0, 1900 / max(base.size))
    if scale > 1:
        base = base.resize((max(1, round(base.width*scale)), max(1, round(base.height*scale))), Image.Resampling.LANCZOS)
    for angle in (0, 90, 180, 270):
        rotated = base.rotate(angle, expand=True)
        gray = ImageOps.autocontrast(rotated)
        bright = ImageEnhance.Contrast(gray).enhance(1.7)
        for processed in (gray, bright):
            for psm in (6, 11, 3):
                # OCR the entire card, not just one line.
                raw = pytesseract.image_to_string(processed, lang='eng', config=f'--oem 3 --psm {psm}')
                for candidate in id_candidates(raw):
                    counts[candidate] += 1
            # A whole-card psm may miss the small 13-digit ID line; detect
            # promising horizontal text lines using TSV bounding boxes.
        if counts and counts.most_common(1)[0][1] >= 3:
            break
    if not counts:
        return ('', 'unreadable', 'ไม่พบเลข 13 หลักในภาพ ลองครอบให้เห็นบัตรชัด ๆ หรือขยายภาพก่อน')
    valid = [(num, c) for num, c in counts.most_common() if checksum_ok(num)]
    if not valid:
        number, n = counts.most_common(1)[0]
        return (number, 'uncertain', 'อ่านได้ 13 หลัก แต่เลขไม่ผ่านการตรวจสอบหลักตรวจสอบ')
    valid.sort(key=lambda x: x[1], reverse=True)
    number, hits = valid[0]
    runner_up = valid[1][1] if len(valid) > 1 else 0
    if hits < 2 or hits <= runner_up:
        return (number, 'uncertain', 'อ่านได้ แต่หลักฐานจาก OCR ยังไม่เพียงพอให้ยืนยัน')
    return (number, 'read', f'ตรวจพบเลข 13 หลักและผ่านการตรวจหลักตรวจสอบ ({hits} ครั้ง)')

def get_monitors():
    """Get real desktop monitor rectangles, including screens with negative x/y."""
    if os.name != 'nt':
        return [(0, 0, ImageGrab.grab().width, ImageGrab.grab().height)]
    from ctypes import wintypes
    monitors = []
    class RECT(ctypes.Structure):
        _fields_ = [('left', wintypes.LONG), ('top', wintypes.LONG), ('right', wintypes.LONG), ('bottom', wintypes.LONG)]
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
    def collect(_monitor, _hdc, rect, _data):
        r = rect.contents
        monitors.append((r.left, r.top, r.right, r.bottom))
        return True
    callback = PROC(collect)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0)
    return monitors


class NumberCheck:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Number Check — ตรวจเลขบัตรประชาชน')
        self.root.geometry('540x455')
        self.root.configure(bg='#f4f7fb')
        self.events = queue.Queue()
        self.overlays = []
        self.capture_active = False
        self.clip_value = ''
        self.original = tk.StringVar(value='—')
        self.ocr = tk.StringVar(value='—')
        self.result = tk.StringVar(value='พร้อมตรวจสอบ')
        self.note = tk.StringVar(value='คัดลอกเลขต้นฉบับ แล้วเลือกพื้นที่บนจอใดก็ได้')
        self.make_ui()
        # Native Windows RegisterHotKey works while Chrome or other windows have focus.
        self.hotkeys = WindowsHotkeys()
        active = list(self.hotkeys.active.values())
        if active:
            self.note.set('คีย์ลัดพร้อมใช้: ' + ' และ '.join(active) + ' — คัดลอกเลขก่อนเริ่ม')
        if self.hotkeys.failed:
            self.root.after(350, self.warn_hotkeys)
        self.root.after(70, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def warn_hotkeys(self):
        messagebox.showwarning('คีย์ลัดบางปุ่มใช้งานไม่ได้',
                               'โปรแกรมเปิดได้ แต่ Windows ไม่อนุญาตคีย์ลัดดังนี้:\n'
                               + '\n'.join(self.hotkeys.failed)
                               + '\n\nลองปิดโปรแกรมอื่นที่ใช้ปุ่มนี้ หรือคลิกปุ่มตรวจสอบใน Number Check')

    def make_ui(self):
        tk.Label(self.root, text='NUMBER CHECK', font=('Segoe UI', 21, 'bold'), fg='#12477b', bg='#f4f7fb').pack(pady=(15, 3))
        tk.Label(self.root, text='ตรวจเลขบัตร 13 หลักจากภาพทั้งใบ • หมุนได้ • รองรับ 2 จอ', font=('Segoe UI', 11), bg='#f4f7fb').pack()
        panel = tk.Frame(self.root, bg='white', padx=18, pady=14)
        panel.pack(fill='x', padx=20, pady=15)
        for label, variable in [('ตัวเลขจากคลิปบอร์ด', self.original), ('ตัวเลข OCR จากภาพ', self.ocr)]:
            tk.Label(panel, text=label, font=('Segoe UI', 10), bg='white', fg='#586779', anchor='w').pack(fill='x')
            tk.Label(panel, textvariable=variable, font=('Consolas', 17, 'bold'), bg='white', fg='#152b45', anchor='w').pack(fill='x', pady=(0, 9))
        self.status_label = tk.Label(self.root, textvariable=self.result, font=('Segoe UI', 17, 'bold'), bg='#f4f7fb', fg='#12477b')
        self.status_label.pack(pady=(1, 5))
        tk.Label(self.root, textvariable=self.note, font=('Segoe UI', 9), wraplength=460, bg='#f4f7fb', fg='#586779').pack(pady=3)
        ttk.Button(self.root, text='ครอบภาพบัตรเพื่อเทียบเลข  (F8 หรือ Ctrl + Alt + Q)', command=self.begin_capture).pack(pady=12, ipadx=9, ipady=6)
        tk.Label(self.root, text='ครอบบัตรทั้งใบได้  |  F8 ใช้ได้ขณะอยู่หน้าต่างอื่น  |  Esc ยกเลิก', font=('Segoe UI', 9), bg='#f4f7fb', fg='#687989').pack()

    def begin_capture(self):
        if self.capture_active:
            return
        try:
            self.clip_value = self.root.clipboard_get().strip()
        except tk.TclError:
            messagebox.showwarning('ยังไม่ได้คัดลอก', 'กรุณาคัดลอกตัวเลขต้นฉบับก่อน')
            return
        if not re.fullmatch(r'\d{13}', re.sub(r'[ -]', '', self.clip_value)):
            messagebox.showwarning('ตัวเลขไม่ถูกต้อง', 'กรุณาคัดลอกเลขบัตรประชาชน 13 หลักก่อน')
            return
        self.clip_value = re.sub(r'[ -]', '', self.clip_value)
        self.original.set(self.clip_value)
        self.ocr.set('—')
        try:
            # Grab each monitor BEFORE creating overlays, to avoid including the overlay itself.
            screens = [(rect, ImageGrab.grab(bbox=rect, all_screens=True)) for rect in get_monitors()]
            self.capture_active = True
            for index, (rect, shot) in enumerate(screens):
                left, top, right, bottom = rect
                window = tk.Toplevel(self.root)
                window.overrideredirect(True)
                window.attributes('-topmost', True)
                # Explicit signed geometry works for a second monitor at negative coordinates.
                xpos = f'+{left}' if left >= 0 else str(left)
                ypos = f'+{top}' if top >= 0 else str(top)
                window.geometry(f'{right-left}x{bottom-top}{xpos}{ypos}')
                canvas = tk.Canvas(window, highlightthickness=0, cursor='crosshair', bd=0)
                canvas.pack(fill='both', expand=True)
                photo = ImageTk.PhotoImage(shot, master=window)
                canvas.create_image(0, 0, image=photo, anchor='nw')
                canvas.create_rectangle(5, 5, 650, 52, fill='#102b49', outline='')
                canvas.create_text(17, 16, anchor='nw', text=f'จอที่ {index+1}: ลากครอบตัวเลข  |  Esc ยกเลิก', fill='white', font=('Segoe UI', 15, 'bold'))
                state = {'window': window, 'canvas': canvas, 'photo': photo, 'shot': shot, 'start': None, 'rect': None}
                self.overlays.append(state)
                canvas.bind('<ButtonPress-1>', lambda e, s=state: self.down(e, s))
                canvas.bind('<B1-Motion>', lambda e, s=state: self.drag(e, s))
                canvas.bind('<ButtonRelease-1>', lambda e, s=state: self.up(e, s))
                window.bind('<Escape>', lambda e: self.cancel())
            if self.overlays:
                self.overlays[0]['window'].focus_force()
        except Exception as exc:
            self.end_capture()
            messagebox.showerror('จับภาพหน้าจอไม่ได้', str(exc))

    def down(self, event, state):
        state['start'] = (event.x, event.y)
        if state['rect'] is not None:
            state['canvas'].delete(state['rect'])
        state['rect'] = state['canvas'].create_rectangle(event.x, event.y, event.x, event.y, outline='#ffe27a', width=3)

    def drag(self, event, state):
        if state['start'] is not None:
            state['canvas'].coords(state['rect'], *state['start'], event.x, event.y)

    def up(self, event, state):
        if state['start'] is None:
            return
        x0, y0 = state['start']
        x1, y1 = event.x, event.y
        x0, x1 = max(0, min(x0, x1)), min(state['shot'].width, max(x0, x1))
        y0, y1 = max(0, min(y0, y1)), min(state['shot'].height, max(y0, y1))
        chosen = state['shot'].crop((x0, y0, x1, y1)) if x1 - x0 >= 8 and y1 - y0 >= 8 else None
        self.end_capture()
        if chosen is not None:
            self.result.set('กำลังค้นหาเลขบัตรจากภาพทั้งใบ...')
            self.status_label.configure(fg='#12477b')
            self.note.set('กำลังอ่านภาพ โปรดรอสักครู่')
            threading.Thread(target=self.worker, args=(chosen, self.clip_value), daemon=True).start()

    def worker(self, image, original):
        try:
            self.events.put(('result', *ocr_number(image), original))
        except Exception as exc:
            self.events.put(('error', str(exc)))

    def end_capture(self):
        for state in self.overlays:
            try:
                state['window'].destroy()
            except tk.TclError:
                pass
        self.overlays = []
        self.capture_active = False
        self.root.lift()

    def cancel(self):
        self.end_capture()
        self.note.set('ยกเลิกแล้ว — เริ่มใหม่ได้')

    def poll(self):
        # Poll the native Windows message queue on Tk's own UI thread.
        for _hotkey_name in self.hotkeys.pop_hotkeys():
            self.events.put(('capture',))
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] == 'capture':
                    self.begin_capture()
                elif item[0] == 'error':
                    self.result.set('อ่านภาพไม่สำเร็จ')
                    self.status_label.configure(fg='#bb3333')
                    self.note.set(item[1][:190])
                elif item[0] == 'result':
                    _, value, quality, reason, original = item
                    self.ocr.set(value or '(ไม่พบเลข 13 หลัก)')
                    if quality != 'read':
                        self.result.set('⚠ ยังยืนยันเลขจากภาพไม่ได้')
                        self.status_label.configure(fg='#b66b00')
                        self.note.set(reason)
                    elif value == original:
                        self.result.set('✓ เลขบัตรตรงกัน 13 หลัก')
                        self.status_label.configure(fg='#13805e')
                        self.note.set(reason + ' — กรุณาดูภาพยืนยันอีกครั้ง')
                    else:
                        self.result.set('✕ เลขบัตรไม่ตรงกัน')
                        self.status_label.configure(fg='#c83737')
                        self.note.set(reason + ' — กรุณาตรวจเลขบนบัตรอีกครั้ง')
        except queue.Empty:
            pass
        self.root.after(70, self.poll)

    def close(self):
        self.end_capture()
        try:
            self.hotkeys.close()
        except Exception:
            pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == '__main__':
    try:
        NumberCheck().run()
    except Exception:
        with open(os.path.join(os.environ.get('LOCALAPPDATA', base_dir), 'NumberCheck_error.txt'), 'a', encoding='utf-8') as f:
            f.write(traceback.format_exc() + '\n')
        raise

