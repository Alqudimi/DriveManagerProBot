from __future__ import annotations

import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import Video
from aiohttp import web

from drive_telegram_manager.config import Settings
from drive_telegram_manager.drive.filesystem import DriveFilesystem
from drive_telegram_manager.indexing.csv_index import CsvIndex
from drive_telegram_manager.jobs.manager import Job, JobManager
from drive_telegram_manager.telegram.bot import (
    ProgressInputFile,
    _create_directory_zip,
    _find_index_rows,
    _media_details,
    _resolve_user_path,
    _split_pair,
    _upload_keyboard,
    build_bot,
    make_dispatcher,
)


def test_mv_parser_supports_quoted_paths():
    assert _split_pair('/mv "folder/a file.txt" "new name.txt"', "mv") == ("folder/a file.txt", "new name.txt")
    assert _split_pair('/move "a.txt" => "folder with spaces"', "move") == ("a.txt", "folder with spaces")


def test_video_is_treated_as_an_uploadable_media_file():
    file_id, unique_id, size, name = _media_details(
        Video(
            file_id="video-id",
            file_unique_id="video-unique-id",
            width=1920,
            height=1080,
            duration=12,
            file_name="رحلة.mp4",
            file_size=2048,
        )
    )
    assert (file_id, unique_id, size, name) == ("video-id", "video-unique-id", 2048, "رحلة.mp4")


def test_upload_keyboard_exposes_next_action_for_each_stage():
    assert _upload_keyboard("collecting").inline_keyboard[0][0].callback_data == "up:done"
    assert _upload_keyboard("description").inline_keyboard[0][0].callback_data == "up:skip"
    assert _upload_keyboard("confirm").inline_keyboard[0][0].callback_data == "up:confirm"


def test_relative_drive_paths_stay_inside_allowed_root(tmp_path: Path):
    root = tmp_path / "drive"
    root.mkdir()
    fs = DriveFilesystem([root])
    assert _resolve_user_path(fs, root, "folder/file.txt") == root / "folder" / "file.txt"
    with pytest.raises(ValueError):
        _resolve_user_path(fs, root, "../secret")


def test_rename_move_copy_and_csv_path_update(tmp_path: Path):
    root = tmp_path / "drive"
    source_dir = root / "from"
    target_dir = root / "to"
    source_dir.mkdir(parents=True)
    target_dir.mkdir()
    source = source_dir / "old.txt"
    source.write_bytes(b"data" * 500)
    index = CsvIndex(source_dir / "_index.csv")
    index.append(source, group_id="group")

    fs = DriveFilesystem([root])
    renamed = fs.rename(source, "new name.txt")
    assert renamed.read_bytes() == b"data" * 500
    assert index.update_path(source, renamed) == 1
    assert index.rows()[0]["drive_path"] == str(renamed)

    moved = Path(fs.move(renamed, target_dir))
    assert moved.exists() and not renamed.exists()
    assert index.update_path(renamed, moved) == 1

    copied = target_dir / "copy.txt"
    assert fs.copy_streaming(moved, copied) == moved.stat().st_size
    assert copied.read_bytes() == moved.read_bytes()


def test_delete_status_and_indexed_search(tmp_path: Path):
    root = tmp_path / "drive"
    root.mkdir()
    target = root / "annual-report.pdf"
    target.write_bytes(b"pdf")
    index = CsvIndex(root / "_drive_telegram_index.csv")
    index.append(target, group_id="g", description="annual financial report")
    assert _find_index_rows(root, "_drive_telegram_index.csv", "financial")[0]["file_name"] == target.name
    assert index.mark_deleted(target) == 1
    assert index.rows()[0]["status"] == "deleted"


def test_dispatcher_and_local_file_endpoints(tmp_path: Path):
    settings = Settings(
        bot_token="123:placeholder",
        telegram_api_id=1,
        telegram_api_hash="placeholder",
        drive_root=tmp_path,
        temp_dir=tmp_path / "tmp",
        _env_file=None,
    )
    bot = build_bot(settings)
    assert bot.session.api.base == "http://127.0.0.1:8081/bot{token}/{method}"
    assert bot.session.api.file == "http://127.0.0.1:8081/file/bot{token}/{path}"
    assert make_dispatcher(settings) is not None


@pytest.mark.asyncio
async def test_progress_input_streams_in_bounded_chunks(tmp_path: Path):
    source = tmp_path / "large-simulated.bin"
    source.write_bytes(b"x" * (2 * 1024 * 1024 + 100_000))
    progress = []

    async def on_progress(completed: int, total: int) -> None:
        progress.append((completed, total))

    upload = ProgressInputFile(source, source.name, on_progress)
    chunks = [chunk async for chunk in upload.read(None)]
    assert b"".join(chunks) == source.read_bytes()
    assert len(chunks) > 1
    assert progress[-1] == (source.stat().st_size, source.stat().st_size)


