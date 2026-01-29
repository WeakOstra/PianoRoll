# Piano Roll (Qt + Python)

A simple piano roll built with **Python** + **Qt** (PySide6).

## Requirements

- Python 3.10+ (recommended)

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

## Controls

- **Mouse wheel**: vertical scroll
- **Shift + wheel**: horizontal scroll
- **Ctrl + wheel**: horizontal zoom (time)
- **Left click + drag (grid)**: create a note
- **Drag a note**: move it (time + pitch)
- **Delete / Backspace**: delete selected note(s)
- **Right click on a note**: delete note
- **Double click (note or grid)**: pitch preview (synthetic piano)
- **Play** (button): play from start
- **Stop** (button): stop playback / stop render
- **Space**: toggle Play/Stop
- **Ctrl + 0**: reset zoom
- **R**: toggle **Record**
- **Left/Right arrows**: move the **playhead**
  - **Shift + arrows**: move 1 beat
  - **Ctrl + arrows**: move 1 bar (4 beats)
- **Ctrl + Up/Down**: change keyboard base octave
- **Alt + note keys**: keyboard preview (without recording)

## Keyboard recording (PC)

With **Record (R)** enabled, you can “play” notes using your computer keyboard:

- Bottom row: `Z S X D C V G B H N J M` (chromatic)
- Top row: `Q 2 W 3 E R 5 T 6 Y 7 U` (one octave up)

## Notes

- The widget draws piano keys on the left and a grid on the right.
- Audio is a simple **synthetic piano** (harmonics + envelope). No external MIDI.
- BPM is adjustable from the toolbar.
- On **Windows**, some setups can be flaky with QtMultimedia; this project includes fallbacks.
- For **chords / polyphony** when playing from the keyboard in **Record** mode, we use `simpleaudio` (included in `requirements.txt`).

