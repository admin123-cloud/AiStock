from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from faster_whisper import WhisperModel


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe a local video/audio file with faster-whisper.")
    parser.add_argument("input", help="Input video or audio path.")
    parser.add_argument("--out-dir", default="data/runtime/transcripts", help="Output directory.")
    parser.add_argument("--model", default="small", help="Whisper model size or path.")
    parser.add_argument("--language", default="zh", help="Language code, e.g. zh.")
    parser.add_argument("--device", default="cpu", help="Device, e.g. cpu or cuda.")
    parser.add_argument("--compute-type", default="int8", help="Compute type, e.g. int8 or float16.")
    parser.add_argument("--beam-size", type=int, default=5, help="Beam size. Use 1 for faster transcription.")
    parser.add_argument("--vad-filter", action="store_true", help="Enable VAD filter.")
    parser.add_argument(
        "--condition-on-previous-text",
        action="store_true",
        help="Condition each segment on previous text. Better coherence, slower and sometimes repetitive.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    segments, info = model.transcribe(
        str(input_path),
        language=args.language,
        vad_filter=bool(args.vad_filter),
        beam_size=max(1, int(args.beam_size)),
        condition_on_previous_text=bool(args.condition_on_previous_text),
    )

    rows = []
    lines = []
    for segment in segments:
        row = {
            "start": round(float(segment.start), 2),
            "end": round(float(segment.end), 2),
            "text": segment.text.strip(),
        }
        rows.append(row)
        lines.append("[{start:07.2f}-{end:07.2f}] {text}".format(**row))

    stem = input_path.stem
    json_path = out_dir / f"{stem}.transcript.json"
    txt_path = out_dir / f"{stem}.transcript.txt"
    json_path.write_text(
        json.dumps(
            {
                "input": str(input_path),
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
                "model": args.model,
                "segments": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "input": str(input_path),
                "language": info.language,
                "duration": info.duration,
                "segments": len(rows),
                "elapsed_sec": round(time.time() - started, 1),
                "json": str(json_path),
                "txt": str(txt_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
