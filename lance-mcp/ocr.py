"""OCR one PNG with macOS Vision (Chinese-simplified + traditional + English).
Used only at ingest time on the local Mac; the server never imports this.
"""
from __future__ import annotations

from Foundation import NSData  # type: ignore
from Quartz import CIImage        # type: ignore
import Vision                    # type: ignore

_REQ = Vision.VNRecognizeTextRequest.alloc().init()
_REQ.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
_REQ.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en"])
_REQ.setUsesLanguageCorrection_(True)
_REQ.setRevision_(Vision.VNRecognizeTextRequestRevision3)


def ocr_png(data: bytes) -> str:
    """Return recognized text joined by newlines. Empty string if Vision finds
    nothing (pure diagram, no label text)."""
    ci = CIImage.imageWithData_(NSData.dataWithBytes_length_(data, len(data)))
    if ci is None:
        return ""
    handler = Vision.VNImageRequestHandler.alloc().initWithCIImage_options_(ci, None)
    ok, err = handler.performRequests_error_([_REQ], None)
    if not ok:
        return ""
    out: list[str] = []
    for obs in _REQ.results() or []:
        c = obs.topCandidates_(1)
        if c and c[0].string():
            out.append(str(c[0].string()))
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        with open(p, "rb") as f:
            print(f"=== {p} ===")
            print(ocr_png(f.read())[:600])
