#!/usr/bin/env python3
import cv2
import numpy as np
import sys
import os
import time
import threading
import queue
from datetime import datetime
import tkinter as tk
from tkinter import filedialog
import gzip
import struct

# ---------- Audio (ffpyplayer) detection ----------

AUDIO_AVAILABLE = False
try:
    from ffpyplayer.player import MediaPlayer
    AUDIO_AVAILABLE = True
except ImportError:
    AUDIO_AVAILABLE = False

# ---------- System volume / headphone detection (Windows, optional) ----------

AUDIO_SYS_AVAILABLE = False
try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL
    AUDIO_SYS_AVAILABLE = True
except ImportError:
    AUDIO_SYS_AVAILABLE = False

def get_system_volume():
    if not AUDIO_SYS_AVAILABLE:
        return 0.2
    try:
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = interface.QueryInterface(IAudioEndpointVolume)
        return float(volume.GetMasterVolumeLevelScalar())
    except Exception:
        return 0.2

def detect_headphones():
    if not AUDIO_SYS_AVAILABLE:
        return False
    try:
        devices = AudioUtilities.GetSpeakers()
        name = devices.FriendlyName.lower()
        return ("headphone" in name) or ("headset" in name)
    except Exception:
        return False

# ---------- Keyboard (media controls) ----------

MSVCRT_AVAILABLE = False
try:
    import msvcrt
    MSVCRT_AVAILABLE = True
except ImportError:
    MSVCRT_AVAILABLE = False

def get_key_nonblocking():
    if not MSVCRT_AVAILABLE:
        return None
    if msvcrt.kbhit():
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            if ch2 == b'K':
                return 'LEFT'
            if ch2 == b'M':
                return 'RIGHT'
            return None
        try:
            return ch.decode('utf-8', errors='ignore')
        except Exception:
            return None
    return None

# ---------- Terminal helpers ----------

def supports_ansi():
    return sys.stdout.isatty()

def detect_redraw_mode():
    if supports_ansi():
        return "cursor"
    return "clear"

def clear_screen():
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()

def move_home():
    sys.stdout.write("\x1b[H")
    sys.stdout.flush()

def hide_cursor():
    if supports_ansi():
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()

def show_cursor():
    if supports_ansi():
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()

def get_terminal_size():
    try:
        import shutil
        size = shutil.get_terminal_size(fallback=(80, 24))
        return size.columns, size.lines
    except Exception:
        return 80, 24

# ---------- File picker ----------

def pick_video_file():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    file_path = filedialog.askopenfilename(
        title="Select a video file",
        filetypes=[
            ("Video files", "*.mp4 *.avi *.mov *.mkv *.webm"),
            ("All files", "*.*")
        ]
    )
    root.destroy()
    return file_path

def pick_ascii_video_file():
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    file_path = filedialog.askopenfilename(
        title="Select an ASCII video file",
        filetypes=[
            ("ASCII video", "*.asciiv *.asciivz *.asciibin *.asciih"),
            ("All files", "*.*")
        ]
    )
    root.destroy()
    return file_path

# ---------- ASCII rendering ----------

SHADE_CHARS = " .:-=+*#%@"

def angle_to_char(angle_deg):
    a = angle_deg % 180.0
    if a < 22.5 or a >= 157.5:
        return '-'
    elif a < 67.5:
        return '/'
    elif a < 112.5:
        return '|'
    else:
        return '\\'

def color_256(gray):
    idx = int((gray / 255.0) * 23)
    return 232 + max(0, min(23, idx))

def color_truecolor(gray):
    g = int(gray)
    return g, g, g

def frame_to_ascii(frame, width, height, color_mode="none", include_ansi=True):
    h_internal = height * 2
    w_internal = width

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray_small = cv2.resize(gray, (w_internal, h_internal), interpolation=cv2.INTER_AREA)

    edges = cv2.Canny(gray_small, 100, 200)
    gx = cv2.Sobel(gray_small, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_small, cv2.CV_32F, 0, 1, ksize=3)
    angles = cv2.phase(gx, gy, angleInDegrees=True)

    norm = gray_small.astype(np.float32) / 255.0

    lines = []
    use_color = color_mode in ("256", "truecolor") and include_ansi
    for y in range(0, h_internal, 2):
        row_chars = []
        for x in range(w_internal):
            top_edge = edges[y, x] > 0
            bottom_edge = edges[y + 1, x] > 0 if y + 1 < h_internal else False

            if top_edge or bottom_edge:
                ch = angle_to_char(angles[y, x])
                gray_val = gray_small[y, x]
            else:
                top_val = norm[y, x]
                bottom_val = norm[y + 1, x] if y + 1 < h_internal else top_val
                avg = (top_val + bottom_val) / 2.0
                idx = int(avg * (len(SHADE_CHARS) - 1))
                ch = SHADE_CHARS[max(0, min(len(SHADE_CHARS) - 1, idx))]
                gray_val = int(avg * 255)

            if use_color:
                if color_mode == "256":
                    cidx = color_256(gray_val)
                    row_chars.append(f"\x1b[38;5;{cidx}m{ch}\x1b[0m")
                elif color_mode == "truecolor":
                    r, g, b = color_truecolor(gray_val)
                    row_chars.append(f"\x1b[38;2;{r};{g};{b}m{ch}\x1b[0m")
            else:
                row_chars.append(ch)
        lines.append("".join(row_chars))
    return "\n".join(lines)

