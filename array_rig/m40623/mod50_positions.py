"""Detector-box labels, separate from the fixed 50-channel acquisition map."""


def normalize_box_side(value):
    if not isinstance(value, str) or value not in ("top", "bottom"):
        raise ValueError("Choose Top or Bottom of the detector box.")
    return value


def detector_position(position, box_side="top"):
    """Translate a canonical wiring slot (1-1..5-10) to its box label."""
    side = normalize_box_side(box_side)
    row, column = map(int, position.split("-"))
    if not (1 <= row <= 5 and 1 <= column <= 10):
        raise ValueError("Expected a wiring slot from 1-1 through 5-10.")
    return f"{row + (5 if side == 'bottom' else 0)}-{column}"
