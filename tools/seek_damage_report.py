#!/usr/bin/env python3
"""Seek-damage report for recorded files (.ts or .mp4).

Scans each file's video packet timeline for gaps/missing frames - the root cause of
Plex FF/RW freezes (see changelog 2026-07-17) - using the same assess_seek_damage()
the post-processor uses to decide copy vs re-encode. Optionally (--idr) decodes short
sampled windows to check whether I-frames there are true IDR seek points.

Usage:
    python3 tools/seek_damage_report.py FILE [FILE ...]
    python3 tools/seek_damage_report.py --idr /dvr-complete/*.mp4
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.probe import assess_seek_damage  # noqa: E402


def idr_check(filepath: str, span: float, windows: int = 5, window_len: int = 30):
    """Decode `windows` sampled windows; report I-frames that are NOT IDR per window."""
    ffprobe = shutil.which('ffprobe') or 'ffprobe'
    results = []
    for i in range(windows):
        t = span * (i + 0.5) / windows
        cmd = [ffprobe, '-v', 'error', '-read_intervals', f'{t:.0f}%+{window_len}',
               '-select_streams', 'v:0', '-show_entries', 'frame=key_frame,pict_type',
               '-of', 'csv=p=0', filepath]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=180).stdout
        except subprocess.TimeoutExpired:
            results.append((t, None, None))
            continue
        i_frames = idr = 0
        for line in out.splitlines():
            parts = line.strip().split(',')
            if len(parts) < 2:
                continue
            if parts[1] == 'I':
                i_frames += 1
                if parts[0] == '1':
                    idr += 1
        results.append((t, i_frames, idr))
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('files', nargs='+')
    ap.add_argument('--idr', action='store_true',
                    help='also decode sampled windows to validate I-frames are IDR (slow)')
    args = ap.parse_args()

    exit_code = 0
    for f in args.files:
        print(f'\n=== {f}')
        damaged, metrics, summary = assess_seek_damage(f)
        print(f'    {summary}')
        if not metrics:
            exit_code = 2
            continue
        if damaged:
            exit_code = max(exit_code, 1)
        print(f"    span={metrics['span_seconds']:.1f}s packets={metrics['packet_count']} "
              f"fps={metrics['fps']:.2f} deficit={metrics['deficit_seconds']:.1f}s "
              f"gaps>{0.25}s: n={metrics['gap_count']} total={metrics['gap_seconds']:.1f}s "
              f"max={metrics['max_gap_seconds']:.2f}s")
        if args.idr:
            for t, i_frames, idr in idr_check(f, metrics['span_seconds']):
                if i_frames is None:
                    print(f'    IDR check @ {t:7.0f}s: TIMED OUT')
                else:
                    flag = '' if (i_frames and idr == i_frames) else '   ← BAD SEEK ZONE' if i_frames else '   ← NO I-FRAMES'
                    print(f'    IDR check @ {t:7.0f}s: {idr}/{i_frames} I-frames are IDR{flag}')
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
