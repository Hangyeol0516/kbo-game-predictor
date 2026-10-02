#!/usr/bin/env python3
"""WAL 안전 온라인 백업, 무결성 확인, 새 경로로 복원한다."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from artifact_io import atomic_write_json
from kbo_analysis import calibrator_model_version, valid_calibrator

TABLES = ("analysis_snapshots", "game_predictions", "game_results", "value_bet_predictions",
          "collector_odds_slots", "prediction_events")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def database_info(path: Path) -> dict:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite 무결성 검사 실패")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SQLite 참조 무결성 검사 실패")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"game_predictions", "game_results"}.issubset(tables):
            raise ValueError("PLAYBALL 저장 테이블이 없습니다.")
        return {"schemaVersion": connection.execute("PRAGMA user_version").fetchone()[0],
                "rows": {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                         for table in TABLES if table in tables}}


def verify_backup(directory: str | Path) -> dict:
    folder = Path(directory)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    files = manifest.get("files", {})
    if (manifest.get("formatVersion") != 1 or not isinstance(files, dict)
            or "playball.db" not in files or not set(files).issubset({"playball.db", "calibration.json"})):
        raise ValueError("백업 manifest 형식이 잘못됐습니다.")
    for name, expected in files.items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"백업 파일 검증 실패: {name}")
    info = database_info(folder / "playball.db")
    if info != manifest.get("database"):
        raise ValueError("백업 DB 메타데이터가 일치하지 않습니다.")
    if "calibration.json" in files:
        calibration = json.loads((folder / "calibration.json").read_text(encoding="utf-8"))
        if not isinstance(calibration, dict):
            raise ValueError("보정 파일은 JSON 객체여야 합니다.")
    return manifest


def create_backup(db: str | Path, calibration: str | Path, directory: str | Path) -> dict:
    source, folder = Path(db), Path(directory)
    if not source.is_file():
        raise ValueError("백업할 DB 파일이 없습니다.")
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(folder / "playball.db")) as writer:
                reader.backup(writer, pages=256, sleep=0.05)
                writer.execute("PRAGMA journal_mode=DELETE")
        os.chmod(folder / "playball.db", 0o600)
        info = database_info(folder / "playball.db")
        files = {"playball.db": digest(folder / "playball.db")}
        model_version = None
        calibration_path = Path(calibration)
        if calibration_path.is_file():
            payload = json.loads(calibration_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("보정 파일은 JSON 객체여야 합니다.")
            atomic_write_json(folder / "calibration.json", payload)
            files["calibration.json"] = digest(folder / "calibration.json")
            model_version = calibrator_model_version(payload) if valid_calibrator(payload) else None
        manifest = {"formatVersion": 1, "startedAt": started,
                    "completedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "database": info, "calibrationVersion": model_version, "files": files}
        atomic_write_json(folder / "manifest.json", manifest)
        return verify_backup(folder)
    except Exception:
        shutil.rmtree(folder)
        raise


def restore_backup(directory: str | Path, destination: str | Path) -> dict:
    manifest = verify_backup(directory)
    target = Path(destination)
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        for name in manifest["files"]:
            shutil.copyfile(Path(directory) / name, target / name)
            os.chmod(target / name, 0o600)
            if digest(target / name) != manifest["files"][name]:
                raise ValueError(f"복원한 파일 체크섬 불일치: {name}")
        if database_info(target / "playball.db") != manifest["database"]:
            raise ValueError("복원한 DB 검증 실패")
    except Exception:
        shutil.rmtree(target)
        raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="실행 중 DB를 새 백업 폴더에 저장")
    create.add_argument("directory")
    create.add_argument("--db", default=os.environ.get("PLAYBALL_DB_PATH", "data/playball.db"))
    create.add_argument("--calibration", default=os.environ.get("PLAYBALL_CALIBRATION_PATH", "data/calibration.json"))
    verify = commands.add_parser("verify", help="체크섬·DB 무결성·행 수 확인")
    verify.add_argument("directory")
    restore = commands.add_parser("restore", help="검증 후 새 빈 경로로 복원")
    restore.add_argument("directory")
    restore.add_argument("destination")
    args = parser.parse_args()
    try:
        if args.command == "create":
            report = create_backup(args.db, args.calibration, args.directory)
        elif args.command == "verify":
            report = verify_backup(args.directory)
        else:
            report = restore_backup(args.directory, args.destination)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(f"백업 작업 실패: {exc}") from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