@pytest.mark.asyncio
async def test_bot_get_me_is_sent_to_configured_local_api(tmp_path: Path):
    requested_paths: list[str] = []
    received_uploads: list[bytes] = []

    async def get_me(request: web.Request) -> web.Response:
        requested_paths.append(request.path)
        return web.json_response(
            {"ok": True, "result": {"id": 123456, "is_bot": True, "first_name": "Test", "username": "local_test_bot"}}
        )

    async def send_message(request: web.Request) -> web.Response:
        requested_paths.append(request.path)
        return web.json_response(
            {"ok": True, "result": {"message_id": 2, "date": 1, "chat": {"id": 42, "type": "private"}, "text": "hello"}}
        )

    async def send_document(request: web.Request) -> web.Response:
        requested_paths.append(request.path)
        received_uploads.append(await request.read())
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "message_id": 3,
                    "date": 1,
                    "chat": {"id": 42, "type": "private"},
                    "document": {"file_id": "f", "file_unique_id": "u", "file_name": "out.bin", "file_size": 4},
                },
            }
        )

    async def get_file(request: web.Request) -> web.Response:
        requested_paths.append(request.path)
        return web.json_response(
            {
                "ok": True,
                "result": {
                    "file_id": "f",
                    "file_unique_id": "u",
                    "file_size": 4,
                    "file_path": str(source),
                },
            }
        )

    app = web.Application()
    app.router.add_post("/bot{token}/getMe", get_me)
    app.router.add_post("/bot{token}/sendMessage", send_message)
    app.router.add_post("/bot{token}/sendDocument", send_document)
    app.router.add_post("/bot{token}/getFile", get_file)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    settings = Settings(
        bot_token="123:placeholder",
        telegram_api_id=1,
        telegram_api_hash="placeholder",
        telegram_local_api_url=f"http://127.0.0.1:{port}",
        drive_root=tmp_path,
        temp_dir=tmp_path / "tmp",
        _env_file=None,
    )
    bot = build_bot(settings)
    source = tmp_path / "out.bin"
    source.write_bytes(b"data")
    received = tmp_path / "received.bin"
    try:
        result = await bot.get_me()
        assert result.id == 123456
        await bot.send_message(42, "hello")
        from aiogram.types import FSInputFile

        await bot.send_document(42, document=FSInputFile(source))
        file = await bot.get_file("f")
        await bot.download_file(file.file_path, destination=received)
        assert received.read_bytes() == b"data"
        assert received_uploads and b"data" in received_uploads[0]
        assert requested_paths == [
            "/bot123:placeholder/getMe",
            "/bot123:placeholder/sendMessage",
            "/bot123:placeholder/sendDocument",
            "/bot123:placeholder/getFile",
        ]
    finally:
        await bot.session.close()
        await runner.cleanup()


def test_zip_directory_excludes_index_and_symlink(tmp_path: Path):
    root = tmp_path / "drive"
    folder = root / "archive"
    folder.mkdir(parents=True)
    (folder / "ok.txt").write_text("inside")
    (folder / "_drive_telegram_index.csv").write_text("index")
    outside = tmp_path / "secret.txt"
    outside.write_text("not for archive")
    try:
        (folder / "outside-link").symlink_to(outside)
    except OSError:
        pass
    temp = tmp_path / "temp"
    temp.mkdir()
    archive = temp / "bundle.zip"
    uncompressed = _create_directory_zip(folder, archive, "_drive_telegram_index.csv", temp)
    assert uncompressed == len("inside")
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.namelist() == ["archive/ok.txt"]


def test_join_rejects_part_path_escape_and_existing_output(tmp_path: Path):
    import json

    from drive_telegram_manager.transfer.chunking import join_parts

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"parts": [{"name": "../outside", "sha256": "x"}], "size_bytes": 1}))
    with pytest.raises(ValueError, match="غير آمن"):
        join_parts(manifest, tmp_path, tmp_path / "joined")
    manifest.write_text(
        json.dumps(
            {"parts": [], "size_bytes": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
        )
    )
    existing = tmp_path / "already.txt"
    existing.write_text("keep")
    with pytest.raises(FileExistsError):
        join_parts(manifest, tmp_path, existing)


def test_job_journal_marks_interrupted_work_for_manual_recovery(tmp_path: Path):
    journal = tmp_path / "jobs.json"
    manager = JobManager(journal)
    queued = Job(job_id="a123", owner_id=42, kind="upload", status="running")
    manager.jobs[queued.job_id] = queued
    manager._persist()

    restored = JobManager(journal)
    assert restored.jobs["a123"].status == "needs_recovery"
    assert "تحقق من الوجهة" in restored.jobs["a123"].error


def test_colab_mount_calls_official_drive_mount(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from drive_telegram_manager.drive.mount import mount_drive

    calls = []
    fake_drive = types.SimpleNamespace(mount=lambda path, force_remount=False: calls.append((path, force_remount)))
    fake_colab = types.ModuleType("google.colab")
    fake_colab.drive = fake_drive
    fake_google = types.ModuleType("google")
    fake_google.__path__ = []
    fake_google.colab = fake_colab
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.colab", fake_colab)

    result = mount_drive(tmp_path / "drive", force_remount=True)
    assert result == tmp_path / "drive"
    assert calls == [(str(tmp_path / "drive"), True)]


@pytest.mark.asyncio
async def test_browse_builds_inline_buttons_for_files_and_folders(tmp_path: Path):
    root = tmp_path / "drive"
    root.mkdir()
    (root / "folder").mkdir()
    (root / "report.txt").write_text("data")
    settings = Settings(
        bot_token="123:placeholder",
        telegram_api_id=1,
        telegram_api_hash="placeholder",
        admin_ids="42",
        drive_root=root,
        temp_dir=tmp_path / "temp",
        _env_file=None,
    )
    dispatcher = make_dispatcher(settings)
    browse = next(h.callback for h in dispatcher.sub_routers[0].message.handlers if h.callback.__name__ == "browse")

    class FakeMessage:
        from_user = SimpleNamespace(id=42)
        text = "/browse"
        markup = None

        async def answer(self, text, reply_markup=None):
            self.markup = reply_markup

    message = FakeMessage()
    await browse(message)
    labels = [button.text for row in message.markup.inline_keyboard for button in row]
    callbacks = [button.callback_data for row in message.markup.inline_keyboard for button in row]
    assert any(label.endswith("folder") for label in labels)
    assert any(label.endswith("report.txt") for label in labels)
    assert all(value.startswith("br:") for value in callbacks)
