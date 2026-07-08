"""Isolated Human Bingo OCR worker.

Runs ONE card scan in its own process. onnxruntime (under RapidOCR) holds the
GIL while it builds its models and spins up a CPU-sized thread pool, which can
freeze a whole Python process for seconds on a busy box. Doing it here — a
separate process with its own GIL and interpreter — means it can NEVER freeze
the bot's event loop or its watchdog. If this process hangs, the bot kills it
and stays fully responsive.

Invoked by the bot as:  python ocr_worker.py <sheet_no>   (image bytes on stdin)
Prints a JSON read_submission result ({"cells": [...]}) to stdout.
"""

import json
import sys


def main():
    try:
        sheet_no = int(sys.argv[1])
    except (IndexError, ValueError):
        sys.stdout.write(json.dumps({"cells": []}))
        return

    image_bytes = sys.stdin.buffer.read()

    import bingo_ocr
    from setup import sheets

    # Build the roster index here (blocking Google-Sheets fetch is fine in this
    # throwaway process — it can't block the bot).
    members = []
    try:
        for og_members in sheets.load_year1_members().values():
            members.extend(og_members)
    except Exception as exc:
        sys.stderr.write(f"roster load failed: {exc}\n")  # match nothing, still scan

    index = bingo_ocr.build_roster_index(members)
    result = bingo_ocr.read_submission(sheet_no, image_bytes, index)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
