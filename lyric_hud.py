"""
lyric_hud.py — Fullscreen Spotify Lyrics HUD for macOS (M2)
- Art moved higher, lyrics and controls repositioned.
- Fast button response (25ms polling).
- Uses LRCLIB for lyrics, TheAudioDB for duration fallback.
- Self‑contained, no separate API server.
"""

import sys
import re
import time
import subprocess
import threading
import urllib.parse
import json

import requests
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QSlider, QPushButton, QFrame,
    QScrollArea, QTextEdit
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import (
    QPainter, QColor, QLinearGradient, QFont,
    QImage, QPixmap, QTextCursor, QTextOption
)

from AppKit import NSImage, NSBitmapImageRep
import objc
import Quartz

# ──────────────────────────────────────────────
#  CONSTANTS
# ──────────────────────────────────────────────
LRCLIB_API_URL      = "https://lrclib.net/api/get"
THEAUDIODB_API_KEY  = "123"
POLL_INTERVAL_MS    = 25
FONT_SIZE           = 38
ACTIVE_FONT_SIZE    = int(FONT_SIZE * 1.25)
DOUBLE_TAP_WINDOW   = 0.30
ART_SIZE            = 220
ART_OFFSET_FROM_CENTER = 590          # horizontal shift left
ART_VERTICAL_OFFSET = -80              # move art up by 80 pixels
LYRICS_WIDTH_RATIO  = 0.8
LYRICS_HEIGHT_RATIO = 0.7
LYRICS_VERTICAL_OFFSET = -40           # shift lyrics up a bit to align

# Fonts
TITLE_FONT      = QFont("Arial Black", 15, QFont.Weight.Bold)
ARTIST_FONT     = QFont("Arial", 12)
TIME_FONT       = QFont("Arial", 10)
BUTTON_FONT     = QFont("Arial", 20)
PLAY_FONT       = QFont("Arial", 24)


# ──────────────────────────────────────────────
#  LRC PARSER (returns lines with timestamps)
# ──────────────────────────────────────────────
def parse_lrc_lines(lrc_text: str) -> list[dict]:
    lines = []
    line_re = re.compile(r"^\[(\d+):(\d+\.\d+)\]\s*(.*)")
    word_re = re.compile(r"<(\d+):(\d+\.\d+)>\s*([^<\[]+)")

    for line in lrc_text.splitlines():
        line = line.strip()
        if not line:
            continue

        lm = line_re.match(line)
        if lm:
            m, s, text = lm.groups()
            t = int(m) * 60 + float(s)
            lines.append({"time": t, "text": text})
            continue

        word_matches = word_re.findall(line)
        if word_matches:
            first_time = int(word_matches[0][0]) * 60 + float(word_matches[0][1])
            full_text = " ".join(w[2] for w in word_matches)
            lines.append({"time": first_time, "text": full_text})

    lines.sort(key=lambda x: x["time"])
    return lines


# ──────────────────────────────────────────────
#  LRCLIB LYRIC FETCHER
# ──────────────────────────────────────────────
def fetch_lyrics_from_lrclib(artist: str, track: str) -> str:
    if not artist or not track:
        return ""
    try:
        params = {"artist_name": artist, "track_name": track}
        url = f"{LRCLIB_API_URL}?{urllib.parse.urlencode(params)}"
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("syncedLyrics", "")
        else:
            print(f"[lrclib] Status {resp.status_code}: {resp.text}")
            return ""
    except Exception as e:
        print(f"[lrclib] Error: {e}")
        return ""


# ──────────────────────────────────────────────
#  THEAUDIODB DURATION FETCHER (seconds)
# ──────────────────────────────────────────────
def get_duration_from_theaudiodb(artist: str, track: str) -> int | None:
    try:
        artist_clean = urllib.parse.quote(artist)
        track_clean = urllib.parse.quote(track)
        url = f"https://www.theaudiodb.com/api/v1/json/{THEAUDIODB_API_KEY}/searchtrack.php?s={artist_clean}&t={track_clean}"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if 'track' in data and data['track']:
                track_info = data['track'][0]
                duration = track_info.get('intDuration')
                if duration and int(duration) > 0:
                    return int(duration)
    except Exception as e:
        print(f"[theaudiodb] Error: {e}")
    return None