# ---------- ASCII video export formats ----------

def export_ascii_video_plain(path, fps, width, height, color_mode, ansi_included, frames):
    with open(path, "w", encoding="utf-8") as f:
        f.write("ASCIIV1\n")
        f.write(f"FPS:{fps}\n")
        f.write(f"WIDTH:{width}\n")
        f.write(f"HEIGHT:{height}\n")
        f.write(f"COLOR_MODE:{color_mode}\n")
        f.write(f"ANSI_INCLUDED:{1 if ansi_included else 0}\n")
        f.write(f"FRAMES:{len(frames)}\n")
        f.write("---\n")
        for frame in frames:
            f.write(frame)
            f.write("\n<<<END>>>\n")

def import_ascii_video_plain(path):
    with open(path, "r", encoding="utf-8") as f:
        header = f.readline().strip()
        if header != "ASCIIV1":
            raise ValueError("Not a valid .asciiv file")
        meta = {}
        while True:
            line = f.readline()
            if not line:
                raise ValueError("Invalid header")
            line = line.strip()
            if line == "---":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        fps = float(meta.get("FPS", "24"))
        width = int(meta.get("WIDTH", "80"))
        height = int(meta.get("HEIGHT", "24"))
        color_mode = meta.get("COLOR_MODE", "none")
        ansi_included = meta.get("ANSI_INCLUDED", "0") == "1"
        frames = []
        buf = []
        for line in f:
            if line.strip() == "<<<END>>>":
                frames.append("".join(buf).rstrip("\n"))
                buf = []
            else:
                buf.append(line)
        if buf:
            frames.append("".join(buf).rstrip("\n"))
    return fps, width, height, color_mode, ansi_included, frames

def export_ascii_video_compressed(path, fps, width, height, color_mode, ansi_included, frames):
    tmp = path + ".tmp_asciiv"
    export_ascii_video_plain(tmp, fps, width, height, color_mode, ansi_included, frames)
    with open(tmp, "rb") as f_in, gzip.open(path, "wb") as f_out:
        f_out.write(f_in.read())
    os.remove(tmp)

def import_ascii_video_compressed(path):
    tmp = path + ".tmp_asciiv"
    with gzip.open(path, "rb") as f_in, open(tmp, "wb") as f_out:
        f_out.write(f_in.read())
    data = import_ascii_video_plain(tmp)
    os.remove(tmp)
    return data

def color_mode_to_int(cm):
    if cm == "256":
        return 1
    if cm == "truecolor":
        return 2
    return 0

def int_to_color_mode(v):
    if v == 1:
        return "256"
    if v == 2:
        return "truecolor"
    return "none"

def export_ascii_video_binary(path, fps, width, height, color_mode, ansi_included, frames):
    with open(path, "wb") as f:
        f.write(b"ASCBIN1\0")
        cm_int = color_mode_to_int(color_mode)
        header = struct.pack("<fIIBBI", fps, width, height, cm_int, 1 if ansi_included else 0, len(frames))
        f.write(header)
        for frame in frames:
            data = frame.encode("utf-8")
            f.write(struct.pack("<I", len(data)))
            f.write(data)

def import_ascii_video_binary(path):
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != b"ASCBIN1\0":
            raise ValueError("Not a valid .asciibin file")
        fps, width, height, cm_int, ansi_flag, nframes = struct.unpack("<fIIBBI", f.read(4+4+4+1+1+4))
        color_mode = int_to_color_mode(cm_int)
        ansi_included = bool(ansi_flag)
        frames = []
        for _ in range(nframes):
            (length,) = struct.unpack("<I", f.read(4))
            data = f.read(length)
            frames.append(data.decode("utf-8"))
    return fps, width, height, color_mode, ansi_included, frames

def export_ascii_video_hybrid(path, fps, width, height, color_mode, ansi_included, frames):
    with open(path, "wb") as f:
        header = (
            "ASCIIH1\n"
            f"FPS:{fps}\n"
            f"WIDTH:{width}\n"
            f"HEIGHT:{height}\n"
            f"COLOR_MODE:{color_mode}\n"
            f"ANSI_INCLUDED:{1 if ansi_included else 0}\n"
            f"FRAMES:{len(frames)}\n"
            "---\n"
        )
        f.write(header.encode("utf-8"))
        joined = "\n<<<END>>>\n".join(frames)
        with gzip.GzipFile(fileobj=f, mode="wb") as gz:
            gz.write(joined.encode("utf-8"))

def import_ascii_video_hybrid(path):
    with open(path, "rb") as f:
        header_lines = []
        while True:
            line = f.readline()
            if not line:
                raise ValueError("Invalid .asciih header")
            if line.strip() == b"---":
                break
            header_lines.append(line.decode("utf-8").rstrip("\n"))
        if not header_lines or header_lines[0] != "ASCIIH1":
            raise ValueError("Not a valid .asciih file")
        meta = {}
        for line in header_lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        fps = float(meta.get("FPS", "24"))
        width = int(meta.get("WIDTH", "80"))
        height = int(meta.get("HEIGHT", "24"))
        color_mode = meta.get("COLOR_MODE", "none")
        ansi_included = meta.get("ANSI_INCLUDED", "0") == "1"
        with gzip.GzipFile(fileobj=f, mode="rb") as gz:
            data = gz.read().decode("utf-8")
        frames = data.split("\n<<<END>>>\n")
    return fps, width, height, color_mode, ansi_included, frames

