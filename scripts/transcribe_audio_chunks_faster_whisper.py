from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import imageio_ffmpeg
from faster_whisper import WhisperModel


def _json_default(value):
    return str(value)


def _split_audio(input_path: Path, chunks_dir: Path, chunk_seconds: int, force: bool) -> list[Path]:
    chunks_dir.mkdir(parents=True, exist_ok=True)
    pattern = chunks_dir / "chunk_%04d.wav"
    existing = sorted(chunks_dir.glob("chunk_*.wav"))
    if existing and not force:
        return existing
    if force and chunks_dir.exists():
        for old in chunks_dir.glob("chunk_*.wav"):
            old.unlink()

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-f",
        "segment",
        "-segment_time",
        str(int(chunk_seconds)),
        "-c",
        "copy",
        str(pattern),
    ]
    subprocess.run(cmd, check=True)
    return sorted(chunks_dir.glob("chunk_*.wav"))


def _load_done(jsonl_path: Path) -> set[int]:
    done: set[int] = set()
    if not jsonl_path.exists():
        return done
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            done.add(int(payload["chunk_index"]))
        except Exception:
            continue
    return done


def _rewrite_txt(jsonl_path: Path, txt_path: Path) -> None:
    rows = []
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    rows.sort(key=lambda r: (int(r["chunk_index"]), float(r.get("start", 0))))
    lines = []
    for row in rows:
        lines.append(
            "[{start:07.2f}-{end:07.2f}] {text}".format(
                start=float(row["start"]),
                end=float(row["end"]),
                text=str(row.get("text", "")).strip(),
            )
        )
    txt_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Incrementally transcribe audio chunks with faster-whisper.")
    parser.add_argument("input", help="Input audio path, preferably 16k mono wav.")
    parser.add_argument("--out-dir", default="data/runtime/transcripts", help="Output directory.")
    parser.add_argument("--model", default="tiny", help="Whisper model size or path.")
    parser.add_argument("--language", default="zh", help="Language code.")
    parser.add_argument("--device", default="cpu", help="Device, e.g. cpu or cuda.")
    parser.add_argument("--compute-type", default="int8", help="Compute type.")
    parser.add_argument("--chunk-seconds", type=int, default=60, help="Chunk size in seconds.")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size.")
    parser.add_argument("--force-split", action="store_true", help="Re-split chunks.")
    parser.add_argument("--force-transcribe", action="store_true", help="Ignore existing jsonl progress.")
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    chunks_dir = out_dir / f"{stem}_chunks_{int(args.chunk_seconds)}s"
    jsonl_path = out_dir / f"{stem}.chunked.transcript.jsonl"
    txt_path = out_dir / f"{stem}.chunked.transcript.txt"
    manifest_path = out_dir / f"{stem}.chunked.manifest.json"

    if args.force_transcribe and jsonl_path.exists():
        jsonl_path.unlink()
    if args.force_transcribe and txt_path.exists():
        txt_path.unlink()

    started = time.time()
    chunks = _split_audio(input_path, chunks_dir, int(args.chunk_seconds), bool(args.force_split))
    done = _load_done(jsonl_path)

    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    with jsonl_path.open("a", encoding="utf-8") as out:
        for idx, chunk_path in enumerate(chunks):
            if idx in done:
                continue
            chunk_started = time.time()
            offset = idx * int(args.chunk_seconds)
            segments, info = model.transcribe(
                str(chunk_path),
                language=args.language,
                beam_size=max(1, int(args.beam_size)),
                vad_filter=False,
                condition_on_previous_text=False,
            )
            segment_count = 0
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                payload = {
                    "chunk_index": idx,
                    "chunk_path": str(chunk_path),
                    "start": round(offset + float(segment.start), 2),
                    "end": round(offset + float(segment.end), 2),
                    "text": text,
                    "language": info.language,
                }
                out.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
                out.flush()
                segment_count += 1
            status = {
                "chunk_index": idx,
                "chunks_total": len(chunks),
                "segments": segment_count,
                "elapsed_sec": round(time.time() - chunk_started, 1),
                "overall_elapsed_sec": round(time.time() - started, 1),
            }
            print(json.dumps(status, ensure_ascii=False), flush=True)
            _rewrite_txt(jsonl_path, txt_path)

    _rewrite_txt(jsonl_path, txt_path)
    manifest = {
        "ok": True,
        "input": str(input_path),
        "chunks": len(chunks),
        "chunk_seconds": int(args.chunk_seconds),
        "model": args.model,
        "jsonl": str(jsonl_path),
        "txt": str(txt_path),
        "elapsed_sec": round(time.time() - started, 1),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
