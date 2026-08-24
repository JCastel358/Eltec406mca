Eltec 406MCA Emitter Tester v4 - assets folder
==============================================

Logo
----
Place the company logo here as:

    eltec_logo.png

The tester looks here first, then walks up parent folders for an
assets\eltec_logo.png (the shared repo-root assets\ already has one), so
this copy is optional. If no logo is found anywhere, the app falls back
to a drawn vector ELTEC mark.

Brand fonts (optional, recommended)
-----------------------------------
The v4 UI is styled after eltecinstruments.com, which uses the Poppins /
Manrope display faces and JetBrains Mono for technical readouts. Drop any
of these TrueType files into a fonts\ subfolder here and the app will
load them privately at startup (no install needed, Windows only):

    assets\fonts\Poppins-SemiBold.ttf
    assets\fonts\Poppins-Regular.ttf
    assets\fonts\Manrope-Regular.ttf
    assets\fonts\JetBrainsMono-Regular.ttf

If the fonts are missing the app falls back to Segoe UI / Consolas and
still looks correct.

Desktop shortcut icon
---------------------
"Create Desktop Shortcut.ps1" builds eltec_logo.ico from eltec_logo.png
automatically using built-in Windows imaging (no Pillow / pip install
needed) and points the desktop shortcut at it. Re-run that script any
time the logo changes or the app folder moves.