# ──────────────────────────────────────────────
#  DOMINANT COLOR EXTRACTOR
# ──────────────────────────────────────────────
def dominant_color_from_url(url: str) -> tuple[int, int, int]:
    if not url or not isinstance(url, str) or url == "missing value" or not url.startswith(('http://', 'https://')):
        return (120, 120, 255)
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, timeout=4, context=ctx) as resp:
            data = resp.read()

        ns_data = objc.lookUpClass("NSData").dataWithBytes_length_(data, len(data))
        img = NSImage.alloc().initWithData_(ns_data)
        if img is None:
            return (120, 120, 255)

        SIZE = 32
        img.setSize_((SIZE, SIZE))
        rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
        if rep is None:
            return (120, 120, 255)

        samples = []
        step = max(1, SIZE // 8)
        for x in range(0, SIZE, step):
            for y in range(0, SIZE, step):
                color = rep.colorAtX_y_(x, y)
                if color is None:
                    continue
                rgb = color.colorUsingColorSpaceName_("NSCalibratedRGBColorSpace")
                if rgb is None:
                    continue
                r = int(rgb.redComponent() * 255)
                g = int(rgb.greenComponent() * 255)
                b = int(rgb.blueComponent() * 255)
                samples.append((r, g, b))

        if not samples:
            return (120, 120, 255)

        return max(samples, key=lambda c: (max(c) - min(c)) / (max(c) + 1e-9))
    except Exception as e:
        print(f"[accent] Color extraction failed: {e}")
        return (120, 120, 255)


# ──────────────────────────────────────────────
#  SPOTIFY READER + CONTROLS (AppleScript)
# ──────────────────────────────────────────────
def get_spotify_state() -> dict | None:
    script = """
    tell application "Spotify"
        if player state is playing or player state is paused then
            set pos      to player position
            set art      to artist of current track
            set trk      to name of current track
            set art_url  to artwork url of current track
            set vol      to sound volume
            set pstate   to (player state as string)
            set dur      to duration of current track
            return (pos as string) & "|||" & art & "|||" & trk & "|||" & art_url & "|||" & (vol as string) & "|||" & pstate & "|||" & dur
        else
            return ""
        end if
    end tell
    """
    try:
        out = subprocess.check_output(
            ["osascript", "-e", script],
            stderr=subprocess.DEVNULL,
            timeout=1,
        ).decode().strip()
        if not out:
            return None
        parts = out.split("|||")
        if len(parts) < 3:
            return None

        artist = parts[1] if parts[1] != "missing value" else ""
        name = parts[2] if parts[2] != "missing value" else ""
        artwork = parts[3] if len(parts) > 3 and parts[3] != "missing value" else ""
        volume = int(float(parts[4])) if len(parts) > 4 and parts[4] != "missing value" else 50
        playing = parts[5].strip() == "playing" if len(parts) > 5 else True
        duration_raw = parts[6] if len(parts) > 6 and parts[6] != "missing value" else "0"
        duration = int(duration_raw)
        if duration > 10000:
            duration = duration // 1000

        return {
            "position": float(parts[0]),
            "artist":   artist,
            "name":     name,
            "artwork":  artwork,
            "volume":   volume,
            "playing":  playing,
            "duration": duration,
        }
    except Exception as e:
        print(f"[spotify] {e}")
        return None


def spotify_play_pause():
    subprocess.Popen(["osascript", "-e", 'tell application "Spotify" to playpause'])


def spotify_next():
    subprocess.Popen(["osascript", "-e", 'tell application "Spotify" to next track'])


def spotify_previous():
    subprocess.Popen(["osascript", "-e", 'tell application "Spotify" to previous track'])


def spotify_set_volume(vol: int):
    subprocess.Popen(["osascript", "-e", f'tell application "Spotify" to set sound volume to {vol}'])


def spotify_seek(position: float):
    subprocess.Popen(["osascript", "-e", f'tell application "Spotify" to set player position to {position}'])


# ──────────────────────────────────────────────
#  SIGNAL BRIDGE
# ──────────────────────────────────────────────
class Bridge(QObject):
    show_hud = pyqtSignal()
    hide_hud = pyqtSignal()


bridge = Bridge()


# ──────────────────────────────────────────────
#  GLOBAL HOTKEY (CGEventTap)
# ──────────────────────────────────────────────
def start_cgevent_tap():
    last_ctrl_time = [0.0]
    hud_visible    = [False]
    NX_FLAGSCHANGED = 12
    CTRL_KEYCODE_CG = 59

    def callback(proxy, event_type, event, refcon):
        try:
            if event_type == NX_FLAGSCHANGED:
                keycode   = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                flags     = Quartz.CGEventGetFlags(event)
                ctrl_down = bool(flags & Quartz.kCGEventFlagMaskControl)

                if keycode == CTRL_KEYCODE_CG:
                    now = time.time()
                    if ctrl_down:
                        gap = now - last_ctrl_time[0]
                        if gap < DOUBLE_TAP_WINDOW:
                            if hud_visible[0]:
                                hud_visible[0] = False
                                bridge.hide_hud.emit()
                            else:
                                hud_visible[0] = True
                                bridge.show_hud.emit()
                            last_ctrl_time[0] = 0.0
                        else:
                            last_ctrl_time[0] = now
        except Exception as e:
            print(f"[cgeventtap] {e}")
        return event

    def run():
        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(NX_FLAGSCHANGED),
            callback,
            None,
        )
        if tap is None:
            print("[cgeventtap] ❌ Could not create event tap.")
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
        loop   = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(tap, True)
        print("[cgeventtap] ✓ Event tap running")
        Quartz.CFRunLoopRun()

    threading.Thread(target=run, daemon=True).start()


