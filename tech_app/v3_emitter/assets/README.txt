Eltec 406MCA Emitter Tester - assets folder
============================================

Place the company logo here as:

    eltec_logo.png

The emitter tester loads this file for the in-app header logo and the
window icon. If the file is missing, the app falls back to a drawn
vector ELTEC logo, so the app still runs without it.

Desktop shortcut icon:

    "Create Desktop Shortcut.ps1" builds eltec_logo.ico from eltec_logo.png
    automatically using built-in Windows imaging (no Pillow / pip install
    needed) and points the desktop shortcut at it. Re-run that script any
    time the logo changes or the app folder moves.