def detect_ascii_video_format(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".asciiv":
        return "plain"
    if ext == ".asciivz":
        return "compressed"
    if ext == ".asciibin":
        return "binary"
    if ext == ".asciih":
        return "hybrid"
    with open(path, "rb") as f:
        sig = f.read(8)
    if sig.startswith(b"ASCBIN1"):
        return "binary"
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        first = f.readline().strip()
        if first == "ASCIIV1":
            return "plain"
        if first == "ASCIIH1":
            return "hybrid"
    return "plain"

def export_ascii_video(path, fmt, fps, width, height, color_mode, ansi_included, frames):
    if fmt == "plain":
        export_ascii_video_plain(path, fps, width, height, color_mode, ansi_included, frames)
    elif fmt == "compressed":
        export_ascii_video_compressed(path, fps, width, height, color_mode, ansi_included, frames)
    elif fmt == "binary":
        export_ascii_video_binary(path, fps, width, height, color_mode, ansi_included, frames)
    elif fmt == "hybrid":
        export_ascii_video_hybrid(path, fps, width, height, color_mode, ansi_included, frames)
    else:
        export_ascii_video_plain(path, fps, width, height, color_mode, ansi_included, frames)

def import_ascii_video(path):
    fmt = detect_ascii_video_format(path)
    if fmt == "plain":
        return import_ascii_video_plain(path)
    if fmt == "compressed":
        return import_ascii_video_compressed(path)
    if fmt == "binary":
        return import_ascii_video_binary(path)
    if fmt == "hybrid":
        return import_ascii_video_hybrid(path)
    return import_ascii_video_plain(path)

# ---------- Export helper ----------

def create_export_filename(fmt_ext):
    base = "ascii_export_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    return base + fmt_ext

# ---------- Video + audio threaded helpers ----------

def video_producer(cap, frame_queue, stop_event):
    while not stop_event.is_set():
        ret, frame = cap.read()
        if not ret:
            break
        frame_queue.put(frame)
    stop_event.set()

def audio_thread_func(path, control, stop_event):
    if not AUDIO_AVAILABLE:
        return
    player = MediaPlayer(path)
    last_volume = control["volume"]
    player.set_volume(last_volume)
    player.set_pause(False)
    while not stop_event.is_set():
        if control["seek_request"] is not None:
            try:
                player.seek(control["seek_request"], relative=True)
            except Exception:
                pass
            control["seek_request"] = None

        if control["paused"] != control["audio_paused"]:
            player.set_pause(control["paused"])
            control["audio_paused"] = control["paused"]

        if control["muted"]:
            if not control["was_muted"]:
                player.set_volume(0.0)
                control["was_muted"] = True
        else:
            if control["was_muted"] or control["volume"] != last_volume:
                last_volume = control["volume"]
                player.set_volume(last_volume)
                control["was_muted"] = False

        frame, val = player.get_frame()
        if val == 'eof':
            break
        time.sleep(0.01)
    player.close_player()

# ---------- HUD ----------

def print_hud(volume=None, locked=False, headphones=False):
    extra = ""
    if volume is not None:
        vol_pct = int(volume * 100)
        extra = f" | Vol:{vol_pct}%"
        if headphones:
            extra += " [HP CAP]"
        if locked:
            extra += " [LOCKED]"
    sys.stdout.write(f"\n[SPACE] Pause | [+/-] Volume | [M] Mute | [←/→] Seek | [,/.] Step | [[] Speed | [Q] Quit{extra}\n")
    sys.stdout.flush()

# ---------- Resolution selection ----------

def choose_resolution():
    print("\nChoose ASCII resolution:")
    print("1. Low (80)")
    print("2. Medium (120)")
    print("3. High (160)")
    print("4. Custom")
    choice = input("Enter choice (1-4): ").strip()
    if choice == "1":
        return 80, 40
    elif choice == "2":
        return 120, 40
    elif choice == "3":
        return 160, 40
    elif choice == "4":
        try:
            w = int(input("Enter width: ").strip())
            h = int(input("Enter height: ").strip())
            return max(20, w), max(10, h)
        except Exception:
            print("Invalid custom size, using Medium (120x40).")
            return 120, 40
    else:
        print("Invalid choice, using Medium (120x40).")
        return 120, 40

# ---------- Volume safety system ----------

SAFE_THRESHOLD = 0.05
MAX_HEADPHONE_VOLUME = 0.04
MAX_GENERAL_VOLUME = 0.2
VOLUME_STEP = 0.01

def handle_volume_up(control, paused):
    now = time.time()
    if now - control["last_press"] < 0.3:
        control["spam_count"] += 1
    else:
        control["spam_count"] = 1
    control["last_press"] = now

    if control["locked"]:
        return paused, False

    sys_vol = get_system_volume()
    new_vol = control["volume"] + VOLUME_STEP

    dangerous = False

    if control["headphones"] and new_vol > MAX_HEADPHONE_VOLUME:
        dangerous = True
    elif sys_vol > 0.25 and new_vol > SAFE_THRESHOLD:
        dangerous = True
    elif control["spam_count"] > 3:
        dangerous = True
    elif new_vol > MAX_GENERAL_VOLUME:
        dangerous = True

    if dangerous:
        control["locked"] = True
        control["paused_by_safety"] = True
        return True, True

    control["volume"] = min(new_vol, MAX_GENERAL_VOLUME)
    return paused, False

def handle_volume_down(control):
    control["locked"] = False
    control["spam_count"] = 0
    control["volume"] = max(0.0, control["volume"] - VOLUME_STEP)

# ---------- Modes ----------

def play_video(path, use_audio, use_export, export_fmt, export_color_mode_choice,
               color_mode, threaded, redraw_mode, width, height):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print("Failed to open video:", path)
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not (fps > 0):
        fps = 24.0
    base_frame_duration = 1.0 / fps

    export_frames = []
    ansi_included = True

    if use_export:
        print("\nChoose export format:")
        print("1. Plain text (.asciiv)")
        print("2. Compressed (.asciivz)")
        print("3. Binary (.asciibin)")
        print("4. Hybrid (.asciih)")
        choice = input("Enter choice (1-4): ").strip()
        if choice == "1":
            export_fmt = "plain"
            ext = ".asciiv"
        elif choice == "2":
            export_fmt = "compressed"
            ext = ".asciivz"
        elif choice == "3":
            export_fmt = "binary"
            ext = ".asciibin"
        elif choice == "4":
            export_fmt = "hybrid"
            ext = ".asciih"
        else:
            export_fmt = "plain"
            ext = ".asciiv"

        print("\nInclude color in export?")
        print("1. Yes (ANSI codes)")
        print("2. No (grayscale only)")
        print("3. Ask each time (per export run)")
        c2 = input("Enter choice (1-3): ").strip()
        if c2 == "1":
            ansi_included = True
        elif c2 == "2":
            ansi_included = False
        else:
            ansi_included = True

        export_path = create_export_filename(ext)
        print("Exporting ASCII video to:", export_path)
    else:
        export_fmt = None
        export_path = None

    audio_stop = threading.Event()
    audio_thread = None
    audio_control = {
        "volume": 0.03,
        "muted": False,
        "was_muted": False,
        "paused": False,
        "audio_paused": False,
        "seek_request": None,
        "locked": False,
        "spam_count": 0,
        "last_press": 0.0,
        "paused_by_safety": False,
        "headphones": detect_headphones()
    }
    if use_audio and AUDIO_AVAILABLE:
        audio_thread = threading.Thread(target=audio_thread_func, args=(path, audio_control, audio_stop), daemon=True)
        audio_thread.start()
    elif use_audio and not AUDIO_AVAILABLE:
        print("Audio mode selected, but ffpyplayer is not installed. Audio disabled.")

    paused = False
    speed_index = 3
    speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

    hide_cursor()
    try:
        if threaded:
            frame_queue = queue.Queue(maxsize=10)
            stop_event = threading.Event()
            prod_thread = threading.Thread(target=video_producer, args=(cap, frame_queue, stop_event), daemon=True)
            prod_thread.start()

            last_time = time.time()
            while not stop_event.is_set():
                key = get_key_nonblocking()
                if key:
                    if key == ' ':
                        if audio_control["paused_by_safety"]:
                            if not audio_control["locked"]:
                                paused = not paused
                                if not paused:
                                    audio_control["paused_by_safety"] = False
                        else:
                            paused = not paused
                        audio_control["paused"] = paused
                    elif key == '+':
                        if use_audio:
                            paused, triggered = handle_volume_up(audio_control, paused)
                            audio_control["paused"] = paused
                        else:
                            pass
                    elif key == '-':
                        if use_audio:
                            handle_volume_down(audio_control)
                        else:
                            pass
                    elif key in ('m', 'M'):
                        audio_control["muted"] = not audio_control["muted"]
                    elif key == 'LEFT':
                        audio_control["seek_request"] = -5.0
                    elif key == 'RIGHT':
                        audio_control["seek_request"] = 5.0
                    elif key == '[':
                        if speed_index > 0:
                            speed_index -= 1
                    elif key == ']':
                        if speed_index < len(speeds) - 1:
                            speed_index += 1
                    elif key in (',', '.'):
                        paused = True
                        audio_control["paused"] = True
                    elif key in ('q', 'Q'):
                        stop_event.set()
                        break

                if paused:
                    if redraw_mode == "clear":
                        clear_screen()
                    else:
                        move_home()
                    if audio_control["paused_by_safety"]:
                        sys.stdout.write("[PAUSED - VOLUME SAFETY]\n")
                        sys.stdout.write("Volume increase blocked for your safety.\nPress '-' to lower volume, then SPACE to resume.\n")
                    else:
                        sys.stdout.write("[PAUSED]\n")
                    print_hud(audio_control["volume"], audio_control["locked"], audio_control["headphones"])
                    sys.stdout.flush()
                    time.sleep(0.05)
                    continue

                try:
                    frame = frame_queue.get(timeout=0.5)
                except queue.Empty:
                    break

                ascii_frame = frame_to_ascii(frame, width, height, color_mode, include_ansi=ansi_included)
                if redraw_mode == "clear":
                    clear_screen()
                else:
                    move_home()
                sys.stdout.write(ascii_frame + "\n")
                print_hud(audio_control["volume"] if use_audio else None,
                          audio_control["locked"] if use_audio else False,
                          audio_control["headphones"] if use_audio else False)
                sys.stdout.flush()

                if use_export:
                    export_frames.append(ascii_frame)

                now = time.time()
                elapsed = now - last_time
                frame_duration = base_frame_duration / speeds[speed_index]
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                last_time = time.time()

            stop_event.set()
            prod_thread.join(timeout=1.0)
        else:
            last_time = time.time()
            while True:
                key = get_key_nonblocking()
                if key:
                    if key == ' ':
                        if audio_control["paused_by_safety"]:
                            if not audio_control["locked"]:
                                paused = not paused
                                if not paused:
                                    audio_control["paused_by_safety"] = False
                        else:
                            paused = not paused
                        audio_control["paused"] = paused
                    elif key == '+':
                        if use_audio:
                            paused, triggered = handle_volume_up(audio_control, paused)
                            audio_control["paused"] = paused
                        else:
                            pass
                    elif key == '-':
                        if use_audio:
                            handle_volume_down(audio_control)
                        else:
                            pass
                    elif key in ('m', 'M'):
                        audio_control["muted"] = not audio_control["muted"]
                    elif key == 'LEFT':
                        audio_control["seek_request"] = -5.0
                    elif key == 'RIGHT':
                        audio_control["seek_request"] = 5.0
                    elif key == '[':
                        if speed_index > 0:
                            speed_index -= 1
                    elif key == ']':
                        if speed_index < len(speeds) - 1:
                            speed_index += 1
                    elif key in (',', '.'):
                        paused = True
                        audio_control["paused"] = True
                    elif key in ('q', 'Q'):
                        break

                if paused:
                    if redraw_mode == "clear":
                        clear_screen()
                    else:
                        move_home()
                    if audio_control["paused_by_safety"]:
                        sys.stdout.write("[PAUSED - VOLUME SAFETY]\n")
                        sys.stdout.write("Volume increase blocked for your safety.\nPress '-' to lower volume, then SPACE to resume.\n")
                    else:
                        sys.stdout.write("[PAUSED]\n")
                    print_hud(audio_control["volume"] if use_audio else None,
                              audio_control["locked"] if use_audio else False,
                              audio_control["headphones"] if use_audio else False)
                    sys.stdout.flush()
                    time.sleep(0.05)
                    continue

                ret, frame = cap.read()
                if not ret:
                    break

                ascii_frame = frame_to_ascii(frame, width, height, color_mode, include_ansi=ansi_included)
                if redraw_mode == "clear":
                    clear_screen()
                else:
                    move_home()
                sys.stdout.write(ascii_frame + "\n")
                print_hud(audio_control["volume"] if use_audio else None,
                          audio_control["locked"] if use_audio else False,
                          audio_control["headphones"] if use_audio else False)
                sys.stdout.flush()

                if use_export:
                    export_frames.append(ascii_frame)

                now = time.time()
                elapsed = now - last_time
                frame_duration = base_frame_duration / speeds[speed_index]
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                last_time = time.time()
    finally:
        show_cursor()
        cap.release()
        if audio_thread is not None:
            audio_stop.set()
            audio_thread.join(timeout=1.0)

    if use_export and export_path is not None:
        export_ascii_video(export_path, export_fmt, fps, width, height, color_mode, ansi_included, export_frames)
        print("Export complete:", export_path)

def play_webcam(use_export, export_fmt, export_color_mode_choice,
                color_mode, threaded, redraw_mode, width, height, cam_index=0):
    cap = cv2.VideoCapture(cam_index)
    if not cap.isOpened():
        print("Failed to open webcam.")
        return

    fps = cap.get(cv2.CAP_PROP_FPS)
    if not (fps > 0):
        fps = 24.0
    base_frame_duration = 1.0 / fps

    export_frames = []
    ansi_included = True

    if use_export:
        print("\nChoose export format:")
        print("1. Plain text (.asciiv)")
        print("2. Compressed (.asciivz)")
        print("3. Binary (.asciibin)")
        print("4. Hybrid (.asciih)")
        choice = input("Enter choice (1-4): ").strip()
        if choice == "1":
            export_fmt = "plain"
            ext = ".asciiv"
        elif choice == "2":
            export_fmt = "compressed"
            ext = ".asciivz"
        elif choice == "3":
            export_fmt = "binary"
            ext = ".asciibin"
        elif choice == "4":
            export_fmt = "hybrid"
            ext = ".asciih"
        else:
            export_fmt = "plain"
            ext = ".asciiv"

        print("\nInclude color in export?")
        print("1. Yes (ANSI codes)")
        print("2. No (grayscale only)")
        print("3. Ask each time (per export run)")
        c2 = input("Enter choice (1-3): ").strip()
        if c2 == "1":
            ansi_included = True
        elif c2 == "2":
            ansi_included = False
        else:
            ansi_included = True

        export_path = create_export_filename(ext)
        print("Exporting ASCII webcam video to:", export_path)
    else:
        export_fmt = None
        export_path = None

    paused = False
    speed_index = 3
    speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]

    hide_cursor()
    try:
        if threaded:
            frame_queue = queue.Queue(maxsize=10)
            stop_event = threading.Event()
            prod_thread = threading.Thread(target=video_producer, args=(cap, frame_queue, stop_event), daemon=True)
            prod_thread.start()

            last_time = time.time()
            while not stop_event.is_set():
                key = get_key_nonblocking()
                if key:
                    if key == ' ':
                        paused = not paused
                    elif key == '[':
                        if speed_index > 0:
                            speed_index -= 1
                    elif key == ']':
                        if speed_index < len(speeds) - 1:
                            speed_index += 1
                    elif key in (',', '.'):
                        paused = True
                    elif key in ('q', 'Q'):
                        stop_event.set()
                        break

                if paused:
                    if redraw_mode == "clear":
                        clear_screen()
                    else:
                        move_home()
                    sys.stdout.write("[PAUSED]\n")
                    print_hud()
                    sys.stdout.flush()
                    time.sleep(0.05)
                    continue

                try:
                    frame = frame_queue.get(timeout=0.5)
                except queue.Empty:
                    break

                ascii_frame = frame_to_ascii(frame, width, height, color_mode, include_ansi=ansi_included)
                if redraw_mode == "clear":
                    clear_screen()
                else:
                    move_home()
                sys.stdout.write(ascii_frame + "\n")
                print_hud()
                sys.stdout.flush()

                if use_export:
                    export_frames.append(ascii_frame)

                now = time.time()
                elapsed = now - last_time
                frame_duration = base_frame_duration / speeds[speed_index]
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                last_time = time.time()

            stop_event.set()
            prod_thread.join(timeout=1.0)
        else:
            last_time = time.time()
            while True:
                key = get_key_nonblocking()
                if key:
                    if key == ' ':
                        paused = not paused
                    elif key == '[':
                        if speed_index > 0:
                            speed_index -= 1
                    elif key == ']':
                        if speed_index < len(speeds) - 1:
                            speed_index += 1
                    elif key in (',', '.'):
                        paused = True
                    elif key in ('q', 'Q'):
                        break

                if paused:
                    if redraw_mode == "clear":
                        clear_screen()
                    else:
                        move_home()
                    sys.stdout.write("[PAUSED]\n")
                    print_hud()
                    sys.stdout.flush()
                    time.sleep(0.05)
                    continue

                ret, frame = cap.read()
                if not ret:
                    break

                ascii_frame = frame_to_ascii(frame, width, height, color_mode, include_ansi=ansi_included)
                if redraw_mode == "clear":
                    clear_screen()
                else:
                    move_home()
                sys.stdout.write(ascii_frame + "\n")
                print_hud()
                sys.stdout.flush()

                if use_export:
                    export_frames.append(ascii_frame)

                now = time.time()
                elapsed = now - last_time
                frame_duration = base_frame_duration / speeds[speed_index]
                sleep_time = frame_duration - elapsed
                if sleep_time > 0:
                    time.sleep(sleep_time)
                last_time = time.time()
    finally:
        show_cursor()
        cap.release()

    if use_export and export_path is not None:
        export_ascii_video(export_path, export_fmt, fps, width, height, color_mode, ansi_included, export_frames)
        print("Export complete:", export_path)

