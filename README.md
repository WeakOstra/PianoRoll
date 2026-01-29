# Piano Roll (Qt + Python)

Piano roll sencillo hecho con **Python** y **Qt** (PySide6).

## Requisitos

- Python 3.10+ (recomendado)

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Ejecutar

```bash
python main.py
```

## Controles

- **Rueda del mouse**: scroll vertical
- **Shift + rueda**: scroll horizontal
- **Ctrl + rueda**: zoom horizontal (tiempo)
- **Clic y arrastrar (en la grilla)**: crear nota
- **Arrastrar una nota**: moverla (tiempo y pitch)
- **Supr / Delete**: borrar nota seleccionada
- **Clic derecho sobre nota**: borrar nota
- **Doble clic (en nota o grilla)**: pre-escucha del pitch (piano sintético)
- **Play** (botón): reproduce desde el inicio
- **Stop** (botón): para reproducción/render
- **Espacio**: Play/Stop
- **Ctrl + 0**: reset zoom
- **R**: activar/desactivar **Grabar**
- **Flechas izquierda/derecha**: mover el **playhead**
  - **Shift + flechas**: mover 1 beat
  - **Ctrl + flechas**: mover 1 compás (4 beats)
- **Ctrl + arriba/abajo**: cambiar **octava base** del teclado
- **Alt + teclas de nota**: pre-escucha desde el teclado (sin grabar)

## Grabación con teclado (PC)

Con **Grabar (R)** activado, puedes “tocar” notas con el teclado:

- Fila inferior: `Z S X D C V G B H N J M` (cromática)
- Fila superior: `Q 2 W 3 E R 5 T 6 Y 7 U` (otra octava arriba)

## Notas

- El widget dibuja teclas de piano a la izquierda y la grilla a la derecha.
- El audio es un **piano sintético** simple (armónicos + envolvente), sin MIDI externo.
- El tempo actual está fijo a **120 BPM** (se puede extender fácilmente).
- En **Windows**, la reproducción usa `winsound` para máxima compatibilidad (y evitar casos donde QtMultimedia no suena).
- Para **acordes / polifonía** al tocar con el teclado en modo **Grabar**, se usa `simpleaudio` (incluido en `requirements.txt`).

