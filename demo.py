import json
import math
from pathlib import Path
import struct
import sys
import zlib

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from creator_report.models import ChannelReport
from creator_report.reports import render_report_bundle


def synthetic_waveform(path):
    width, height = 640, 360
    pixels = bytearray()
    for y in range(height):
        pixels.append(0)
        for x in range(width):
            amplitude = 35 + 90 * abs(math.sin(x / 43)) * abs(math.sin(x / 91))
            grid = x % 40 == 0 or y % 40 == 0
            waveform = x % 12 < 7 and abs(y - height / 2) < amplitude
            pixels.extend((35, 155, 105) if waveform else ((213, 219, 222) if grid else (242, 246, 246)))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(pixels))) + chunk(b"IEND", b"")
    )


def main():
    fixture = json.loads((ROOT / "fixtures" / "synthetic-channel.json").read_text(encoding="utf-8"))
    report = ChannelReport.model_validate(fixture)
    output = ROOT / "demo-output"
    synthetic_waveform(output / "assets" / "media" / "synthetic-waveform.png")
    render_report_bundle(report, output, ROOT / "templates")
    transcripts = (output / "full_transcripts.md").read_text(encoding="utf-8")
    assert "SYNTHETIC TRANSCRIPT" in transcripts and "SYNTHETIC MODEL OUTPUT" not in transcripts
    assert (output / "assets" / "fonts" / "OFL.txt").is_file()
    assert (output / "assets" / "vendor" / "LUCIDE-LICENSE.txt").is_file()
    print(json.dumps({
        "data": "SYNTHETIC ONLY", "networkRequests": 0, "modelCalls": 0,
        "output": "demo-output/index.html",
        "transcriptAndSummarySeparated": True, "bundledAssetNotices": True,
    }, indent=2))


if __name__ == "__main__":
    main()