def play_ascii_video_file():
    path = pick_ascii_video_file()
    if not path:
        print("No file selected.")
        return
    try:
        fps, width, height, color_mode, ansi_included, frames = import_ascii_video(path)
    except Exception as e:
        print("Failed to load ASCII video:", e)
        return

    redraw_mode = detect_redraw_mode()
    hide_cursor()
    try:
        frame_duration = 1.0 / fps if fps > 0 else 1.0 / 24.0
        last_time = time.time()
        paused = False
        speed_index = 3
        speeds = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]
        idx = 0
        n = len(frames)
        while idx < n:
            key = get_key_nonblocking()
            if key:
                if key == ' ':
                    paused = not paused
                elif key == '[':
                    if speed_index > 0:
                        speed_index -= 1
                elif key == ']':
                    if speed_index < len(speeds) - 1:
                        speed_index += 1
                elif key == 'LEFT':
                    idx = max(0, idx - int(fps * 5))
                elif key == 'RIGHT':
                    idx = min(n - 1, idx + int(fps * 5))
                elif key == ',':
                    paused = True
                    idx = max(0, idx - 1)
                elif key == '.':
                    paused = True
                    idx = min(n - 1, idx + 1)
                elif key in ('q', 'Q'):
                    break

            if paused:
                if redraw_mode == "clear":
                    clear_screen()
                else:
                    move_home()
                sys.stdout.write("[PAUSED]\n")
                print_hud()
                sys.stdout.flush()
                time.sleep(0.05)
                continue

            ascii_frame = frames[idx]
            if redraw_mode == "clear":
                clear_screen()
            else:
                move_home()
            sys.stdout.write(ascii_frame + "\n")
            print_hud()
            sys.stdout.flush()

            idx += 1
            now = time.time()
            elapsed = now - last_time
            frame_duration = (1.0 / fps) / speeds[speed_index]
            sleep_time = frame_duration - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
            last_time = time.time()
    finally:
        show_cursor()

