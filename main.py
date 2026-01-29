from __future__ import annotations

import math
import os
import sys
import tempfile
import wave
#
from array import array
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QElapsedTimer, QObject, QPoint, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer, QSoundEffect
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QToolBar,
    QStatusBar,
    QSpinBox,
)

try:
    import winsound as _winsound  # type: ignore
except Exception:  # pragma: no cover
    _winsound = None

try:
    import simpleaudio as _sa  # type: ignore
except Exception:  # pragma: no cover
    _sa = None


@dataclass
class Note:
    """Nota estilo piano-roll: pitch MIDI y rango de tiempo en ticks."""

    pitch: int  # 0..127
    start: int  # ticks
    length: int  # ticks, >0
    selected: bool = False

    @property
    def end(self) -> int:
        return self.start + self.length


def midi_note_name(n: int) -> str:
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    octave = (n // 12) - 1
    return f"{names[n % 12]}{octave}"


def midi_to_freq(pitch: int) -> float:
    # A4 (69) = 440Hz
    return 440.0 * (2.0 ** ((pitch - 69) / 12.0))


def ticks_to_seconds(ticks: int, ppq: int, bpm: float) -> float:
    return (ticks / float(ppq)) * (60.0 / float(bpm))


def _write_wav_mono_16bit(path: str, samples: array, sample_rate: int) -> None:
    """Escribe un WAV mono 16-bit PCM desde floats en [-1, 1]."""
    # Normalizar
    peak = 0.0
    for x in samples:
        ax = abs(float(x))
        if ax > peak:
            peak = ax
    if peak < 1e-9:
        peak = 1.0
    gain = 0.9 / peak

    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        chunk = array("h")
        for x in samples:
            y = float(x) * gain
            if y > 1.0:
                y = 1.0
            elif y < -1.0:
                y = -1.0
            chunk.append(int(round(y * 32767.0)))
            if len(chunk) >= 16384:
                wf.writeframes(chunk.tobytes())
                chunk = array("h")
        if chunk:
            wf.writeframes(chunk.tobytes())


def _floats_to_pcm16_mono(samples: array) -> bytes:
    """Convierte floats a PCM16 mono (bytes) con normalización."""
    peak = 0.0
    for x in samples:
        ax = abs(float(x))
        if ax > peak:
            peak = ax
    if peak < 1e-9:
        peak = 1.0
    gain = 0.9 / peak

    out = array("h")
    for x in samples:
        y = float(x) * gain
        if y > 1.0:
            y = 1.0
        elif y < -1.0:
            y = -1.0
        out.append(int(round(y * 32767.0)))
    return out.tobytes()


def _synth_piano_note(
    freq: float,
    duration_s: float,
    sample_rate: int,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Optional[array]:
    """
    Sintetizador simple tipo piano:
    - suma de armónicos + tanh suave (brillo)
    - envolvente de ataque corto + decaimiento exponencial
    """
    attack = 0.003
    # Notas más graves: decaimiento algo más largo
    decay = 1.2 * ((440.0 / max(20.0, freq)) ** 0.18)
    decay = max(0.7, min(3.0, decay))
    tail = min(2.0, decay * 2.2)

    total_s = max(0.02, float(duration_s) + tail)
    n = int(total_s * sample_rate)
    out = array("f", [0.0]) * n

    harmonics = (
        (1, 1.00),
        (2, 0.55),
        (3, 0.35),
        (4, 0.22),
        (5, 0.16),
        (6, 0.12),
    )

    two_pi = 2.0 * math.pi
    for i in range(n):
        if should_cancel is not None and (i & 2047) == 0 and should_cancel():
            return None
        t = i / float(sample_rate)
        a = 1.0 if t >= attack else (t / attack)
        env = a * math.exp(-t / decay)

        s = 0.0
        for k, amp in harmonics:
            s += amp * math.sin(two_pi * (freq * k) * t)
        # saturación suave para "cuerpo"
        out[i] = math.tanh(s * 1.1) * env * 0.35

    return out


def render_pianoroll_to_wav(
    notes: list[Note],
    ppq: int,
    bpm: float,
    out_path: str,
    sample_rate: int = 44100,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> bool:
    if not notes:
        # Un archivo silencioso corto
        silence = array("f", [0.0]) * int(sample_rate * 0.25)
        _write_wav_mono_16bit(out_path, silence, sample_rate)
        return True

    end_tick = max(n.end for n in notes)
    total_s = ticks_to_seconds(end_tick, ppq, bpm) + 2.5
    total_n = int(total_s * sample_rate)
    mix = array("f", [0.0]) * total_n

    # Render por nota y mezclar
    for note in notes:
        if should_cancel is not None and should_cancel():
            return False
        start_s = ticks_to_seconds(note.start, ppq, bpm)
        dur_s = max(0.02, ticks_to_seconds(note.length, ppq, bpm))
        start_i = int(start_s * sample_rate)
        if start_i >= total_n:
            continue

        voice = _synth_piano_note(midi_to_freq(note.pitch), dur_s, sample_rate, should_cancel=should_cancel)
        if voice is None:
            return False

        max_len = min(len(voice), total_n - start_i)
        for i in range(max_len):
            mix[start_i + i] += voice[i]

    _write_wav_mono_16bit(out_path, mix, sample_rate)
    return True


class AudioRenderWorker(QObject):
    # Importante: incluir gen/key en la señal para sincronizar en el hilo de UI.
    finished = Signal(int, object, str)  # generation, render_key, wav_path
    cancelled = Signal(int)
    failed = Signal(int, str)

    def __init__(self, notes: list[Note], ppq: int, bpm: float, generation: int, render_key: tuple) -> None:
        super().__init__()
        self._notes = [Note(pitch=n.pitch, start=n.start, length=n.length) for n in notes]
        self._ppq = ppq
        self._bpm = bpm
        self._generation = int(generation)
        self._render_key = render_key
        self._cancel = False

    @Slot()
    def cancel(self) -> None:
        self._cancel = True

    def _should_cancel(self) -> bool:
        return self._cancel

    @Slot()
    def run(self) -> None:
        path: Optional[str] = None
        try:
            fd, path = tempfile.mkstemp(prefix="pianoroll_", suffix=".wav")
            os.close(fd)
            ok = render_pianoroll_to_wav(
                notes=self._notes,
                ppq=self._ppq,
                bpm=self._bpm,
                out_path=path,
                should_cancel=self._should_cancel,
            )
            if not ok:
                try:
                    os.remove(path)
                except OSError:
                    pass
                self.cancelled.emit(self._generation)
                return
            self.finished.emit(self._generation, self._render_key, path)
        except Exception as e:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            self.failed.emit(self._generation, str(e))


class PianoRollWidget(QWidget):
    previewPitchRequested = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Rango visible (pitches)
        self.high_pitch = 84  # C6
        self.low_pitch = 36  # C2

        # Geometría
        self.key_width = 90
        self.row_h = 16
        self.header_h = 24

        # Tiempo
        self.ppq = 480  # ticks por negra
        self.beat_w = 80.0  # px por negra (zoom horizontal)
        self.min_beat_w = 20.0
        self.max_beat_w = 300.0
        self.quant = self.ppq // 4  # semicorchea

        # Scroll (en px)
        self.scroll_x = 0
        self.scroll_y = 0

        # Contenido (timeline)
        self.total_beats = 64  # longitud visible total

        # Notas
        self.notes: list[Note] = [
            Note(pitch=60, start=0, length=self.ppq),
            Note(pitch=64, start=self.ppq, length=self.ppq),
            Note(pitch=67, start=self.ppq * 2, length=self.ppq * 2),
        ]

        # Interacción
        self._mode: Optional[str] = None  # "create" | "drag" | "resize_l" | "resize_r"
        self._resize_edge_px = 7
        self._press_pos = QPoint()
        self._press_note_index: Optional[int] = None
        self._press_note_start = 0
        self._press_note_end = 0
        self._press_note_pitch = 60
        self._create_note_index: Optional[int] = None
        self._hover_pitch: Optional[int] = None
        self._hover_tick: Optional[int] = None

        self._recompute_scrollbars()

        # Playhead / grabación
        self.playhead_tick: int = 0
        self.record_enabled: bool = False

    # ---------- utilidades de layout / conversión ----------
    def pitches_count(self) -> int:
        return (self.high_pitch - self.low_pitch) + 1

    def content_height_px(self) -> int:
        return self.pitches_count() * self.row_h

    def content_width_px(self) -> int:
        return int(self.total_beats * self.beat_w)

    def tick_to_x(self, tick: int) -> float:
        return (tick / self.ppq) * self.beat_w

    def x_to_tick(self, x: float) -> int:
        beats = x / self.beat_w
        return int(round(beats * self.ppq))

    def pitch_to_y(self, pitch: int) -> int:
        # y=0 es header inferior; pitch alto arriba
        idx_from_top = self.high_pitch - pitch
        return idx_from_top * self.row_h

    def y_to_pitch(self, y: int) -> int:
        idx = int(y // self.row_h)
        pitch = self.high_pitch - idx
        return max(self.low_pitch, min(self.high_pitch, pitch))

    def grid_rect(self) -> QRect:
        return QRect(self.key_width, self.header_h, self.width() - self.key_width, self.height() - self.header_h)

    def _recompute_scrollbars(self) -> None:
        # Mantener scroll dentro de límites.
        max_x = max(0, self.content_width_px() - max(0, self.grid_rect().width()))
        max_y = max(0, self.content_height_px() - max(0, self.grid_rect().height()))
        self.scroll_x = max(0, min(self.scroll_x, max_x))
        self.scroll_y = max(0, min(self.scroll_y, max_y))

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(1100, 600)

    # ---------- selección / hit testing ----------
    def _note_rect_px(self, note: Note) -> QRectF:
        x = self.tick_to_x(note.start)
        w = max(1.0, self.tick_to_x(note.length))
        y = float(self.pitch_to_y(note.pitch))
        return QRectF(x, y, w, float(self.row_h))

    def _hit_test_note(self, grid_pos: QPoint) -> Optional[int]:
        # grid_pos: coordenadas dentro del área de grilla (0,0 en esquina sup izq de grilla, bajo header)
        x = grid_pos.x() + self.scroll_x
        y = grid_pos.y() + self.scroll_y
        p = QPoint(int(x), int(y))
        for i in reversed(range(len(self.notes))):
            r = self._note_rect_px(self.notes[i])
            if r.contains(p):
                return i
        return None

    def _hit_test_note_part(self, grid_pos: QPoint) -> tuple[Optional[int], Optional[str]]:
        """
        Devuelve (idx, part) donde part es:
        - "left": borde izquierdo (resize)
        - "right": borde derecho (resize)
        - "body": cuerpo de la nota (drag)
        """
        x = grid_pos.x() + self.scroll_x
        y = grid_pos.y() + self.scroll_y
        p = QPoint(int(x), int(y))
        for i in reversed(range(len(self.notes))):
            r = self._note_rect_px(self.notes[i])
            if not r.contains(p):
                continue
            # prioridad a bordes
            if abs(x - r.left()) <= self._resize_edge_px:
                return i, "left"
            if abs(x - r.right()) <= self._resize_edge_px:
                return i, "right"
            return i, "body"
        return None, None

    def _clear_selection(self) -> None:
        for n in self.notes:
            n.selected = False

    def _select_one(self, idx: int) -> None:
        self._clear_selection()
        self.notes[idx].selected = True

    def delete_selected(self) -> None:
        before = len(self.notes)
        self.notes = [n for n in self.notes if not n.selected]
        if len(self.notes) != before:
            self.update()

    # ---------- pintura ----------
    def paintEvent(self, event) -> None:  # type: ignore[override]
        self._recompute_scrollbars()

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor("#111317"))

        self._paint_header(p)
        self._paint_keys(p)
        self._paint_grid(p)
        self._paint_notes(p)
        self._paint_playhead(p)
        self._paint_hover(p)

    def _paint_header(self, p: QPainter) -> None:
        header = QRect(0, 0, self.width(), self.header_h)
        p.fillRect(header, QColor("#0c0e12"))

        # Separador vertical entre teclas y grilla
        p.setPen(QPen(QColor("#2a2f3a")))
        p.drawLine(self.key_width, 0, self.key_width, self.height())

        # Regla de compases/tiempos
        grid = self.grid_rect()
        p.save()
        p.setClipRect(QRect(self.key_width, 0, grid.width(), self.header_h))
        p.translate(self.key_width - self.scroll_x, 0)

        font = QFont()
        font.setPointSize(9)
        p.setFont(font)
        p.setPen(QPen(QColor("#9aa4b2")))

        beats_visible = int((self.scroll_x + grid.width()) / self.beat_w) + 2
        for beat in range(0, min(self.total_beats + 1, beats_visible)):
            x = int(beat * self.beat_w)
            is_bar = (beat % 4) == 0
            p.setPen(QPen(QColor("#3a4250" if is_bar else "#242a35"), 1))
            p.drawLine(x, self.header_h - 6, x, self.header_h)
            if is_bar:
                bar_num = (beat // 4) + 1
                p.setPen(QPen(QColor("#c3c9d4")))
                p.drawText(QRect(x + 4, 0, 80, self.header_h - 2), Qt.AlignmentFlag.AlignVCenter, f"Compás {bar_num}")
        p.restore()

    def _paint_keys(self, p: QPainter) -> None:
        keys_area = QRect(0, self.header_h, self.key_width, self.height() - self.header_h)
        p.fillRect(keys_area, QColor("#0d1016"))

        p.save()
        p.setClipRect(keys_area)
        p.translate(0, self.header_h - self.scroll_y)

        for pitch in range(self.high_pitch, self.low_pitch - 1, -1):
            y = self.pitch_to_y(pitch)
            is_black = (pitch % 12) in {1, 3, 6, 8, 10}
            base = QColor("#101521") if not is_black else QColor("#0b0e14")
            r = QRect(0, y, self.key_width, self.row_h)
            p.fillRect(r, base)

            # Línea horizontal
            p.setPen(QPen(QColor("#1c2230")))
            p.drawLine(0, y, self.key_width, y)

            # Etiqueta en C
            if pitch % 12 == 0:
                p.setPen(QPen(QColor("#b9c0cb")))
                p.drawText(QRect(6, y, self.key_width - 10, self.row_h), Qt.AlignmentFlag.AlignVCenter, midi_note_name(pitch))

        # Borde inferior final
        y_end = self.content_height_px()
        p.setPen(QPen(QColor("#1c2230")))
        p.drawLine(0, y_end, self.key_width, y_end)

        p.restore()

    def _paint_grid(self, p: QPainter) -> None:
        grid = self.grid_rect()
        p.fillRect(grid, QColor("#0f131b"))

        p.save()
        p.setClipRect(grid)
        p.translate(self.key_width - self.scroll_x, self.header_h - self.scroll_y)

        # Líneas horizontales (pitches)
        for i in range(self.pitches_count() + 1):
            y = i * self.row_h
            col = QColor("#1a202c")
            p.setPen(QPen(col, 1))
            p.drawLine(0, y, self.content_width_px(), y)

        # Líneas verticales (subdivisiones)
        ticks_per_beat = self.ppq
        beats = self.total_beats
        steps_per_beat = max(1, ticks_per_beat // max(1, self.quant))
        step_w = self.beat_w / steps_per_beat
        total_steps = beats * steps_per_beat

        for s in range(total_steps + 1):
            x = int(round(s * step_w))
            beat = s // steps_per_beat
            is_beat = (s % steps_per_beat) == 0
            is_bar = is_beat and (beat % 4 == 0)
            if is_bar:
                pen = QPen(QColor("#394152"), 2)
            elif is_beat:
                pen = QPen(QColor("#273042"), 1)
            else:
                pen = QPen(QColor("#1b2230"), 1)
            p.setPen(pen)
            p.drawLine(x, 0, x, self.content_height_px())

        p.restore()

    def _paint_notes(self, p: QPainter) -> None:
        grid = self.grid_rect()
        p.save()
        p.setClipRect(grid)
        p.translate(self.key_width - self.scroll_x, self.header_h - self.scroll_y)

        for note in self.notes:
            r = self._note_rect_px(note)
            if note.selected:
                fill = QColor("#4aa3ff")
                border = QColor("#a6d5ff")
            else:
                fill = QColor("#2e6bb3")
                border = QColor("#7fb6ff")

            rr = QRectF(r.x() + 1.0, r.y() + 1.0, max(1.0, r.width() - 2.0), max(1.0, r.height() - 2.0))
            p.fillRect(rr, fill)
            p.setPen(QPen(border, 1))
            p.drawRect(rr)

        p.restore()

    def _paint_hover(self, p: QPainter) -> None:
        if self._hover_pitch is None or self._hover_tick is None:
            return
        grid = self.grid_rect()
        if not grid.contains(self.mapFromGlobal(self.cursor().pos())):
            return

        p.save()
        p.setClipRect(grid)
        p.translate(self.key_width - self.scroll_x, self.header_h - self.scroll_y)

        x = self.tick_to_x(self._hover_tick)
        y = self.pitch_to_y(self._hover_pitch)
        p.setPen(QPen(QColor("#7a8494"), 1, Qt.PenStyle.DashLine))
        p.drawLine(int(x), 0, int(x), self.content_height_px())
        p.drawLine(0, int(y), self.content_width_px(), int(y))
        p.restore()

    def _paint_playhead(self, p: QPainter) -> None:
        grid = self.grid_rect()
        if grid.width() <= 0 or grid.height() <= 0:
            return

        p.save()
        p.setClipRect(grid)
        p.translate(self.key_width - self.scroll_x, self.header_h - self.scroll_y)

        x = self.tick_to_x(self.playhead_tick)
        if self.record_enabled:
            pen = QPen(QColor("#ff4d4d"), 2)
        else:
            pen = QPen(QColor("#e6e6e6"), 2)
        p.setPen(pen)
        p.drawLine(int(x), 0, int(x), self.content_height_px())
        p.restore()

    # ---------- wheel / zoom / scroll ----------
    def wheelEvent(self, event) -> None:  # type: ignore[override]
        modifiers = event.modifiers()
        delta = event.angleDelta()

        if modifiers & Qt.KeyboardModifier.ControlModifier:
            # Zoom horizontal alrededor del cursor
            steps = delta.y() / 120.0
            if steps == 0:
                return
            grid = self.grid_rect()
            cursor_x_in_grid = max(0, min(grid.width(), event.position().toPoint().x() - self.key_width))
            anchor_content_x = self.scroll_x + cursor_x_in_grid

            old = self.beat_w
            factor = 1.12 ** steps
            self.beat_w = float(max(self.min_beat_w, min(self.max_beat_w, self.beat_w * factor)))
            if abs(self.beat_w - old) < 1e-6:
                return

            # Mantener el mismo punto bajo el cursor
            ratio = self.beat_w / old
            new_anchor = anchor_content_x * ratio
            self.scroll_x = int(round(new_anchor - cursor_x_in_grid))
            self._recompute_scrollbars()
            self.update()
            return

        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            # scroll horizontal
            dx = -delta.y()
            self.scroll_x = int(self.scroll_x + dx)
            self._recompute_scrollbars()
            self.update()
            return

        # scroll vertical (default)
        dy = -delta.y()
        self.scroll_y = int(self.scroll_y + dy)
        self._recompute_scrollbars()
        self.update()

    # ---------- mouse ----------
    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        grid = self.grid_rect()
        pos = event.position().toPoint()

        if grid.contains(pos):
            local = QPoint(pos.x() - self.key_width, pos.y() - self.header_h)
            x = local.x() + self.scroll_x
            y = local.y() + self.scroll_y

            pitch = self.y_to_pitch(y)
            tick = self.x_to_tick(x)
            tick = max(0, self._quantize_tick(tick))
            self._hover_pitch = pitch
            self._hover_tick = tick
        else:
            self._hover_pitch = None
            self._hover_tick = None

        # Cursor feedback para resize
        if self._mode is None and grid.contains(pos):
            local = QPoint(pos.x() - self.key_width, pos.y() - self.header_h)
            hit_i, part = self._hit_test_note_part(local)
            if hit_i is not None and part in ("left", "right"):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

        if self._mode == "create" and self._create_note_index is not None:
            idx = self._create_note_index
            note = self.notes[idx]

            local = QPoint(pos.x() - self.key_width, pos.y() - self.header_h)
            x = local.x() + self.scroll_x
            tick = max(0, self._quantize_tick(self.x_to_tick(x)))

            start = min(note.start, tick)
            end = max(note.start, tick)
            end = max(end, start + self.quant)
            note.start = start
            note.length = max(self.quant, end - start)
            self.update()
            return

        if self._mode == "drag" and self._press_note_index is not None:
            idx = self._press_note_index
            note = self.notes[idx]
            delta_px = pos - self._press_pos

            dtick = self.x_to_tick(delta_px.x())
            dpitch = int(round(delta_px.y() / self.row_h))

            new_start = self._press_note_start + dtick
            new_start = max(0, self._quantize_tick(new_start))
            new_pitch = self._press_note_pitch - dpitch
            new_pitch = max(self.low_pitch, min(self.high_pitch, new_pitch))

            note.start = new_start
            note.pitch = new_pitch
            self.update()
            return

        if self._mode in ("resize_l", "resize_r") and self._press_note_index is not None:
            idx = self._press_note_index
            note = self.notes[idx]

            local = QPoint(pos.x() - self.key_width, pos.y() - self.header_h)
            x = local.x() + self.scroll_x
            tick = max(0, self._quantize_tick(self.x_to_tick(x)))

            if self._mode == "resize_r":
                new_end = max(note.start + self.quant, tick)
                note.length = max(self.quant, new_end - note.start)
            else:
                # resize_l mantiene el end original
                new_start = min(tick, self._press_note_end - self.quant)
                new_start = max(0, new_start)
                note.start = new_start
                note.length = max(self.quant, self._press_note_end - note.start)

            self.update()
            return

        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._press_pos = event.position().toPoint()

            grid = self.grid_rect()
            if not grid.contains(self._press_pos):
                self._clear_selection()
                self.update()
                return

            local = QPoint(self._press_pos.x() - self.key_width, self._press_pos.y() - self.header_h)
            hit, part = self._hit_test_note_part(local)
            if hit is not None:
                self._select_one(hit)
                self._press_note_index = hit
                self._press_note_start = self.notes[hit].start
                self._press_note_end = self.notes[hit].end
                self._press_note_pitch = self.notes[hit].pitch
                self.playhead_tick = self._quantize_tick(self.notes[hit].start)
                if part == "left":
                    self._mode = "resize_l"
                elif part == "right":
                    self._mode = "resize_r"
                else:
                    self._mode = "drag"
                self.update()
                return

            # crear nota nueva
            x = local.x() + self.scroll_x
            y = local.y() + self.scroll_y
            pitch = self.y_to_pitch(y)
            start = max(0, self._quantize_tick(self.x_to_tick(x)))
            n = Note(pitch=pitch, start=start, length=self.quant)
            self._clear_selection()
            n.selected = True
            self.notes.append(n)
            self.playhead_tick = start
            self._mode = "create"
            self._create_note_index = len(self.notes) - 1
            self.update()
            return

        if event.button() == Qt.MouseButton.RightButton:
            grid = self.grid_rect()
            pos = event.position().toPoint()
            if grid.contains(pos):
                local = QPoint(pos.x() - self.key_width, pos.y() - self.header_h)
                hit = self._hit_test_note(local)
                if hit is not None:
                    del self.notes[hit]
                    self.update()
                    return

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._mode = None
            self._press_note_index = None
            self._create_note_index = None
            self.unsetCursor()
            self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            return
        grid = self.grid_rect()
        pos = event.position().toPoint()
        if not grid.contains(pos):
            return

        local = QPoint(pos.x() - self.key_width, pos.y() - self.header_h)
        hit = self._hit_test_note(local)
        if hit is not None:
            self.previewPitchRequested.emit(self.notes[hit].pitch)
            return

        # pre-escucha del pitch bajo el cursor
        y = local.y() + self.scroll_y
        self.previewPitchRequested.emit(self.y_to_pitch(y))

    # ---------- teclado ----------
    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
            return
        # Algunas versiones de PySide6 no tienen QKeySequence.StandardKey.ZoomReset.
        # Usamos un check explícito para Ctrl+0.
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier) and event.key() in (Qt.Key.Key_0, Qt.Key.Key_ParenRight):
            self.beat_w = 80.0
            self.scroll_x = 0
            self._recompute_scrollbars()
            self.update()
            return
        super().keyPressEvent(event)

    # ---------- helpers ----------
    def _quantize_tick(self, tick: int) -> int:
        q = max(1, self.quant)
        return int(round(tick / q) * q)

    def set_playhead_tick(self, tick: int) -> None:
        self.playhead_tick = max(0, int(tick))
        self.update()

    def ensure_playhead_visible(self) -> None:
        grid = self.grid_rect()
        if grid.width() <= 0:
            return
        x = int(self.tick_to_x(self.playhead_tick))
        view_left = self.scroll_x
        view_right = self.scroll_x + grid.width()
        pad = 40
        if x < view_left + pad:
            self.scroll_x = max(0, x - pad)
        elif x > view_right - pad:
            self.scroll_x = max(0, x - grid.width() + pad)
        self._recompute_scrollbars()
        self.update()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Piano Roll (Qt + Python)")

        self.roll = PianoRollWidget(self)
        self.roll.previewPitchRequested.connect(self._preview_pitch)

        self._bpm = 120.0
        self._audio_output = QAudioOutput(self)
        self._audio_output.setMuted(False)
        self._audio_output.setVolume(1.0)
        try:
            self._audio_output.setDevice(QMediaDevices.defaultAudioOutput())
        except Exception:
            pass
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.errorOccurred.connect(self._on_player_error)
        self._current_wav_path: Optional[str] = None
        self._winsound_is_playing = False
        self._prefer_winsound = (os.name == "nt" and _winsound is not None)
        self._main_play_obj: Optional[object] = None  # simpleaudio playback handle
        self._main_audio_bytes: Optional[bytes] = None  # mantener buffer vivo durante play (simpleaudio)

        self._preview_effect = QSoundEffect(self)
        self._preview_effect.setVolume(0.5)
        self._preview_temp_paths: deque[str] = deque(maxlen=12)

        self._render_thread: Optional[QThread] = None
        self._render_worker: Optional[AudioRenderWorker] = None
        self._render_generation = 0  # para invalidar renders viejos (Stop)
        self._cached_render_key: Optional[tuple] = None
        self._cached_render_path: Optional[str] = None

        # Grabación / teclado / playhead
        self._record_enabled = False
        self._kb_base_pitch = 60  # C4
        self._active_key_notes: dict[int, int] = {}  # qt_key -> note_index
        self._active_key_voices: dict[int, object] = {}  # qt_key -> voice handle (simpleaudio)
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(16)  # ~60fps
        self._play_timer.timeout.connect(self._tick_playhead)
        self._play_elapsed = QElapsedTimer()
        self._play_start_tick = 0
        self._transport_running = False

        QApplication.instance().installEventFilter(self)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.roll)
        self.setCentralWidget(central)

        tb = QToolBar("Herramientas", self)
        self.addToolBar(tb)

        act_reset_zoom = QAction("Reset zoom", self)
        act_reset_zoom.setShortcut("Ctrl+0")
        act_reset_zoom.triggered.connect(self._reset_zoom)
        tb.addAction(act_reset_zoom)

        act_del = QAction("Borrar nota(s)", self)
        act_del.setShortcut("Delete")
        act_del.triggered.connect(self.roll.delete_selected)
        tb.addAction(act_del)

        tb.addSeparator()
        act_play_btn = QAction("Play", self)
        act_play_btn.triggered.connect(self.play_from_start)
        tb.addAction(act_play_btn)

        act_stop_btn = QAction("Stop", self)
        act_stop_btn.triggered.connect(self.stop)
        tb.addAction(act_stop_btn)

        # Atajo cómodo (no consume teclas de notas): Espacio alterna Play/Stop
        act_toggle = QAction("Toggle Play/Stop", self)
        act_toggle.setShortcut("Space")
        act_toggle.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_toggle.triggered.connect(self.toggle_play)
        self.addAction(act_toggle)

        act_record = QAction("Grabar", self)
        act_record.setCheckable(True)
        act_record.setShortcut("R")
        act_record.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_record.toggled.connect(self.set_record_enabled)
        tb.addAction(act_record)

        tb.addSeparator()
        bpm_box = QSpinBox(self)
        bpm_box.setRange(30, 300)
        bpm_box.setValue(int(self._bpm))
        bpm_box.setSingleStep(1)
        bpm_box.setPrefix("BPM ")
        bpm_box.valueChanged.connect(self._set_bpm_int)
        tb.addWidget(bpm_box)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage(
            "R: Grabar | Teclado: notas | Flechas: mover playhead | Espacio: Play/Stop | Doble clic: pre-escucha"
        )

    def _reset_zoom(self) -> None:
        self.roll.beat_w = 80.0
        self.roll.scroll_x = 0
        self.roll.update()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_rendering()
        self._stop_playback(delete_file=True)
        self._play_timer.stop()
        super().closeEvent(event)

    def _is_audio_playing(self) -> bool:
        if self._main_play_obj is not None and _sa is not None:
            try:
                if self._main_play_obj.is_playing():
                    return True
            except Exception:
                pass
        return bool(self._winsound_is_playing) or (
            self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        )

    def _start_transport(self) -> None:
        # Arranca el avance del playhead desde la posición actual
        self._play_start_tick = int(self.roll.playhead_tick)
        self._play_elapsed.restart()
        if not self._play_timer.isActive():
            self._play_timer.start()
        self._transport_running = True

    def _maybe_stop_transport(self) -> None:
        # Solo paramos el playhead si no hay audio y no está armado Grabar
        if self._record_enabled:
            return
        if self._is_audio_playing():
            return
        self._play_timer.stop()
        self._transport_running = False

    def _stop_playback(self, delete_file: bool) -> None:
        # simpleaudio (si está en uso)
        if self._main_play_obj is not None and _sa is not None:
            try:
                self._main_play_obj.stop()
            except Exception:
                pass
            self._main_play_obj = None
            self._main_audio_bytes = None

        if self._prefer_winsound and _winsound is not None:
            try:
                # SND_PURGE es el stop "duro" para reproducción async
                _winsound.PlaySound(None, getattr(_winsound, "SND_PURGE", 0))
            except Exception:
                pass
            self._winsound_is_playing = False
        self._player.stop()
        self._maybe_stop_transport()
        if delete_file and self._current_wav_path:
            try:
                os.remove(self._current_wav_path)
            except OSError:
                pass
            self._current_wav_path = None

    def _stop_rendering(self) -> None:
        if self._render_worker is not None:
            self._render_worker.cancel()
        if self._render_thread is not None:
            self._render_thread.quit()
            self._render_thread.wait(500)
        self._render_thread = None
        self._render_worker = None

    def _stop_all_live_voices(self) -> None:
        if _sa is None:
            return
        voices = list(self._active_key_voices.values())
        self._active_key_voices.clear()
        for v in voices:
            try:
                v.stop()
            except Exception:
                pass

    def toggle_play(self) -> None:
        if self._is_audio_playing() or (self._render_thread is not None and self._render_thread.isRunning()):
            self.stop()
            return

        self.play_from_start()

    def play_from_start(self) -> None:
        # Cancelar cualquier reproducción/render anterior
        self.stop(keep_playhead=False)

        # Invalida resultados antiguos (por seguridad)
        self._render_generation += 1
        gen = self._render_generation

        # Play siempre desde el inicio
        self.roll.set_playhead_tick(0)
        self.roll.ensure_playhead_visible()
        self.roll.record_enabled = self._record_enabled

        # Cache: si no cambió nada, evitamos re-render y el audio arranca ya.
        key = self._compute_render_key()
        if self._cached_render_key == key and self._cached_render_path and os.path.exists(self._cached_render_path):
            self._start_main_playback(self._cached_render_path)
            self._start_transport()
            self.statusBar().showMessage(f"Reproduciendo ({int(self._bpm)} BPM)")
            return

        self.statusBar().showMessage(f"Renderizando audio… ({int(self._bpm)} BPM)")

        # Render en background para no congelar UI
        t = QThread(self)
        w = AudioRenderWorker(self.roll.notes, ppq=self.roll.ppq, bpm=self._bpm, generation=gen, render_key=key)
        w.moveToThread(t)
        t.started.connect(w.run)
        # Forzar QueuedConnection: siempre ejecutar en el hilo de UI.
        w.finished.connect(self._on_render_finished_ui, Qt.ConnectionType.QueuedConnection)
        w.cancelled.connect(self._on_render_cancelled_ui, Qt.ConnectionType.QueuedConnection)
        w.failed.connect(self._on_render_failed_ui, Qt.ConnectionType.QueuedConnection)
        w.finished.connect(t.quit)
        w.cancelled.connect(t.quit)
        w.failed.connect(t.quit)
        w.finished.connect(w.deleteLater)
        w.cancelled.connect(w.deleteLater)
        w.failed.connect(w.deleteLater)
        t.finished.connect(t.deleteLater)

        self._render_thread = t
        self._render_worker = w
        t.start()

    def stop(self, keep_playhead: bool = True) -> None:
        # Invalida callbacks de renders viejos
        self._render_generation += 1
        self._stop_all_live_voices()
        self._stop_rendering()
        self._stop_playback(delete_file=False)
        # Stop "real": detener transporte incluso si Grabar está armado
        self._play_timer.stop()
        self._transport_running = False
        try:
            self._play_elapsed.invalidate()
        except Exception:
            pass
        if not keep_playhead:
            self.roll.set_playhead_tick(0)
            self.roll.ensure_playhead_visible()
        self.statusBar().showMessage("Stop")

    @Slot(int, object, str)
    def _on_render_finished_ui(self, gen: int, key: object, path: str) -> None:
        if gen != self._render_generation:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        # Limpiar anterior
        if self._current_wav_path and self._current_wav_path != path:
            try:
                os.remove(self._current_wav_path)
            except OSError:
                pass
        self._current_wav_path = path
        # key llega como object por Qt, pero es un tuple nuestro.
        self._cached_render_key = key  # type: ignore[assignment]
        self._cached_render_path = path

        self._start_main_playback(path)
        # Empezar el playhead cuando el audio empieza (no durante el render)
        self._start_transport()
        self.statusBar().showMessage(f"Reproduciendo ({int(self._bpm)} BPM)")

        self._render_thread = None
        self._render_worker = None

    @Slot(int)
    def _on_render_cancelled_ui(self, gen: int) -> None:
        if gen != self._render_generation:
            return
        self.statusBar().showMessage("Render cancelado")
        self._maybe_stop_transport()
        self._render_thread = None
        self._render_worker = None

    @Slot(int, str)
    def _on_render_failed_ui(self, gen: int, err: str) -> None:
        if gen != self._render_generation:
            return
        self.statusBar().showMessage(f"Error audio: {err}")
        self._maybe_stop_transport()
        self._render_thread = None
        self._render_worker = None

    def _preview_pitch(self, pitch: int) -> None:
        try:
            # una nota corta (con cola)
            voice = _synth_piano_note(midi_to_freq(pitch), duration_s=0.18, sample_rate=44100)
            if voice is None:
                return

            # Si está disponible, esto permite superponer (acordes) sin bloquear.
            if _sa is not None:
                pcm = _floats_to_pcm16_mono(voice)
                _sa.play_buffer(pcm, 1, 2, 44100)
                return

            # Fallback: WAV temporal (sin SND_NOSTOP para que NO "espere").
            fd, path = tempfile.mkstemp(prefix="pianoroll_preview_", suffix=".wav")
            os.close(fd)
            _write_wav_mono_16bit(path, voice, 44100)
            self._preview_temp_paths.append(path)
            if self._prefer_winsound and _winsound is not None:
                _winsound.PlaySound(path, _winsound.SND_FILENAME | _winsound.SND_ASYNC)
            else:
                self._preview_effect.setSource(QUrl.fromLocalFile(path))
                self._preview_effect.play()

            QTimer.singleShot(8000, self._cleanup_preview_files)
        except Exception:
            return

    def _cleanup_preview_files(self) -> None:
        # Borra archivos viejos dejando el más reciente (por si aún está en uso)
        while len(self._preview_temp_paths) > 2:
            old = self._preview_temp_paths.popleft()
            try:
                os.remove(old)
            except OSError:
                pass

    def _wav_duration_ms(self, path: str) -> int:
        try:
            with wave.open(path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
            if rate <= 0:
                return 1000
            return int((frames / float(rate)) * 1000.0)
        except Exception:
            return 3000

    def _play_with_winsound(self, path: str) -> None:
        try:
            _winsound.PlaySound(path, _winsound.SND_FILENAME | _winsound.SND_ASYNC)  # type: ignore[union-attr]
            self._winsound_is_playing = True
            dur = self._wav_duration_ms(path)

            def mark_done() -> None:
                if self._current_wav_path == path:
                    self._winsound_is_playing = False
                    self._maybe_stop_transport()

            QTimer.singleShot(max(500, dur + 150), mark_done)
        except Exception as e:
            self._winsound_is_playing = False
            self.statusBar().showMessage(f"Error winsound: {e}")

    def _start_main_playback(self, path: str) -> None:
        # Preferir simpleaudio si está disponible: Stop siempre funciona.
        if _sa is not None:
            try:
                with wave.open(path, "rb") as wf:
                    ch = wf.getnchannels()
                    sw = wf.getsampwidth()
                    rate = wf.getframerate()
                    data = wf.readframes(wf.getnframes())
                if sw != 2:
                    raise RuntimeError(f"Sample width inesperado: {sw}")
                # Mantener bytes vivo por seguridad durante reproducción
                self._main_audio_bytes = data
                self._main_play_obj = _sa.play_buffer(self._main_audio_bytes, ch, sw, rate)
                dur = self._wav_duration_ms(path)

                def done() -> None:
                    # Si sigue siendo el mismo handle, lo limpiamos al terminar
                    if self._main_play_obj is None:
                        return
                    try:
                        if not self._main_play_obj.is_playing():
                            self._main_play_obj = None
                            self._main_audio_bytes = None
                            self._maybe_stop_transport()
                    except Exception:
                        self._main_play_obj = None
                        self._main_audio_bytes = None
                        self._maybe_stop_transport()

                QTimer.singleShot(max(500, dur + 150), done)
                return
            except Exception:
                self._main_play_obj = None
                self._main_audio_bytes = None

        # Fallbacks
        if self._prefer_winsound and _winsound is not None:
            self._play_with_winsound(path)
        else:
            self._player.setSource(QUrl.fromLocalFile(path))
            self._player.play()

    def _compute_render_key(self) -> tuple:
        # Determinista y suficientemente barato para cachear renders.
        notes_key = tuple(sorted((n.pitch, int(n.start), int(n.length)) for n in self.roll.notes))
        return (int(self.roll.ppq), float(self._bpm), notes_key)

    @Slot()
    def _on_player_error(self, *_args) -> None:
        # Si QtMultimedia falla, activar fallback en Windows
        try:
            err = self._player.errorString()
        except Exception:
            err = "Error desconocido"
        if os.name == "nt" and _winsound is not None:
            self._prefer_winsound = True
        self.statusBar().showMessage(f"Qt audio error: {err}")

    def _set_bpm_int(self, bpm: int) -> None:
        self._bpm = float(bpm)

    def set_record_enabled(self, enabled: bool) -> None:
        self._record_enabled = bool(enabled)
        self.roll.record_enabled = self._record_enabled
        self.roll.update()
        if self._record_enabled:
            # En grabación queremos que el playhead se mueva según BPM
            self._start_transport()
        else:
            self._maybe_stop_transport()
            # Al parar de grabar, volver al inicio
            self.roll.set_playhead_tick(0)
            self.roll.ensure_playhead_visible()
        self.statusBar().showMessage("Grabación ON" if self._record_enabled else "Grabación OFF")

    def _ticks_per_second(self) -> float:
        return float(self.roll.ppq) * (float(self._bpm) / 60.0)

    def _tick_playhead(self) -> None:
        if not self._transport_running:
            return
        # Avanza playhead basado en BPM, sin depender del backend de audio
        if not self._play_elapsed.isValid():
            return
        elapsed_s = self._play_elapsed.elapsed() / 1000.0
        tick = int(self._play_start_tick + elapsed_s * self._ticks_per_second())
        self.roll.set_playhead_tick(tick)
        self.roll.ensure_playhead_visible()

    def _note_key_map(self) -> dict[int, int]:
        """
        Mapeo de teclado QWERTY a semitonos.
        - Fila inferior (Z..M): una octava cromática
        - Fila superior (Q..U con números): otra octava
        """
        return {
            # Fila Z (C)
            Qt.Key.Key_Z: 0,
            Qt.Key.Key_S: 1,
            Qt.Key.Key_X: 2,
            Qt.Key.Key_D: 3,
            Qt.Key.Key_C: 4,
            Qt.Key.Key_V: 5,
            Qt.Key.Key_G: 6,
            Qt.Key.Key_B: 7,
            Qt.Key.Key_H: 8,
            Qt.Key.Key_N: 9,
            Qt.Key.Key_J: 10,
            Qt.Key.Key_M: 11,
            Qt.Key.Key_Comma: 12,
            Qt.Key.Key_L: 13,
            Qt.Key.Key_Period: 14,
            Qt.Key.Key_Semicolon: 15,
            Qt.Key.Key_Slash: 16,
            # Fila Q (una octava arriba)
            Qt.Key.Key_Q: 12,
            Qt.Key.Key_2: 13,
            Qt.Key.Key_W: 14,
            Qt.Key.Key_3: 15,
            Qt.Key.Key_E: 16,
            Qt.Key.Key_R: 17,
            Qt.Key.Key_5: 18,
            Qt.Key.Key_T: 19,
            Qt.Key.Key_6: 20,
            Qt.Key.Key_Y: 21,
            Qt.Key.Key_7: 22,
            Qt.Key.Key_U: 23,
            Qt.Key.Key_I: 24,
        }

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        et = event.type()
        if et == event.Type.KeyPress:
            if event.isAutoRepeat():
                return False
            key = int(event.key())

            # mover playhead
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End):
                self._handle_playhead_keys(event)
                return True

            # transposición/octava
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if key == Qt.Key.Key_Up:
                    self._kb_base_pitch = min(120, self._kb_base_pitch + 12)
                    self.statusBar().showMessage(f"Octava base: {midi_note_name(self._kb_base_pitch)}")
                    return True
                if key == Qt.Key.Key_Down:
                    self._kb_base_pitch = max(0, self._kb_base_pitch - 12)
                    self.statusBar().showMessage(f"Octava base: {midi_note_name(self._kb_base_pitch)}")
                    return True

            # grabación: teclas de notas
            if self._record_enabled:
                mp = self._note_key_map()
                if key in mp:
                    pitch = max(0, min(127, self._kb_base_pitch + mp[key]))
                    self._start_record_note(qt_key=key, pitch=pitch)
                    return True

            # pre-escucha sin grabar (Alt + tecla)
            if (event.modifiers() & Qt.KeyboardModifier.AltModifier) != 0:
                mp = self._note_key_map()
                if key in mp:
                    pitch = max(0, min(127, self._kb_base_pitch + mp[key]))
                    self._preview_pitch(pitch)
                    return True

        if et == event.Type.KeyRelease:
            if event.isAutoRepeat():
                return False
            key = int(event.key())
            if self._record_enabled and key in self._active_key_notes:
                self._finish_record_note(qt_key=key)
                return True

        return super().eventFilter(obj, event)

    def _handle_playhead_keys(self, event) -> None:
        key = int(event.key())
        mods = event.modifiers()
        step = self.roll.quant
        if mods & Qt.KeyboardModifier.ShiftModifier:
            step = self.roll.ppq  # 1 beat
        if mods & Qt.KeyboardModifier.ControlModifier:
            step = self.roll.ppq * 4  # 1 compás (4/4)

        if key == Qt.Key.Key_Home:
            self.roll.set_playhead_tick(0)
            self.roll.ensure_playhead_visible()
            return
        if key == Qt.Key.Key_End:
            self.roll.set_playhead_tick(self.roll.ppq * self.roll.total_beats)
            self.roll.ensure_playhead_visible()
            return

        if key == Qt.Key.Key_Left:
            self.roll.set_playhead_tick(max(0, self.roll.playhead_tick - step))
            self.roll.ensure_playhead_visible()
            return
        if key == Qt.Key.Key_Right:
            self.roll.set_playhead_tick(self.roll.playhead_tick + step)
            self.roll.ensure_playhead_visible()
            return

    def _start_record_note(self, qt_key: int, pitch: int) -> None:
        if qt_key in self._active_key_notes:
            return
        start = max(0, self.roll._quantize_tick(int(self.roll.playhead_tick)))
        n = Note(pitch=pitch, start=start, length=self.roll.quant, selected=True)
        # selección: solo la nueva
        for note in self.roll.notes:
            note.selected = False
        self.roll.notes.append(n)
        idx = len(self.roll.notes) - 1
        self._active_key_notes[qt_key] = idx
        self._start_live_voice(qt_key=qt_key, pitch=pitch)
        self.roll.set_playhead_tick(start)
        self.roll.ensure_playhead_visible()
        self.roll.update()

    def _finish_record_note(self, qt_key: int) -> None:
        idx = self._active_key_notes.pop(qt_key, None)
        if idx is None or idx < 0 or idx >= len(self.roll.notes):
            return
        note = self.roll.notes[idx]
        end = max(note.start + self.roll.quant, self.roll._quantize_tick(int(self.roll.playhead_tick)))
        note.length = max(self.roll.quant, end - note.start)
        self._stop_live_voice(qt_key=qt_key)
        self.roll.update()

    def _start_live_voice(self, qt_key: int, pitch: int) -> None:
        # Para acordes reales: simpleaudio permite varias voces simultáneas.
        if _sa is None:
            self._preview_pitch(pitch)
            return
        try:
            voice = _synth_piano_note(midi_to_freq(pitch), duration_s=3.0, sample_rate=44100)
            if voice is None:
                return
            pcm = _floats_to_pcm16_mono(voice)
            play_obj = _sa.play_buffer(pcm, 1, 2, 44100)
            self._active_key_voices[qt_key] = play_obj
        except Exception:
            self._preview_pitch(pitch)

    def _stop_live_voice(self, qt_key: int) -> None:
        if _sa is None:
            return
        play_obj = self._active_key_voices.pop(qt_key, None)
        if play_obj is None:
            return
        try:
            play_obj.stop()
        except Exception:
            pass


def main() -> int:
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(1200, 650)
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