# ──────────────────────────────────────────────
#  ROUNDED ALBUM ART LABEL
# ──────────────────────────────────────────────
class RoundedLabel(QLabel):
    def __init__(self, radius=18, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._radius = radius
        self._pixmap = None
        self.accent_color = (120, 120, 255)

    def setRoundedPixmap(self, pixmap: QPixmap):
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        if self._pixmap and not self._pixmap.isNull():
            p.drawPixmap(self.rect(), self._pixmap)

        r, g, b = self.accent_color
        v_grad = QLinearGradient(0, 0, 0, self.height())
        v_grad.setColorAt(0.0, QColor(r, g, b, 75))
        v_grad.setColorAt(0.5, QColor(0, 0, 0, 0))
        v_grad.setColorAt(1.0, QColor(r, g, b, 75))
        p.fillRect(self.rect(), v_grad)

        h_grad = QLinearGradient(0, 0, self.width(), 0)
        h_grad.setColorAt(0.0, QColor(r, g, b, 12))
        h_grad.setColorAt(0.2, QColor(0, 0, 0, 0))
        h_grad.setColorAt(0.8, QColor(0, 0, 0, 0))
        h_grad.setColorAt(1.0, QColor(r, g, b, 12))
        p.fillRect(self.rect(), h_grad)
        p.end()


# ──────────────────────────────────────────────
#  HUD WIDGET
# ──────────────────────────────────────────────
class LyricHUD(QWidget):
    _sig_art    = pyqtSignal(QPixmap)
    _sig_accent = pyqtSignal(int, int, int)
    _sig_meta   = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet("QWidget { border: none; background: transparent; }")

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(0, 0, screen.width(), screen.height())
        self._sw = screen.width()
        self._sh = screen.height()

        self._lines = []
        self._accent = (120, 120, 255)
        self._current_song = ""
        self._fetching = False
        self._is_playing = True
        self._user_dragging_vol = False
        self._user_seeking = False
        self._duration = 0

        self._build_ui()

        self._sig_art.connect(self._on_art)
        self._sig_accent.connect(self._on_accent)
        self._sig_meta.connect(self._on_meta)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

        self.hide()

    def _build_ui(self):
        sw, sh = self._sw, self._sh
        center_x = sw // 2
        # Art position: horizontally shifted left, vertically moved up
        art_x = center_x - (ART_SIZE // 2) - ART_OFFSET_FROM_CENTER
        art_y = (sh - ART_SIZE) // 2 + ART_VERTICAL_OFFSET

        # Album art
        self._art = RoundedLabel(radius=18, parent=self)
        self._art.setGeometry(art_x, art_y, ART_SIZE, ART_SIZE)
        self._art.setStyleSheet("background: rgba(20,20,20,0.55); border-radius: 18px;")

        # Song title & artist
        self._title = QLabel("—", self)
        self._title.setGeometry(art_x, art_y + ART_SIZE + 16, ART_SIZE + 100, 36)
        self._title.setFont(TITLE_FONT)
        self._title.setStyleSheet("color: white; background: transparent; border: none;")

        self._artist = QLabel("—", self)
        self._artist.setGeometry(art_x, art_y + ART_SIZE + 56, ART_SIZE + 100, 26)
        self._artist.setFont(ARTIST_FONT)
        self._artist.setStyleSheet("color: rgba(255,255,255,0.6); background: transparent; border: none;")

        # Transport buttons
        btn_w = 50
        btn_h = 50
        btn_y = art_y + ART_SIZE + 96
        btn_spacing = 20
        total_btn_w = btn_w * 3 + btn_spacing * 2
        start_x = art_x + (ART_SIZE - total_btn_w) // 2

        self._prev_btn = QPushButton("⏮", self)
        self._prev_btn.setGeometry(start_x, btn_y, btn_w, btn_h)
        self._prev_btn.setFont(BUTTON_FONT)
        self._prev_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._prev_btn.clicked.connect(spotify_previous)

        self._play_btn = QPushButton("⏸", self)
        self._play_btn.setGeometry(start_x + btn_w + btn_spacing, btn_y, btn_w, btn_h)
        self._play_btn.setFont(PLAY_FONT)
        self._play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_btn.clicked.connect(self._on_play_pause)

        self._next_btn = QPushButton("⏭", self)
        self._next_btn.setGeometry(start_x + 2*(btn_w + btn_spacing), btn_y, btn_w, btn_h)
        self._next_btn.setFont(BUTTON_FONT)
        self._next_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._next_btn.clicked.connect(spotify_next)

        # Timeline slider and time labels
        timeline_y = btn_y + btn_h + 20
        slider_width = ART_SIZE + 100
        self._time_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._time_slider.setGeometry(art_x, timeline_y, slider_width, 22)
        self._time_slider.setRange(0, 1000)
        self._time_slider.setValue(0)
        self._time_slider.sliderPressed.connect(self._on_seek_start)
        self._time_slider.sliderMoved.connect(self._on_seek_move)
        self._time_slider.sliderReleased.connect(self._on_seek_end)

        self._current_time = QLabel("0:00", self)
        self._current_time.setGeometry(art_x, timeline_y + 30, 60, 20)
        self._current_time.setFont(TIME_FONT)
        self._current_time.setStyleSheet("color: rgba(255,255,255,0.7); background: transparent; border: none;")

        self._total_time = QLabel("0:00", self)
        self._total_time.setGeometry(art_x + slider_width - 60, timeline_y + 30, 60, 20)
        self._total_time.setFont(TIME_FONT)
        self._total_time.setStyleSheet("color: rgba(255,255,255,0.7); background: transparent; border: none;")

        # Volume slider
        vol_y = timeline_y + 55
        self._vol_icon = QLabel("🔊", self)
        self._vol_icon.setGeometry(art_x, vol_y, 28, 28)
        self._vol_icon.setStyleSheet("background: transparent; border: none;")

        self._vol_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._vol_slider.setGeometry(art_x + 34, vol_y + 3, slider_width - 34, 22)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(50)
        self._vol_slider.sliderPressed.connect(lambda: setattr(self, '_user_dragging_vol', True))
        self._vol_slider.sliderReleased.connect(self._on_vol_released)

        # Lyrics: centered scroll area with adjustable vertical offset
        lyric_width = int(sw * LYRICS_WIDTH_RATIO)
        lyric_height = int(sh * LYRICS_HEIGHT_RATIO)
        lyric_x = (sw - lyric_width) // 2
        lyric_y = (sh - lyric_height) // 2 + LYRICS_VERTICAL_OFFSET

        self._lyric_scroll = QScrollArea(self)
        self._lyric_scroll.setGeometry(lyric_x, lyric_y, lyric_width, lyric_height)
        self._lyric_scroll.setWidgetResizable(True)
        self._lyric_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._lyric_text = QTextEdit()
        self._lyric_text.setReadOnly(True)
        self._lyric_text.setFrameStyle(QFrame.Shape.NoFrame)
        self._lyric_text.setStyleSheet("""
            QTextEdit {
                background: transparent;
                border: none;
                color: white;
                text-align: center;
            }
        """)
        self._lyric_text.setFont(QFont("Arial", FONT_SIZE))
        self._lyric_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lyric_text.document().setDefaultTextOption(
            QTextOption(Qt.AlignmentFlag.AlignCenter)
        )
        self._lyric_scroll.setWidget(self._lyric_text)

        self._interactive_widgets = [self._prev_btn, self._play_btn, self._next_btn,
                                      self._vol_slider, self._time_slider]

        self._style_buttons(120, 120, 255)

    def _style_buttons(self, r, g, b):
        for btn in (self._prev_btn, self._play_btn, self._next_btn):
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: rgba({r},{g},{b},0.22);
                    border: 1.5px solid rgba({r},{g},{b},0.45);
                    border-radius: 25px;
                    color: white;
                }}
                QPushButton:hover {{ background: rgba({r},{g},{b},0.38); }}
                QPushButton:pressed {{ background: rgba({r},{g},{b},0.55); }}
            """)
        self._time_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: rgba(255,255,255,0.25);
                border-radius: 2px;
                border: none;
            }}
            QSlider::sub-page:horizontal {{
                background: rgb({r},{g},{b});
                border-radius: 2px;
                border: none;
            }}
            QSlider::handle:horizontal {{
                width: 14px; height: 14px;
                margin: -5px 0;
                border-radius: 7px;
                background: white;
                border: none;
            }}
        """)
        self._vol_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: rgba(255,255,255,0.18);
                border-radius: 2px;
                border: none;
            }}
            QSlider::sub-page:horizontal {{
                background: rgb({r},{g},{b});
                border-radius: 2px;
                border: none;
            }}
            QSlider::handle:horizontal {{
                width: 16px; height: 16px;
                margin: -6px 0;
                border-radius: 8px;
                background: rgb({r},{g},{b});
                border: none;
            }}
        """)

    def _on_art(self, pixmap):
        self._art.setRoundedPixmap(pixmap)

    def _on_accent(self, r, g, b):
        self._accent = (r, g, b)
        self._style_buttons(r, g, b)
        self._art.accent_color = (r, g, b)
        self._art.update()
        self.update()

    def _on_meta(self, title, artist):
        self._title.setText(title[:34] + ("…" if len(title) > 34 else ""))
        self._artist.setText(artist[:44] + ("…" if len(artist) > 44 else ""))

    def _on_play_pause(self):
        spotify_play_pause()
        self._is_playing = not self._is_playing
        self._play_btn.setText("⏸" if self._is_playing else "▶")

    def _on_vol_released(self):
        self._user_dragging_vol = False
        vol = self._vol_slider.value()
        spotify_set_volume(vol)
        icons = ["🔇", "🔈", "🔉", "🔊"]
        self._vol_icon.setText(icons[min(3, vol // 26)])

    def _on_seek_start(self):
        self._user_seeking = True

    def _on_seek_move(self, value):
        if self._duration > 0:
            new_pos = (value / 1000.0) * self._duration
            self._current_time.setText(self._format_time(new_pos))

    def _on_seek_end(self):
        if self._duration > 0:
            new_pos = (self._time_slider.value() / 1000.0) * self._duration
            spotify_seek(new_pos)
        self._user_seeking = False

    def paintEvent(self, event):
        r, g, b = self._accent
        w, h = self.width(), self.height()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Full‑screen accent overlay (5% transparency)
        accent_overlay = QColor(r, g, b, 13)
        p.fillRect(0, 0, w, h, accent_overlay)

        # Top band
        tg = QLinearGradient(0, 0, 0, 210)
        tg.setColorAt(0.0, QColor(r, g, b, 210))
        tg.setColorAt(1.0, QColor(r, g, b, 0))
        p.fillRect(0, 0, w, 210, tg)
        p.fillRect(0, 0, w, 5, QColor(r, g, b, 255))

        # Bottom band
        bg = QLinearGradient(0, h - 210, 0, h)
        bg.setColorAt(0.0, QColor(r, g, b, 0))
        bg.setColorAt(0.55, QColor(r, g, b, 80))
        bg.setColorAt(1.0, QColor(r, g, b, 220))
        p.fillRect(0, h - 210, w, 210, bg)
        p.fillRect(0, h - 5, w, 5, QColor(r, g, b, 255))

        # Side gradients
        lg = QLinearGradient(0, 0, w//3, 0)
        lg.setColorAt(0.0, QColor(r, g, b, 60))
        lg.setColorAt(1.0, QColor(r, g, b, 0))
        p.fillRect(0, 0, w//3, h, lg)

        rg = QLinearGradient(w - w//3, 0, w, 0)
        rg.setColorAt(0.0, QColor(r, g, b, 0))
        rg.setColorAt(1.0, QColor(r, g, b, 12))
        p.fillRect(w - w//3, 0, w//3, h, rg)

        p.end()

    def _poll(self):
        state = get_spotify_state()
        if not state:
            return

        song_key = f"{state['artist']}|||{state['name']}"

        if state.get("duration", 0) > 0:
            self._duration = state["duration"]

        if not self._user_dragging_vol:
            v = state.get("volume", 50)
            if abs(v - self._vol_slider.value()) > 2:
                self._vol_slider.setValue(v)

        playing = state.get("playing", True)
        if playing != self._is_playing:
            self._is_playing = playing
            self._play_btn.setText("⏸" if playing else "▶")

        if not self._user_seeking and self._duration > 0:
            pos = state.get("position", 0)
            slider_val = int((pos / self._duration) * 1000)
            self._time_slider.setValue(slider_val)
            self._current_time.setText(self._format_time(pos))
            self._total_time.setText(self._format_time(self._duration))
        elif self._duration == 0:
            self._time_slider.setValue(0)
            self._current_time.setText("0:00")
            self._total_time.setText("0:00")

        if song_key != self._current_song and not self._fetching:
            print(f"\n[poll] New song: {state['name']} — {state['artist']}")
            self._current_song = song_key
            self._lines = []
            self._lyric_text.setPlainText("Loading…")
            self._fetching = True
            threading.Thread(
                target=self._load_song,
                args=(state["artist"], state["name"], state["artwork"], state.get("duration", 0)),
                daemon=True,
            ).start()

        self._render(state["position"])

    def _load_song(self, artist, name, artwork, spotify_duration):
        self._sig_meta.emit(name, artist)

        # Duration from Spotify or TheAudioDB
        if spotify_duration <= 0 and artist and name:
            print("[duration] Spotify duration 0, trying TheAudioDB...")
            dur = get_duration_from_theaudiodb(artist, name)
            if dur:
                self._duration = dur
                print(f"[duration] TheAudioDB returned {self._duration}s")
            else:
                print("[duration] TheAudioDB also failed")
                self._duration = 0
        else:
            self._duration = spotify_duration
            print(f"[duration] Using Spotify duration: {self._duration}s")

        # Fetch lyrics from LRCLIB
        if artist and name:
            print(f"[lyrics] Fetching from LRCLIB: {artist} – {name}")
            lrc = fetch_lyrics_from_lrclib(artist, name)
            if lrc:
                self._lines = parse_lrc_lines(lrc)
                print(f"[lyrics] {len(self._lines)} lines")
            else:
                print("[lyrics] No synced lyrics found")
                self._lines = []
        else:
            self._lines = []

        # Fetch artwork
        if artwork and artwork.startswith(('http://', 'https://')):
            try:
                resp = requests.get(artwork, timeout=5)
                img = QImage()
                img.loadFromData(resp.content)
                px = QPixmap.fromImage(img).scaled(
                    ART_SIZE, ART_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._sig_art.emit(px)
            except Exception as e:
                print(f"[art] {e}")

        # Accent color from artwork
        if artwork and artwork.startswith(('http://', 'https://')):
            r, g, b = dominant_color_from_url(artwork)
            r = min(255, int(r * 1.15))
            g = min(255, int(g * 1.15))
            b = min(255, int(b * 1.15))
            self._sig_accent.emit(r, g, b)
        else:
            self._sig_accent.emit(120, 120, 255)

        self._fetching = False

    def _render(self, position):
        if not self._lines:
            return

        # Find current line index
        cur_idx = 0
        for i, line in enumerate(self._lines):
            if line["time"] <= position:
                cur_idx = i
            else:
                break

        start = max(0, cur_idx - 5)
        end   = min(len(self._lines), cur_idx + 6)
        window = self._lines[start:end]

        r, g, b = self._accent
        html = []
        for i, line in enumerate(window):
            gidx = start + i
            if gidx == cur_idx:
                html.append(
                    f'<div style="margin: 16px 0; line-height: 1.4;">'
                    f'<span style="'
                    f'color:white;'
                    f'font-size:{ACTIVE_FONT_SIZE}pt;'
                    f'font-weight:bold;'
                    f'text-shadow:0 0 12px rgba({r},{g},{b},0.8);'
                    f'">{line["text"]}</span>'
                    f'</div>'
                )
            else:
                dist = abs(gidx - cur_idx)
                opacity = max(0.4, 0.8 - dist * 0.1)
                sz = max(FONT_SIZE - dist*3, 20)
                html.append(
                    f'<div style="margin: 8px 0; line-height: 1.3;">'
                    f'<span style="color:rgba(255,255,255,{opacity});'
                    f'font-size:{sz}pt;">'
                    f'{line["text"]}</span>'
                    f'</div>'
                )

        self._lyric_text.setHtml("".join(html))

        # Auto‑scroll to the active line
        target_line_number = cur_idx - start
        block = self._lyric_text.document().firstBlock()
        count = 0
        target_block = None
        while block.isValid():
            if count == target_line_number:
                target_block = block
                break
            block = block.next()
            count += 1
        if target_block:
            cursor = QTextCursor(target_block)
            self._lyric_text.setTextCursor(cursor)
            self._lyric_text.ensureCursorVisible()

    def _format_time(self, seconds):
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}:{secs:02d}"

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        for w in self._interactive_widgets:
            if w.geometry().contains(pos):
                super().mousePressEvent(event)
                return
        self.hide()

    def show(self):
        super().show()
        self.raise_()
        self.activateWindow()

    def hide(self):
        super().hide()


# ──────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────
def main():
    from AppKit import NSApplication
    ns_app = NSApplication.sharedApplication()
    ns_app.setActivationPolicy_(2)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    hud = LyricHUD()
    bridge.show_hud.connect(hud.show)
    bridge.hide_hud.connect(hud.hide)

    start_cgevent_tap()

    print("LyricHUD running.")
    print("  Double-tap Control → show/hide HUD")
    print("  Click outside controls → hide HUD")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