# ---------- Interactive tutorial ----------

def interactive_tutorial():
    clear_screen()
    print("=== INTERACTIVE TUTORIAL ===")
    print("This will walk you through how to use the renderer.")
    input("Press ENTER to begin...")

    clear_screen()
    print("Step 1: Mode Selection")
    print("----------------------")
    print("In the real program, you choose modes like:")
    print("1. Video, 2. Video+Audio, 3. Webcam, 4. Export, 5. Color, 6. Multithread, 7. ALL")
    print("Try typing a mode selection now, e.g. '1,4,5':")
    modes_str = input("Enter modes: ").strip()
    print(f"You entered: {modes_str}")
    input("Nice. That's how you select modes. Press ENTER to continue...")

    def parse_modes_input_tut(s):
        parts = [p.strip() for p in s.split(",") if p.strip()]
        modes = set()
        for p in parts:
            if p.isdigit():
                n = int(p)
                if 1 <= n <= 7:
                    modes.add(n)
        if 7 in modes:
            modes = {1, 2, 3, 4, 5, 6}
        return modes

    modes = parse_modes_input_tut(modes_str)
    video = 1 in modes or 2 in modes
    audio = 2 in modes
    webcam = 3 in modes
    export = 4 in modes
    color = 5 in modes

    if video:
        clear_screen()
        print("Step 2: File Picker (Video)")
        print("---------------------------")
        print("When you enable video, a file picker opens so you can choose a video file.")
        print("Example:")
        print("  [File Picker]")
        print("  demo_video.mp4")
        print("  bad_apple.mkv")
        print("  cancel")
        input("In the real program, you'd click a file. Press ENTER to continue...")

    if color:
        clear_screen()
        print("Step 3: Color Mode")
        print("------------------")
        print("You can choose how color is handled in ASCII:")
        print("1. 256-color")
        print("2. TrueColor (24-bit)")
        print("3. Auto")
        print("4. None")
        c = input("Pick one now (1-4): ").strip()
        print(f"You chose: {c or 'default'}")
        input("In the real program, this affects how the ASCII is colored. Press ENTER...")

    if export:
        clear_screen()
        print("Step 4: Export Formats")
        print("----------------------")
        print("When export is enabled, you can choose a format:")
        print("1. Plain text (.asciiv)")
        print("2. Compressed (.asciivz)")
        print("3. Binary (.asciibin)")
        print("4. Hybrid (.asciih)")
        ef = input("Pick one now (1-4): ").strip()
        print(f"You chose: {ef or 'default'}")
        print("\nThen you choose whether to include color in the export:")
        print("1. Include ANSI color")
        print("2. Grayscale only")
        print("3. Ask each time")
        ec = input("Pick one now (1-3): ").strip()
        print(f"You chose: {ec or 'default'}")
        input("That's how export works. Press ENTER to continue...")

    if webcam:
        clear_screen()
        print("Step 5: Webcam Mode")
        print("-------------------")
        print("If you enable webcam mode, the renderer reads frames from your camera")
        print("and converts them to ASCII in real time.")
        input("Press ENTER to continue...")

    clear_screen()
    print("Step 6: Playback")
    print("----------------")
    print("During playback, frames are drawn in the terminal at the correct FPS.")
    print("Here's a tiny fake ASCII animation:")
    frames = [
        "@@      ",
        " @@     ",
        "  @@    ",
        "   @@   ",
        "    @@  ",
        "     @@ ",
        "      @@",
    ]
    input("Press ENTER to play the mini animation...")
    hide_cursor()
    try:
        for ftxt in frames:
            clear_screen()
            print(ftxt)
            time.sleep(0.1)
    finally:
        show_cursor()

    clear_screen()
    print("Tutorial complete!")
    print("You now know the flow:")
    print("- Select modes")
    print("- (Optional) Pick a video")
    print("- (Optional) Choose color")
    print("- (Optional) Choose export format")
    print("- Start playback")
    print("Press ENTER to return to the main menu.")
    input()

