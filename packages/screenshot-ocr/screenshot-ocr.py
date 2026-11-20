"""OCR screenshots on-device (macOS Vision) into markdown for qmd.

Usage: screenshot-ocr [SRC] [OUT]

    SRC  screenshots dir (default ~/Screenshots)
    OUT  markdown dir (default ~/.local/share/screenshots/md)

Writes OUT/YYYY/MM/<image name>.md with the capture time, image path and
recognized text. An md carries its image's mtime,
so only new or changed images are OCRed. Deleted images keep their md.
OCR runs in a small Swift program (macOS Vision has no Python API here).
The first run builds it with the Xcode Command Line Tools and keeps the
binary in ~/.cache/screenshot-ocr; later runs reuse it.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

UNREADABLE = "cannot read image"

# Prints one JSON object per image: {"path", "lines"} or {"path", "error"}.
SWIFT = r"""
import AppKit
import Foundation
import Vision

func emit(_ obj: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: obj)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

for path in CommandLine.arguments.dropFirst() {
    autoreleasepool {
        guard let image = NSImage(contentsOfFile: path),
              let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil)
        else {
            emit(["path": path, "error": "cannot read image"])
            return
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.automaticallyDetectsLanguage = true
        request.usesLanguageCorrection = true
        do {
            try VNImageRequestHandler(cgImage: cg).perform([request])
            let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            emit(["path": path, "lines": lines])
        } catch {
            emit(["path": path, "error": "\(error)"])
        }
    }
}
"""

# "Screenshot 2026-11-20 at 17.11.44.png", 12-hour locales add " PM".
NAME_TIME = re.compile(
    r"(\d{4}-\d{2}-\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})(?:\s*([AP]M))?", re.I
)


def helper():
    digest = hashlib.sha256(SWIFT.encode()).hexdigest()[:12]
    cache = Path.home() / ".cache" / "screenshot-ocr"
    binary = cache / f"ocr-{digest}"
    if binary.exists():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    src = cache / f"ocr-{digest}.swift"
    src.write_text(SWIFT, encoding="utf-8")
    tmp = binary.with_suffix(".tmp")
    subprocess.run(
        ["/usr/bin/xcrun", "swiftc", "-O", "-o", str(tmp), str(src)],
        check=True,
        timeout=60 * 5,
    )
    os.replace(tmp, binary)
    return binary


def taken(image):
    m = NAME_TIME.search(image.name)
    if m:
        day, hour, minute, second, ampm = m.groups()
        hour = int(hour)
        if ampm:
            hour = hour % 12 + (12 if ampm.upper() == "PM" else 0)
        try:
            return datetime.fromisoformat(f"{day}T{hour:02d}:{minute}:{second}")
        except ValueError:
            pass
    st = image.stat()
    return datetime.fromtimestamp(getattr(st, "st_birthtime", st.st_mtime))


def render(image, when, lines):
    front = {"taken": when.isoformat(), "image": str(image)}
    out = ["---"]
    out += [f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items()]
    out += ["---", "", f"# Screenshot {when:%Y-%m-%d %H:%M}", ""]
    out += lines or ["(no text recognized)"]
    return "\n".join(out) + "\n"


def target(out, image):
    when = taken(image)
    return when, out / f"{when:%Y}" / f"{when:%m}" / f"{image.name}.md"


def write(image, when, dst, lines):
    mtime = image.stat().st_mtime
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_text(render(image, when, lines), encoding="utf-8")
    os.utime(tmp, (mtime, mtime))
    os.replace(tmp, dst)


def main(src, out):
    # Always exists afterwards, so qmd can add it as a collection.
    out.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        print(f"screenshot-ocr: {src} missing, nothing to do")
        return
    todo = {}
    for image in sorted(src.iterdir()):
        # Hidden files: .DS_Store, and macOS's in-progress captures.
        if image.name.startswith(".") or not image.is_file():
            continue
        when, dst = target(out, image)
        if dst.exists() and dst.stat().st_mtime == image.stat().st_mtime:
            continue
        todo[str(image)] = (image, when, dst)
    if not todo:
        print("screenshot-ocr: wrote 0 files")
        return

    binary = helper()
    written = 0
    # One helper run per file: a crash or hang skips only that file,
    # which is retried next run.
    for path, (image, when, dst) in todo.items():
        try:
            res = subprocess.run(
                [str(binary), path], capture_output=True, text=True, timeout=60 * 2
            )
            r = json.loads(res.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"screenshot-ocr: {image}: {e}", file=sys.stderr)
            continue
        if "error" in r:
            print(f"screenshot-ocr: {image}: {r['error']}", file=sys.stderr)
            # Not an image: write an empty md so it isn't retried every
            # run. Other errors (Vision failures) retry next run.
            if r["error"] != UNREADABLE:
                continue
        write(image, when, dst, r.get("lines", []))
        written += 1
    print(f"screenshot-ocr: wrote {written} files")


def run():
    args = sys.argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return
    if len(args) > 2:
        sys.exit(__doc__)
    home = Path.home()
    src = Path(args[0]).expanduser() if args else home / "Screenshots"
    out = (
        Path(args[1]).expanduser()
        if len(args) > 1
        else home / ".local" / "share" / "screenshots" / "md"
    )
    main(src, out)


if __name__ == "__main__":
    run()