# ---------- Menu and mode handling ----------

def parse_modes_input(s):
    parts = [p.strip() for p in s.split(",") if p.strip()]
    modes = set()
    for p in parts:
        if p.isdigit():
            n = int(p)
            if 1 <= n <= 7:
                modes.add(n)
    return modes

def choose_color_mode():
    print("\nChoose color mode:")
    print("1. 256-color ANSI")
    print("2. TrueColor (24-bit)")
    print("3. Auto-detect")
    print("4. No color")
    choice = input("Enter choice (1-4): ").strip()
    if choice == "1":
        return "256"
    elif choice == "2":
        return "truecolor"
    elif choice == "3":
        return "truecolor" if supports_ansi() else "256"
    elif choice == "4":
        return "none"
    else:
        return "none"

def resolve_conflicts(modes):
    if 7 in modes:
        modes = {1, 2, 3, 4, 5, 6}

    video = 1 in modes or 2 in modes
    audio = 2 in modes
    webcam = 3 in modes
    threaded = 6 in modes

    if audio and not video:
        print("\nConflict: Audio mode requires video playback.")
        while True:
            ans = input("Remove audio mode (2)? (y/n): ").strip().lower()
            if ans == "y":
                modes.discard(2)
                audio = False
                break
            elif ans == "n":
                ans2 = input("Enable video mode (1)? (y/n): ").strip().lower()
                if ans2 == "y":
                    modes.add(1)
                    video = True
                    break
                else:
                    print("Cannot keep audio without video. Removing audio.")
                    modes.discard(2)
                    audio = False
                    break

    video = 1 in modes or 2 in modes
    audio = 2 in modes
    webcam = 3 in modes
    threaded = 6 in modes

    if video and webcam and not threaded:
        print("\nConflict: Video (1/2) and Webcam (3) cannot run together without multithreading (6).")
        ans = input("Enable multithreaded mode (6)? (y/n): ").strip().lower()
        if ans == "y":
            modes.add(6)
            threaded = True
        else:
            print("Which mode do you want to disable?")
            print("1. Video (1/2)")
            print("2. Webcam (3)")
            while True:
                c = input("Enter 1 or 2: ").strip()
                if c == "1":
                    modes.discard(1)
                    modes.discard(2)
                    video = False
                    break
                elif c == "2":
                    modes.discard(3)
                    webcam = False
                    break

    video = 1 in modes or 2 in modes
    audio = 2 in modes
    webcam = 3 in modes

    if webcam and audio:
        print("\nConflict: Webcam (3) does not support audio (2).")
        print("Which mode do you want to disable?")
        print("1. Audio (2)")
        print("2. Webcam (3)")
        while True:
            c = input("Enter 1 or 2: ").strip()
            if c == "1":
                modes.discard(2)
                audio = False
                break
            elif c == "2":
                modes.discard(3)
                webcam = False
                break

    return modes

def print_quick_guide():
    print("ASCII Renderer — Quick Guide")
    print("----------------------------")
    print("1. Choose modes (video, audio, webcam, export, color, etc.).")
    print("2. If video is enabled, a file picker opens for your video.")
    print("3. If export is enabled, you'll choose export format and color handling.")
    print("4. Playback starts automatically.")
    print("5. Press CTRL+C to stop.")
    print("Press H at the main menu for an interactive tutorial.\n")

def main_menu():
    print("Select modes (comma separated) or press H for tutorial:")
    print("1. Video (ASCII)")
    print("2. Video + Audio")
    print("3. Webcam (ASCII)")
    print("4. Export ASCII video")
    print("5. Color ASCII Mode")
    print("6. Multithreaded (smooth)")
    print("7. ALL MODES")
    print("8. Play ASCII video file (.asciiv / .asciivz / .asciibin / .asciih)")
    print("H. Interactive Tutorial")
    return input("Enter modes or option: ").strip()

def main_loop():
    while True:
        clear_screen()
        print_quick_guide()

        choice = main_menu()
        if choice.lower() == "h":
            interactive_tutorial()
            continue

        if choice == "8":
            play_ascii_video_file()
            print("\nPlayback finished. Returning to main menu...")
            time.sleep(1)
            continue

        modes = parse_modes_input(choice)
        if not modes:
            print("No valid modes selected. Exiting.")
            return

        modes = resolve_conflicts(modes)

        video = 1 in modes or 2 in modes
        audio = 2 in modes
        webcam = 3 in modes
        export = 4 in modes
        color_selected = 5 in modes
        threaded = 6 in modes

        if color_selected:
            color_mode = choose_color_mode()
        else:
            color_mode = "none"

        redraw_mode = detect_redraw_mode()

        width, height = choose_resolution()

        video_path = None
        if video:
            print("Opening file picker for video...")
            video_path = pick_video_file()
            if not video_path:
                print("No file selected. Disabling video/audio modes.")
                modes.discard(1)
                modes.discard(2)
                video = False
                audio = False

        print("\nFinal modes:")
        if video:
            print("- Video (ASCII)")
        if audio:
            print("- Video + Audio (ffpyplayer)" + ("" if AUDIO_AVAILABLE else " [UNAVAILABLE: ffpyplayer not installed]"))
        if webcam:
            print("- Webcam (ASCII)")
        if export:
            print("- Export ASCII video")
        if color_mode != "none":
            print(f"- Color mode: {color_mode}")
        if threaded:
            print("- Multithreaded")
        print(f"- Redraw mode: {redraw_mode}")
        print(f"- Resolution: {width}x{height}")
        input("\nPress Enter to start...")

        if video and webcam and threaded:
            webcam_thread = threading.Thread(
                target=play_webcam,
                args=(export, None, None, color_mode, threaded, redraw_mode, width, height),
                daemon=True
            )
            webcam_thread.start()
            play_video(video_path, audio, export, None, None, color_mode, threaded, redraw_mode, width, height)
            webcam_thread.join(timeout=1.0)
        elif video:
            play_video(video_path, audio, export, None, None, color_mode, threaded, redraw_mode, width, height)
        elif webcam:
            play_webcam(export, None, None, color_mode, threaded, redraw_mode, width, height)
        else:
            print("Nothing to run after resolving modes.")

        print("\nPlayback finished. Returning to main menu...")
        time.sleep(1)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        show_cursor()
        print("\nInterrupted.")