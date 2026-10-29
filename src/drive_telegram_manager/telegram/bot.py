from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Audio,
    BotCommand,
    CallbackQuery,
    Document,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    Video,
)

from drive_telegram_manager.config import Settings
from drive_telegram_manager.drive.filesystem import DriveFilesystem, safe_name, unique_destination
from drive_telegram_manager.indexing.csv_index import CsvIndex
from drive_telegram_manager.jobs.manager import JobManager
from drive_telegram_manager.transfer.chunking import join_parts, sha256_file, split_file

log = logging.getLogger(__name__)
MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📂 استعراض Drive"), KeyboardButton(text="🔎 البحث")],
        [KeyboardButton(text="📤 رفع ملفات"), KeyboardButton(text="📥 تنزيل ملف")],
        [KeyboardButton(text="📁 إنشاء مجلد"), KeyboardButton(text="📊 العمليات")],
        [KeyboardButton(text="❓ المساعدة")],
    ],
    resize_keyboard=True,
)
TELEGRAM_UPLOAD_LIMIT = 2_000_000_000
_PENDING_DELETE: dict[str, tuple[int, Path]] = {}
_UPLOAD_SESSIONS: dict[int, dict] = {}
_DOWNLOAD_SESSIONS: dict[int, dict] = {}
_BROWSE_TOKENS: dict[str, tuple[int, str, Path, int]] = {}


def _upload_keyboard(stage: str) -> InlineKeyboardMarkup:
    if stage == "collecting":
        rows = [
            [InlineKeyboardButton(text="✅ انتهيت من إرسال الملفات", callback_data="up:done")],
            [InlineKeyboardButton(text="📂 تغيير مجلد الحفظ", callback_data="up:folder")],
            [InlineKeyboardButton(text="❌ إلغاء جلسة الرفع", callback_data="up:cancel")],
        ]
    elif stage in {"description", "note"}:
        rows = [
            [InlineKeyboardButton(text="⏭️ تخطي هذه الخطوة", callback_data="up:skip")],
            [InlineKeyboardButton(text="❌ إلغاء جلسة الرفع", callback_data="up:cancel")],
        ]
    elif stage == "confirm":
        rows = [
            [InlineKeyboardButton(text="🚀 تأكيد ورفع إلى Drive", callback_data="up:confirm")],
            [InlineKeyboardButton(text="➕ إضافة ملفات أخرى", callback_data="up:more")],
            [InlineKeyboardButton(text="❌ إلغاء جلسة الرفع", callback_data="up:cancel")],
        ]
    else:
        rows = [[InlineKeyboardButton(text="📋 القائمة الرئيسية", callback_data="up:menu")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _media_details(media: Document | Video | Audio | PhotoSize) -> tuple[str, str, int | None, str]:
    if isinstance(media, Video):
        default_name = f"video-{uuid4().hex[:8]}.mp4"
    elif isinstance(media, Audio):
        default_name = f"audio-{uuid4().hex[:8]}.mp3"
    elif isinstance(media, PhotoSize):
        default_name = f"photo-{uuid4().hex[:8]}.jpg"
    else:
        default_name = f"upload-{uuid4().hex[:8]}.bin"
    name = safe_name(getattr(media, "file_name", None) or default_name)
    return media.file_id, media.file_unique_id, getattr(media, "file_size", None), name


class ProgressInputFile(FSInputFile):
    """Read a local file in chunks and report bytes passed to the HTTP request stream."""

    def __init__(self, path: Path, filename: str, progress: Callable[[int, int], Awaitable[None]]):
        super().__init__(path, filename=filename, chunk_size=1024 * 1024)
        self.progress = progress

    async def read(self, bot: Bot):
        total = self.path.stat().st_size
        completed = 0
        with self.path.open("rb") as stream:
            while chunk := await asyncio.to_thread(stream.read, self.chunk_size):
                yield chunk
                completed += len(chunk)
                await self.progress(completed, total)


def authorized(message: Message, settings: Settings) -> bool:
    return message.from_user is not None and message.from_user.id in settings.admin_id_set


def build_bot(settings: Settings) -> Bot:
    server = TelegramAPIServer.from_base(settings.telegram_local_api_url, is_local=True)
    if settings.telegram_local_file_url:
        server.file = settings.telegram_local_file_url.rstrip("/") + "/file/bot{token}/{path}"
    return Bot(token=settings.bot_token.get_secret_value(), session=AiohttpSession(api=server))


def _args(text: str) -> list[str]:
    try:
        return shlex.split(text.partition(" ")[2])
    except ValueError as exc:
        raise ValueError("صيغة علامات الاقتباس غير صحيحة") from exc


def _resolve_user_path(fs: DriveFilesystem, root: Path, value: str) -> Path:
    supplied = Path(value).expanduser()
    return fs.resolve(supplied if supplied.is_absolute() else root / supplied)


def _split_pair(raw: str, command: str) -> tuple[str, str]:
    body = raw.partition(" ")[2].strip()
    if "=>" in body:
        left, right = body.split("=>", 1)
        try:
            left_parts, right_parts = shlex.split(left), shlex.split(right)
        except ValueError as exc:
            raise ValueError("صيغة علامات الاقتباس غير صحيحة") from exc
        if len(left_parts) == 1 and len(right_parts) == 1:
            return left_parts[0], right_parts[0]
    try:
        parts = shlex.split(body)
    except ValueError as exc:
        raise ValueError("استخدم علامات اقتباس حول المسارات التي تحتوي مسافات") from exc
    if len(parts) != 2:
        raise ValueError(f'الصيغة: /{command} "مسار المصدر" "القيمة الجديدة"')
    return parts[0], parts[1]


def _find_index_rows(root: Path, index_filename: str, query: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [directory for directory in dirs if not directory.startswith(".")]
        if index_filename not in files:
            continue
        for row in CsvIndex(Path(folder) / index_filename).rows():
            searchable = " ".join(row.get(key, "") for key in ("file_name", "description", "note", "drive_path"))
            if query in searchable.casefold():
                matches.append(row)
                if len(matches) >= 30:
                    return matches
    return matches


def _create_directory_zip(target: Path, archive: Path, index_filename: str, temp_dir: Path) -> int:
    import zipfile

    total = 0
    for item in target.rglob("*"):
        if item.is_symlink() or not item.is_file() or item.name == index_filename:
            continue
        if item == temp_dir or temp_dir in item.parents:
            continue
        resolved_item = item.resolve()
        try:
            resolved_item.relative_to(target.resolve())
        except ValueError:
            continue
        total += resolved_item.stat().st_size
    if shutil.disk_usage(archive.parent).free < total + 64 * 1024 * 1024:
        raise OSError(f"مساحة Colab غير كافية لإنشاء ZIP. المطلوب تقريبًا {total:,} بايت")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for original in target.rglob("*"):
            if original.is_symlink() or not original.is_file() or original.name == index_filename:
                continue
            if original == temp_dir or temp_dir in original.parents:
                continue
            resolved = original.resolve()
            try:
                resolved.relative_to(target.resolve())
            except ValueError:
                continue
            zf.write(resolved, original.relative_to(target.parent))
    return total


def make_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    router = Router()
    fs = DriveFilesystem(settings.allowed_roots)
    journal = (
        settings.drive_root / ".drive-telegram-manager" / "jobs.json"
        if settings.drive_root.is_dir()
        else settings.temp_dir / "jobs.json"
    )
    jobs = JobManager(journal, limit=settings.max_concurrent_jobs)

    def root_path() -> Path:
        root = fs.resolve(settings.drive_root)
        if not root.is_dir():
            raise FileNotFoundError("جذر Drive غير متاح؛ اربط Google Drive في Colab أولًا.")
        return root

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not authorized(message, settings):
            await message.answer("عذرًا، هذا البوت خاص بالمستخدمين المصرح لهم فقط.")
            return
        await message.answer(
            "مرحبًا بك في مدير Google Drive. اختر وظيفة أو اكتب /help لعرض أوامر إدارة الملفات.", reply_markup=MENU
        )

    @router.message(Command("help"))
    @router.message(F.text == "❓ المساعدة")
    async def help_message(message: Message) -> None:
        if not authorized(message, settings):
            return
        await message.answer(
            "الأوامر (المسارات النسبية تبدأ من DRIVE_ROOT):\n"
            "/browse [مجلد] — استعراض 15 عنصرًا في الصفحة بأزرار التنقل.\n"
            "/upload ثم اختيار المجلد وإرسال الفيديو أو الملفات — استخدم أزرار إنهاء الجمع والتخطي والتأكيد الظاهرة في كل خطوة.\n"
            "/search كلمة — البحث بالاسم.\n"
            '/download — اختيار ملفات متعددة من المتصفح، ثم /download_done؛ أو /download "مسار" مباشرة.\n'
            "/cancel_download — إلغاء اختيار التنزيل الجاري.\n"
            '/mv "مسار الملف" "الاسم الجديد" — إعادة تسمية داخل مجلد Drive نفسه.\n'
            '/move "المصدر" "مجلد الوجهة" — نقل عنصر إلى مجلد آخر.\n'
            '/cp "الملف" "مجلد الوجهة" — نسخ ملف تدفقيًا.\n'
            '/rm "المسار" — حذف ملف أو مجلد فارغ بعد تأكيد.\n'
            '/mkdir "الاسم" — إنشاء مجلد في DRIVE_ROOT.\n'
            "/index [مسار المجلد] — عرض سجل CSV الموجود؛ /index_sync [مجلد] لمزامنة ملفات المجلد دون حذف السجلات.\n"
            '/info "المسار" — تفاصيل الملف وحجمه وSHA-256.\n'
            '/join "مسار manifest.json" [اسم الناتج] — إعادة تجميع الأجزاء في Drive.\n'
            "/find كلمة — البحث في فهارس CSV.\n"
            "/jobs و/cancel <معرف> و/status.\n"
            'مثال: /mv "تقارير 2025/قديم.pdf" "نهائي.pdf"'
        )

    @router.message(Command("menu"))
    async def menu(message: Message) -> None:
        if authorized(message, settings):
            await message.answer("القائمة الرئيسية:", reply_markup=MENU)

    @router.message(Command("status"))
    @router.message(F.text == "📊 العمليات")
    async def status(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            mounted = settings.drive_root.exists()
            free = shutil.disk_usage(
                settings.temp_dir if settings.temp_dir.exists() else settings.drive_mount_point.parent
            ).free
            await message.answer(
                f"جذر Drive: {'متاح' if mounted else 'غير متاح'}\nالمسار: {settings.drive_root}\nالمساحة المحلية الحرة: {free:,} بايت\nخادم Telegram المحلي: {settings.telegram_local_api_url}"
            )
        except OSError as exc:
            await message.answer(f"تعذر فحص الحالة: {exc}")

    async def render_browse(
        message: Message, owner_id: int, folder: Path, page: int = 0, *, edit: bool = False
    ) -> None:
        root = root_path()
        folder = fs.resolve(folder)
        current_root = next((allowed for allowed in fs.roots if folder == allowed or allowed in folder.parents), root)
        for token, details in list(_BROWSE_TOKENS.items()):
            if details[0] == owner_id:
                _BROWSE_TOKENS.pop(token, None)

        def token(action: str, path: Path, target_page: int = 0) -> str:
            value = uuid4().hex[:12]
            _BROWSE_TOKENS[value] = (owner_id, action, path, target_page)
            return value

        items = await asyncio.to_thread(fs.list_dir, folder, page, 15)
        keyboard: list[list[InlineKeyboardButton]] = []
        upload_session = _UPLOAD_SESSIONS.get(owner_id)
        if folder == root:
            for shared_root in fs.roots:
                if shared_root != root and shared_root.is_dir():
                    key = token("open_root", shared_root)
                    keyboard.append(
                        [InlineKeyboardButton(text=f"☁️ {shared_root.name[:90]}", callback_data=f"br:{key}")]
                    )
        if upload_session and upload_session["stage"] == "collecting":
            key = token("choose_upload_folder", folder)
            keyboard.append([InlineKeyboardButton(text="📥 اختر هذا المجلد للرفع", callback_data=f"br:{key}")])
        for item in items:
            action = (
                "open" if item.is_dir() else ("select_download" if owner_id in _DOWNLOAD_SESSIONS else "send_download")
            )
            key = token(action, item)
            label = ("📁 " if item.is_dir() else "📄 ") + item.name[:100]
            keyboard.append([InlineKeyboardButton(text=label, callback_data=f"br:{key}")])
        navigation: list[InlineKeyboardButton] = []
        if folder != current_root:
            key = token("up", folder.parent)
            navigation.append(InlineKeyboardButton(text="⬆️ الأعلى", callback_data=f"br:{key}"))
        if page > 0:
            key = token("page", folder, page - 1)
            navigation.append(InlineKeyboardButton(text="⬅️ السابقة", callback_data=f"br:{key}"))
        next_items = await asyncio.to_thread(fs.list_dir, folder, page + 1, 15)
        if next_items:
            key = token("page", folder, page + 1)
            navigation.append(InlineKeyboardButton(text="التالية ➡️", callback_data=f"br:{key}"))
        if navigation:
            keyboard.append(navigation)
        text = f"المجلد: {folder}\nالصفحة: {page + 1}\nاختر مجلدًا لفتحه أو ملفًا لتنزيله."
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None
        if edit:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)

    @router.message(Command("browse"))
    @router.message(F.text == "📂 استعراض Drive")
    async def browse(message: Message) -> None:
        if not authorized(message, settings):
            return
        args = _args(message.text or "/browse") if (message.text or "").startswith("/") else []
        try:
            root = root_path()
            page = int(args.pop()) if args and args[-1].isdigit() else 0
            folder = _resolve_user_path(fs, root, " ".join(args)) if args else root
            await render_browse(message, message.from_user.id, folder, page)
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر استعراض Drive: {exc}")

    @router.callback_query(F.data.startswith("br:"))
    async def browse_callback(query: CallbackQuery, bot: Bot) -> None:
        token_id = (query.data or "").partition(":")[2]
        details = _BROWSE_TOKENS.pop(token_id, None)
        if not details or details[0] != query.from_user.id:
            await query.answer("انتهت صلاحية هذا الزر أو لا يخصك.", show_alert=True)
            return
        _, action, path, page = details
        if not isinstance(query.message, Message):
            await query.answer("تعذر الوصول إلى رسالة المتصفح.", show_alert=True)
            return
        try:
            if action in {"open", "open_root", "up", "page"}:
                await render_browse(query.message, query.from_user.id, path, page, edit=True)
            elif action == "choose_upload_folder":
                session = _UPLOAD_SESSIONS.get(query.from_user.id)
                if not session or session["stage"] != "collecting":
                    raise ValueError("لا توجد جلسة رفع قابلة لتحديد الوجهة")
                session["destination"] = str(fs.resolve(path))
                await query.message.answer(f"حُدد مجلد الحفظ: {session['destination']}")
            elif action == "select_download":
                session = _DOWNLOAD_SESSIONS.get(query.from_user.id)
                if not session or not path.is_file():
                    raise ValueError("جلسة التنزيل غير متاحة أو أن العنصر لم يعد ملفًا")
                if str(path) not in session["files"]:
                    session["files"].append(str(path))
                await query.message.answer(
                    f"أُضيف الملف إلى قائمة التنزيل ({len(session['files'])}). استخدم /download_done عند الانتهاء."
                )
            elif action == "send_download":
                if not path.is_file():
                    raise FileNotFoundError("الملف لم يعد متاحًا")
                await send_drive_path(query.message, bot, path, owner_id=query.from_user.id)
            await query.answer()
        except (OSError, ValueError) as exc:
            await query.answer(str(exc)[:180], show_alert=True)

    @router.message(F.text == "🔎 البحث")
    async def search_prompt(message: Message) -> None:
        if authorized(message, settings):
            await message.answer("استخدم /search كلمة_البحث")

    @router.message(Command("search"))
    async def search_command(message: Message) -> None:
        if not authorized(message, settings):
            return
        query = (message.text or "").partition(" ")[2].strip()
        if not query:
            await message.answer("استخدم: /search كلمة_البحث")
            return
        try:
            root = root_path()
            results = await asyncio.to_thread(lambda: list(fs.search(query, root, limit=30)))
            await message.answer("\n".join(str(p.relative_to(root))[:120] for p in results) or "لا توجد نتائج.")
        except (OSError, ValueError) as exc:
            await message.answer(f"فشل البحث: {exc}")

    @router.message(Command("mkdir"))
    @router.message(F.text == "📁 إنشاء مجلد")
    async def mkdir(message: Message) -> None:
        if not authorized(message, settings):
            return
        raw = message.text or ""
        if not raw.startswith("/"):
            await message.answer('استخدم /mkdir "اسم_المجلد"')
            return
        name = raw.partition(" ")[2].strip().strip('"')
        if not name:
            await message.answer('استخدم /mkdir "اسم_المجلد"')
            return
        try:
            target = fs.mkdir(root_path(), name)
            await message.answer(f"تم إنشاء المجلد: {target}")
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر إنشاء المجلد: {exc}")

    @router.message(Command("upload"))
    @router.message(F.text == "📤 رفع ملفات")
    async def upload_prompt(message: Message) -> None:
        if not authorized(message, settings):
            return
        user_id = message.from_user.id
        if user_id in _UPLOAD_SESSIONS:
            await message.answer(
                "لديك جلسة رفع قائمة. أرسل الملفات أولًا، ثم استخدم الزر المناسب أدناه.",
                reply_markup=_upload_keyboard("collecting"),
            )
            return
        _UPLOAD_SESSIONS[user_id] = {
            "stage": "collecting",
            "files": [],
            "destination": str(root_path()),
            "description": "",
            "note": "",
            "group_id": uuid4().hex,
        }
        await message.answer(
            f"الوجهة الحالية: {settings.drive_root}\nيمكنك اختيار مجلد آخر من المتصفح أدناه، "
            f"ثم أرسل حتى {settings.max_files_per_batch} ملفات. بعد كل ملف سأعرض لك حالته."
            " عند الانتهاء اضغط زر «انتهيت» أو أرسل /upload_done.",
            reply_markup=_upload_keyboard("collecting"),
        )
        await render_browse(message, user_id, root_path())

    async def show_upload_summary(message: Message, session: dict) -> None:
        session["stage"] = "confirm"
        rows = "\n".join(f"• {item['name'][:100]} — {item['size']:,} بايت" for item in session["files"][:20])
        await message.answer(
            f"ملخص الرفع إلى {session['destination']}:\n{rows}\n"
            f"الوصف: {session['description'] or '—'}\nالملاحظة: {session['note'] or '—'}\n"
            "راجع التفاصيل ثم اضغط «تأكيد ورفع إلى Drive». يمكنك إضافة ملفات أخرى أو إلغاء الجلسة.",
            reply_markup=_upload_keyboard("confirm"),
        )

    @router.message(Command("upload_done"))
    async def upload_done(message: Message) -> None:
        if not authorized(message, settings):
            return
        session = _UPLOAD_SESSIONS.get(message.from_user.id)
        if not session or session["stage"] != "collecting" or not session["files"]:
            await message.answer(
                "لا توجد ملفات مجمعة. ابدأ عبر /upload، ثم أرسل الفيديو أو الملف قبل إنهاء الجمع.",
                reply_markup=_upload_keyboard("collecting"),
            )
            return
        session["stage"] = "description"
        await message.answer(
            f"تم جمع {len(session['files'])} ملفًا. أرسل وصفًا مشتركًا اختياريًا أو اضغط «تخطي هذه الخطوة».",
            reply_markup=_upload_keyboard("description"),
        )

    @router.message(Command("skip"))
    async def skip_upload_field(message: Message) -> None:
        if not authorized(message, settings):
            return
        session = _UPLOAD_SESSIONS.get(message.from_user.id)
        if not session:
            return
        if session["stage"] == "description":
            session["stage"] = "note"
            await message.answer(
                "أرسل ملاحظة مشتركة اختيارية أو اضغط «تخطي هذه الخطوة».",
                reply_markup=_upload_keyboard("note"),
            )
        elif session["stage"] == "note":
            await show_upload_summary(message, session)

    @router.message(Command("cancel_upload"))
    async def cancel_upload(message: Message) -> None:
        if not authorized(message, settings):
            return
        session = _UPLOAD_SESSIONS.pop(message.from_user.id, None)
        if not session:
            await message.answer("لا توجد جلسة رفع لإلغائها.")
            return
        for item in session["files"]:
            Path(item["path"]).unlink(missing_ok=True)
        await message.answer(
            "أُلغيت الجلسة وحُذفت الملفات المؤقتة؛ لم تتغير ملفات Drive.", reply_markup=MENU
        )

    @router.message(Command("upload_confirm"))
    async def upload_confirm(message: Message) -> None:
        if not authorized(message, settings):
            return
        user_id = message.from_user.id
        session = _UPLOAD_SESSIONS.get(user_id)
        if not session or session["stage"] != "confirm":
            await message.answer("لا توجد دفعة جاهزة للتأكيد.")
            return
        results, failures, remaining = [], [], []
        try:
            root = fs.resolve(Path(session["destination"]))
            if not root.is_dir():
                raise NotADirectoryError("مجلد الوجهة لم يعد متاحًا في Drive")
            async with jobs.semaphore:
                for item in session["files"]:
                    source = Path(item["path"])
                    target = unique_destination(root / safe_name(item["name"]))
                    copied_to_drive = False
                    try:
                        size = await asyncio.to_thread(fs.copy_streaming, source, target)
                        if size != item["size"]:
                            target.unlink(missing_ok=True)
                            raise OSError("الحجم المحفوظ لا يطابق المتوقع")
                        try:
                            await asyncio.to_thread(
                                CsvIndex(root / settings.index_filename).append,
                                target,
                                group_id=session["group_id"],
                                description=session["description"],
                                note=session["note"],
                            )
                            copied_to_drive = True
                            results.append(f"✅ {target.name}")
                        except OSError:
                            results.append(f"✅ {target.name} — حُفظ لكن فشل تحديث CSV")
                            continue
                        try:
                            source.unlink(missing_ok=True)
                        except OSError:
                            log.warning("Could not remove staged upload %s", source.name)
                    except Exception as exc:
                        if not copied_to_drive:
                            target.unlink(missing_ok=True)
                        failures.append(f"❌ {item['name']}: {type(exc).__name__}")
                        remaining.append(item)
            await message.answer("نتيجة الدفعة:\n" + "\n".join(results + failures))
            if remaining:
                session["files"] = remaining
                await message.answer(
                    "أُبقيت الملفات الفاشلة محليًا. صحح المشكلة ثم اضغط «تأكيد ورفع» للمحاولة مجددًا أو ألغِ الجلسة.",
                    reply_markup=_upload_keyboard("confirm"),
                )
            else:
                _UPLOAD_SESSIONS.pop(user_id, None)
                for item in session["files"]:
                    Path(item["path"]).unlink(missing_ok=True)
                await message.answer("اكتمل الرفع بنجاح. يمكنك بدء دفعة جديدة من القائمة.", reply_markup=MENU)
        except (OSError, ValueError) as exc:
            await message.answer(
                f"تعذر بدء الرفع: {exc}. تبقى الملفات المؤقتة لمحاولة أخرى.",
                reply_markup=_upload_keyboard("confirm"),
            )

    @router.callback_query(F.data.startswith("up:"))
    async def upload_callback(query: CallbackQuery) -> None:
        if not isinstance(query.message, Message):
            await query.answer("تعذر الوصول إلى رسالة جلسة الرفع.", show_alert=True)
            return
        user_id = query.from_user.id
        action = (query.data or "").partition(":")[2]
        session = _UPLOAD_SESSIONS.get(user_id)
        try:
            if action == "menu":
                await query.message.answer("القائمة الرئيسية:", reply_markup=MENU)
            elif action == "folder":
                if not session or session["stage"] != "collecting":
                    raise ValueError("لا توجد جلسة جمع ملفات نشطة")
                await render_browse(query.message, user_id, Path(session["destination"]), edit=False)
            elif action == "done":
                if not session or session["stage"] != "collecting" or not session["files"]:
                    raise ValueError("أرسل ملفًا واحدًا على الأقل قبل إنهاء الجمع")
                session["stage"] = "description"
                await query.message.answer(
                    f"تم جمع {len(session['files'])} ملفًا. أرسل وصفًا مشتركًا اختياريًا أو اضغط «تخطي هذه الخطوة».",
                    reply_markup=_upload_keyboard("description"),
                )
            elif action == "skip":
                if not session or session["stage"] not in {"description", "note"}:
                    raise ValueError("لا توجد خطوة وصف أو ملاحظة نشطة")
                if session["stage"] == "description":
                    session["stage"] = "note"
                    await query.message.answer(
                        "أرسل ملاحظة مشتركة اختيارية أو اضغط «تخطي هذه الخطوة».",
                        reply_markup=_upload_keyboard("note"),
                    )
                else:
                    await show_upload_summary(query.message, session)
            elif action == "more":
                if not session or session["stage"] != "confirm":
                    raise ValueError("لا توجد دفعة جاهزة للإضافة")
                session["stage"] = "collecting"
                await query.message.answer(
                    "يمكنك الآن إرسال ملفات أخرى. عند الانتهاء اضغط الزر أو أرسل /upload_done.",
                    reply_markup=_upload_keyboard("collecting"),
                )
            elif action == "confirm":
                if not session or session["stage"] != "confirm":
                    raise ValueError("لا توجد دفعة جاهزة للتأكيد")
                await upload_confirm(query.message)
            elif action == "cancel":
                if not session:
                    raise ValueError("لا توجد جلسة رفع لإلغائها")
                _UPLOAD_SESSIONS.pop(user_id, None)
                for item in session["files"]:
                    Path(item["path"]).unlink(missing_ok=True)
                await query.message.answer("أُلغيت الجلسة ولم تتغير ملفات Drive.", reply_markup=MENU)
            else:
                raise ValueError("زر غير معروف")
            await query.answer("تم")
        except (OSError, ValueError) as exc:
            await query.answer(str(exc)[:180], show_alert=True)

    @router.message(F.text & ~F.text.startswith("/"))
    async def upload_metadata(message: Message) -> None:
        if not authorized(message, settings):
            return
        session = _UPLOAD_SESSIONS.get(message.from_user.id)
        if not session or session["stage"] not in {"description", "note"}:
            return
        text = (message.text or "").strip()
        if text.startswith("/"):
            await message.answer("أرسل النص أو استخدم /skip.")
        elif session["stage"] == "description":
            session["description"] = text
            session["stage"] = "note"
            await message.answer(
                "تم حفظ الوصف. أرسل ملاحظة مشتركة اختيارية أو اضغط «تخطي هذه الخطوة».",
                reply_markup=_upload_keyboard("note"),
            )
        else:
            session["note"] = text
            await show_upload_summary(message, session)

    @router.message(Command("info"))
    async def file_info(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            args = _args(message.text or "")
            if len(args) != 1:
                raise ValueError('الصيغة: /info "مسار الملف"')
            target = _resolve_user_path(fs, root_path(), args[0])
            if not target.exists():
                raise FileNotFoundError("العنصر غير موجود")
            stat = await asyncio.to_thread(target.stat)
            if target.is_file():
                digest = await asyncio.to_thread(sha256_file, target)
                await message.answer(
                    f"الاسم: {target.name}\nالمسار: {target}\nالحجم: {stat.st_size:,} بايت\nآخر تعديل: {stat.st_mtime}\nSHA-256: {digest}"
                )
            else:
                count = await asyncio.to_thread(lambda: sum(1 for _ in target.iterdir()))
                await message.answer(f"المجلد: {target}\nالعناصر المباشرة: {count}")
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر قراءة التفاصيل: {exc}")

    @router.message(Command("join"))
    async def reassemble(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            args = _args(message.text or "")
            if len(args) not in {1, 2}:
                raise ValueError('الصيغة: /join "مسار manifest.json" [اسم_الناتج]')
            root = root_path()
            manifest_path = _resolve_user_path(fs, root, args[0])
            if not manifest_path.is_file() or manifest_path.name != "manifest.json":
                raise ValueError("المسار يجب أن يشير إلى manifest.json موجود داخل Drive")
            manifest = await asyncio.to_thread(lambda: json.loads(manifest_path.read_text(encoding="utf-8")))
            output_name = safe_name(args[1] if len(args) == 2 else manifest.get("original_name", "reassembled.bin"))
            output = manifest_path.parent / output_name
            expected = int(manifest["size_bytes"])
            if output.exists():
                raise FileExistsError("يوجد ملف بهذا الاسم؛ اختر اسمًا آخر لتجنب الاستبدال")
            if shutil.disk_usage(output.parent).free < expected:
                raise OSError("المساحة المتاحة غير كافية لإعادة التجميع")
            result = await asyncio.to_thread(join_parts, manifest_path, manifest_path.parent, output)
            try:
                await asyncio.to_thread(
                    CsvIndex(result.parent / settings.index_filename).append,
                    result,
                    group_id=f"joined-{uuid4().hex}",
                    description="ملف أُعيد تجميعه من أجزاء والتحقق من SHA-256",
                    sha256=manifest["sha256"],
                )
            except OSError:
                await message.answer(f"اكتملت إعادة التجميع والتحقق: {result}، لكن فشل تحديث CSV.")
                return
            await message.answer(f"تمت إعادة التجميع والتحقق من SHA-256: {result} ({result.stat().st_size:,} بايت)")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            await message.answer(f"لم تكتمل إعادة التجميع: {exc}")

    @router.message(Command("mv"))
    async def rename(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            old, new = _split_pair(message.text or "", "mv")
            source_path = _resolve_user_path(fs, root_path(), old)
            target = await asyncio.to_thread(fs.rename, source_path, new)
            try:
                await asyncio.to_thread(
                    CsvIndex(source_path.parent / settings.index_filename).update_path, source_path, target
                )
            except OSError:
                await message.answer(f"تمت إعادة التسمية في Drive إلى {target.name}، لكن فشل تحديث سجل CSV.")
                return
            await message.answer(f"تمت إعادة التسمية داخل Google Drive:\n{old} → {target.name}")
        except (OSError, ValueError) as exc:
            await message.answer(f"لم تتم إعادة التسمية: {exc}")

    @router.message(Command("move"))
    async def move(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            source_text, dest_text = _split_pair(message.text or "", "move")
            root = root_path()
            source = _resolve_user_path(fs, root, source_text)
            source_index = CsvIndex(source.parent / settings.index_filename)
            result = await asyncio.to_thread(fs.move, source, _resolve_user_path(fs, root, dest_text))
            try:
                await asyncio.to_thread(source_index.update_path, source, Path(result))
            except OSError:
                await message.answer(f"تم النقل إلى Drive: {result}، لكن فشل تحديث سجل CSV.")
                return
            await message.answer(f"تم نقل العنصر داخل Drive إلى: {result}")
        except (OSError, ValueError) as exc:
            await message.answer(f"لم يتم النقل: {exc}")

    @router.message(Command("cp"))
    async def copy_file(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            source_text, dest_text = _split_pair(message.text or "", "cp")
            root = root_path()
            source = _resolve_user_path(fs, root, source_text)
            destination = _resolve_user_path(fs, root, dest_text)
            if not source.is_file() or not destination.is_dir():
                raise ValueError("النسخ متاح للملفات فقط، والوجهة يجب أن تكون مجلدًا")
            if shutil.disk_usage(destination).free < source.stat().st_size:
                raise OSError("المساحة المتاحة غير كافية لتنفيذ النسخ بأمان")
            target = unique_destination(destination / source.name)
            copied = await asyncio.to_thread(fs.copy_streaming, source, target)
            if copied != source.stat().st_size:
                target.unlink(missing_ok=True)
                raise OSError("الحجم المنسوخ لا يطابق حجم المصدر")
            try:
                await asyncio.to_thread(
                    CsvIndex(destination / settings.index_filename).append,
                    target,
                    group_id=f"copy-{uuid4().hex}",
                    description=f"نسخة من {source}",
                )
            except OSError:
                await message.answer(f"تم نسخ الملف إلى Drive: {target}، لكن فشل تحديث سجل CSV.")
                return
            await message.answer(f"تم نسخ الملف إلى Drive: {target}")
        except (OSError, ValueError) as exc:
            await message.answer(f"لم يتم النسخ: {exc}")

    @router.message(Command("rm"))
    async def remove_prompt(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            args = _args(message.text or "")
            if len(args) != 1:
                raise ValueError('الصيغة: /rm "مسار الملف"')
            target = _resolve_user_path(fs, root_path(), args[0])
            if target == root_path() or not target.exists():
                raise ValueError("العنصر غير موجود أو لا يمكن حذف جذر Drive")
            token = uuid4().hex[:12]
            _PENDING_DELETE[token] = (message.from_user.id, target)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="تأكيد الحذف", callback_data=f"rm:{token}"),
                        InlineKeyboardButton(text="إلغاء", callback_data=f"no:{token}"),
                    ]
                ]
            )
            await message.answer(
                f"تأكيد حذف هذا العنصر من Drive؟\n{target}\nالمجلدات غير الفارغة لا تحذف بهذا الأمر.",
                reply_markup=keyboard,
            )
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر طلب الحذف: {exc}")

    @router.callback_query(F.data.startswith("rm:"))
    async def remove_confirm(query: CallbackQuery) -> None:
        token = (query.data or "").partition(":")[2]
        pending = _PENDING_DELETE.pop(token, None)
        if not pending or query.from_user.id != pending[0]:
            await query.answer("هذا التأكيد غير صالح أو لا يخصك.", show_alert=True)
            return
        target = fs.resolve(pending[1])
        try:
            await asyncio.to_thread(target.rmdir if target.is_dir() else target.unlink)
            try:
                await asyncio.to_thread(CsvIndex(target.parent / settings.index_filename).mark_deleted, target)
                result = f"تم الحذف من Drive وتحديث CSV: {target}"
            except OSError:
                result = f"تم حذف العنصر من Drive: {target}، لكن فشل تحديث CSV."
            await query.message.edit_text(result)
            await query.answer("اكتمل الحذف")
        except OSError as exc:
            await query.message.edit_text(f"لم يتم الحذف: {exc}")
            await query.answer()

    @router.callback_query(F.data.startswith("no:"))
    async def remove_cancel(query: CallbackQuery) -> None:
        token = (query.data or "").partition(":")[2]
        pending = _PENDING_DELETE.pop(token, None)
        if not pending or query.from_user.id != pending[0]:
            await query.answer("هذا الطلب لا يخصك.", show_alert=True)
            return
        await query.message.edit_text("أُلغي طلب الحذف.")
        await query.answer()

    async def send_drive_path(message: Message, bot: Bot, target: Path, *, owner_id: int | None = None) -> bool:
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        owner_id = owner_id or (message.from_user.id if message.from_user else 0)
        job = jobs.create(owner_id, "download")
        task = asyncio.current_task()
        if task:
            jobs.tasks[job.job_id] = task
        acquired = False
        cleanup_path: Path | None = None
        succeeded = False
        try:
            await jobs.semaphore.acquire()
            acquired = True
            job.status = "running"
            jobs._persist()
            if target.is_dir():
                archive = settings.temp_dir / f"dtm-{job.job_id}-{safe_name(target.name)}.zip"
                await asyncio.to_thread(
                    _create_directory_zip, target, archive, settings.index_filename, settings.temp_dir
                )
                send_path = archive
                cleanup_path = archive
                display_name = archive.name
            elif target.is_file():
                send_path = target
                cleanup_path = None
                display_name = target.name
            else:
                raise FileNotFoundError("المسار ليس ملفًا أو مجلدًا قابلًا للإرسال")

            size = send_path.stat().st_size
            progress_message = await message.answer(f"📥 بدء إرسال {display_name} ({size:,} بايت).")
            started = time.monotonic()
            last_update = [started]

            def format_size(value: int) -> str:
                amount = float(value)
                for unit in ("B", "KB", "MB", "GB", "TB"):
                    if amount < 1024 or unit == "TB":
                        return f"{amount:.1f} {unit}"
                    amount /= 1024
                return f"{amount:.1f} TB"

            async def update_progress(completed: int, overall: int, *, force: bool = False) -> None:
                now = time.monotonic()
                if not force and now - last_update[0] < settings.progress_update_interval:
                    return
                last_update[0] = now
                ratio = min(1.0, completed / overall) if overall else 1.0
                elapsed = max(now - started, 0.001)
                speed = completed / elapsed
                remaining = max(0, overall - completed) / speed if speed else 0
                bar = "█" * int(ratio * 10) + "░" * (10 - int(ratio * 10))
                text = (
                    f"📥 إرسال {display_name}\n{bar} {ratio:.0%}\n"
                    f"المُمرر إلى الخادم المحلي: {format_size(completed)} / {format_size(overall)}\n"
                    f"السرعة التقريبية: {format_size(int(speed))}/ث\n"
                    f"الوقت المنقضي: {int(elapsed)}ث — المتبقي التقريبي: {int(remaining)}ث\n"
                    "الحالة: إرسال إلى Telegram (التقدم يقيس تدفق الملف من القرص إلى طلب API)."
                )
                try:
                    await progress_message.edit_text(text)
                except Exception:
                    log.debug("Unable to update Telegram transfer progress message")

            async def send_local_file(path: Path, filename: str, caption: str, offset: int, overall: int) -> None:
                async def on_read(completed: int, _file_size: int) -> None:
                    await update_progress(offset + completed, overall)

                await bot.send_document(
                    message.chat.id,
                    ProgressInputFile(path, filename, on_read),
                    caption=caption[:900],
                )

            if size <= TELEGRAM_UPLOAD_LIMIT:
                await send_local_file(send_path, display_name, display_name, 0, size)
                await update_progress(size, size, force=True)
            else:
                part_size = settings.transfer_chunk_size_mb * 1024 * 1024
                free_space = shutil.disk_usage(settings.temp_dir).free
                if free_space < size + 64 * 1024 * 1024:
                    raise OSError("المساحة المؤقتة غير كافية لإنشاء الأجزاء مع الحفاظ على الملف الأصلي")
                parts_dir = settings.temp_dir / f"dtm-parts-{job.job_id}"
                manifest = await asyncio.to_thread(split_file, send_path, parts_dir, part_size)
                manifest_path = parts_dir / "manifest.json"
                overall = sum(part["size_bytes"] for part in manifest["parts"]) + manifest_path.stat().st_size
                uploaded = 0
                for number, part in enumerate(manifest["parts"], start=1):
                    part_path = parts_dir / part["name"]
                    await send_local_file(
                        part_path,
                        part["name"],
                        f"{display_name} — الجزء {number}/{manifest['part_count']}",
                        uploaded,
                        overall,
                    )
                    uploaded += part["size_bytes"]
                await send_local_file(
                    manifest_path,
                    "manifest.json",
                    "ملف manifest لإعادة تجميع الأجزاء والتحقق من SHA-256. استخدم أداة join_parts في المشروع.",
                    uploaded,
                    overall,
                )
                await update_progress(overall, overall, force=True)
                cleanup_path = None  # retain parts and manifest for recovery if the session ends
                await message.answer(
                    f"أُرسل الملف مقسمًا إلى {manifest['part_count']} أجزاء. أُبقيت الأجزاء في: {parts_dir}"
                )
            try:
                await progress_message.edit_text(f"✅ اكتمل إرسال {display_name} إلى محادثة Telegram.")
            except Exception:
                log.debug("Unable to finalize Telegram transfer progress message")
            job.status = "completed"
            succeeded = True
        except asyncio.CancelledError:
            job.status = "cancelled"
            await message.answer("أُلغيت العملية. لم يُحذف ملف Drive الأصلي.")
            raise
        except Exception as exc:
            job.status, job.error = "failed", type(exc).__name__
            log.exception("Drive download/send failed")
            await message.answer(
                f"فشل تنزيل الملف أو إرساله في مرحلة {job.status}: {type(exc).__name__}. ملف Drive الأصلي لم يتغير."
            )
        finally:
            from datetime import datetime, timezone

            job.finished_at = datetime.now(timezone.utc).isoformat()
            jobs._persist()
            if acquired:
                jobs.semaphore.release()
            if cleanup_path:
                cleanup_path.unlink(missing_ok=True)
        return succeeded

    @router.message(Command("download"))
    @router.message(F.text == "📥 تنزيل ملف")
    async def download(message: Message, bot: Bot) -> None:
        if not authorized(message, settings):
            return
        raw = message.text or ""
        try:
            args = _args(raw) if raw.startswith("/") else []
            if not args:
                user_id = message.from_user.id
                _DOWNLOAD_SESSIONS[user_id] = {"files": []}
                await message.answer("اختر ملفات من المتصفح؛ يمكنك اختيار أكثر من ملف ثم إرسال /download_done.")
                await render_browse(message, user_id, root_path())
                return
            if len(args) != 1:
                await message.answer('استخدم /download "مسار الملف" أو ابدأ اختيارًا متعددًا عبر /download.')
                return
            target = _resolve_user_path(fs, root_path(), args[0])
            if not target.exists():
                raise FileNotFoundError("لم يُعثر على المسار داخل جذور Drive المسموحة")
            if target.is_file():
                await send_drive_path(message, bot, target, owner_id=message.from_user.id)
            else:
                await send_drive_path(message, bot, target, owner_id=message.from_user.id)
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر بدء التنزيل: {exc}")

    @router.message(Command("download_done"))
    async def finish_download_selection(message: Message, bot: Bot) -> None:
        if not authorized(message, settings):
            return
        user_id = message.from_user.id
        session = _DOWNLOAD_SESSIONS.get(user_id)
        if not session or not session["files"]:
            await message.answer("لم تختر ملفات. ابدأ عبر /download ثم حدد الملفات من المتصفح.")
            return
        files = [fs.resolve(Path(value)) for value in session["files"]]
        await message.answer(f"سيبدأ إرسال {len(files)} ملفًا بالتتابع إلى هذه المحادثة.")
        failures: list[str] = []
        for target in files:
            if not target.is_file():
                await message.answer(f"تُخطي ملفًا لم يعد موجودًا: {target.name}")
                continue
            if not await send_drive_path(message, bot, target, owner_id=user_id):
                failures.append(str(target))
        if failures:
            session["files"] = failures
            await message.answer(
                f"تعذر إرسال {len(failures)} ملفًا؛ بقيت في الجلسة. صحح السبب ثم أرسل /download_done لإعادة المحاولة."
            )
        else:
            _DOWNLOAD_SESSIONS.pop(user_id, None)

    @router.message(Command("cancel_download"))
    async def cancel_download_selection(message: Message) -> None:
        if not authorized(message, settings):
            return
        removed = _DOWNLOAD_SESSIONS.pop(message.from_user.id, None)
        await message.answer("أُلغي اختيار التنزيل." if removed else "لا يوجد اختيار تنزيل نشط.")

    @router.message(Command("index"))
    async def show_index(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            args = _args(message.text or "")
            folder = _resolve_user_path(fs, root_path(), args[0]) if args else root_path()
            rows = await asyncio.to_thread(CsvIndex(folder / settings.index_filename).rows)
            if not rows:
                await message.answer("لا يحتوي هذا المجلد على سجل CSV بعد.")
                return
            lines = [
                f"{r.get('file_name', '')[:100]} — {r.get('size_bytes', '')} بايت — {r.get('status', '')}"
                for r in rows[-20:]
            ]
            await message.answer("سجلات الفهرس (حتى 20):\n" + "\n".join(lines))
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر قراءة الفهرس: {exc}")

    @router.message(Command("index_sync"))
    async def sync_index(message: Message) -> None:
        if not authorized(message, settings):
            return
        try:
            args = _args(message.text or "")
            folder = _resolve_user_path(fs, root_path(), args[0]) if args else root_path()
            if not folder.is_dir():
                raise NotADirectoryError("المسار المحدد ليس مجلدًا")
            index = CsvIndex(folder / settings.index_filename)
            entries = await asyncio.to_thread(lambda: list(folder.iterdir()))
            added = 0
            group_id = f"sync-{uuid4().hex}"
            for item in entries:
                if item.is_file() and not item.is_symlink() and item.name != settings.index_filename:
                    await asyncio.to_thread(index.append, item, group_id=group_id)
                    added += 1
            await message.answer(
                f"اكتملت مزامنة {folder}: تمت مراجعة {added} ملفًا.\n"
                "المزامنة لا تحذف السجلات القديمة، وقد تتجاوز السجلات المتطابقة بالمسار والحجم."
            )
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذرت مزامنة CSV: {type(exc).__name__}: {exc}")

    @router.message(Command("find"))
    async def find_index(message: Message) -> None:
        if not authorized(message, settings):
            return
        query = (message.text or "").partition(" ")[2].strip().casefold()
        if not query:
            await message.answer("استخدم /find كلمة للبحث في الاسم والوصف والملاحظة والمسار داخل فهرس CSV.")
            return
        try:
            root = root_path()
            matches = await asyncio.to_thread(_find_index_rows, root, settings.index_filename, query)
            rendered = "\n".join(f"{r.get('file_name', '')[:80]} — {r.get('drive_path', '')[:150]}" for r in matches)
            await message.answer(rendered or "لا توجد نتائج في الفهارس المتاحة.")
        except (OSError, ValueError) as exc:
            await message.answer(f"تعذر البحث في CSV: {exc}")

    @router.message(Command("jobs"))
    async def list_jobs(message: Message) -> None:
        if not authorized(message, settings):
            return
        own = jobs.owned(message.from_user.id)
        await message.answer(
            "\n".join(f"{j.job_id} — {j.kind} — {j.status}" for j in own[-20:]) or "لا توجد عمليات مسجلة في هذه الجلسة."
        )

    @router.message(Command("cancel"))
    async def cancel_job(message: Message) -> None:
        if not authorized(message, settings):
            return
        job_id = (message.text or "").partition(" ")[2].strip()
        if not job_id:
            await message.answer("استخدم /cancel <معرف العملية>")
            return
        await message.answer(
            "أُرسل طلب الإلغاء." if jobs.cancel(job_id, message.from_user.id) else "لم أجد عملية جارية بهذا المعرف تخصك."
        )

    @router.message(F.document | F.video | F.audio | F.photo)
    async def receive_media(message: Message, bot: Bot) -> None:
        if not authorized(message, settings):
            await message.answer("غير مصرح لك باستخدام البوت.")
            return
        media: Document | Video | Audio | PhotoSize | None = (
            message.document
            or message.video
            or message.audio
            or (message.photo[-1] if message.photo else None)
        )
        if media is None:
            return
        file_id, file_unique_id, expected_size, name = _media_details(media)
        user_id = message.from_user.id
        session = _UPLOAD_SESSIONS.get(user_id)
        if session and session["stage"] != "collecting":
            await message.answer(
                "اكتملت مرحلة جمع الملفات. استخدم الأزرار الظاهرة لتأكيد الرفع أو أضف الملفات بعد اختيار «إضافة ملفات أخرى».",
                reply_markup=_upload_keyboard(session["stage"]),
            )
            return
        if session and len(session["files"]) >= settings.max_files_per_batch:
            await message.answer(
                f"وصلت إلى الحد المضبوط وهو {settings.max_files_per_batch} ملفًا لهذه الدفعة. اضغط «انتهيت» للمتابعة.",
                reply_markup=_upload_keyboard("collecting"),
            )
            return
        if expected_size and expected_size > settings.max_operation_gb * 1024**3:
            await message.answer("حجم الملف يتجاوز حد العملية المضبوط.")
            return
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(settings.temp_dir).free < (expected_size or 0) * 1.1:
            await message.answer("المساحة المحلية غير كافية لاستقبال الملف بأمان.")
            return
        local_temp: Path | None = None
        target: Path | None = None
        try:
            remote = await bot.get_file(file_id)
            local = Path(remote.file_path)
            if session:
                local_temp = settings.temp_dir / f"dtm-batch-{uuid4().hex}-{name}"
                if local.is_absolute():
                    size = await asyncio.to_thread(fs.copy_streaming, local, local_temp)
                else:
                    await bot.download_file(remote.file_path, destination=local_temp)
                    size = local_temp.stat().st_size
                if expected_size is not None and size != expected_size:
                    raise OSError("حجم الملف المستلم لا يطابق القيمة المتوقعة")
                session["files"].append(
                    {
                        "path": str(local_temp),
                        "name": name,
                        "size": size,
                    }
                )
                local_temp = None  # ownership transferred to the per-user session
                await message.answer(
                    f"✅ أُضيف {name} إلى دفعتك ({len(session['files'])}/{settings.max_files_per_batch}).\n"
                    "أرسل المزيد، أو اضغط «انتهيت من إرسال الملفات» للانتقال للخطوة التالية.",
                    reply_markup=_upload_keyboard("collecting"),
                )
                return
            if not local.is_absolute():
                local_temp = settings.temp_dir / f"dtm-in-{uuid4().hex}-{name}"
                await bot.download_file(remote.file_path, destination=local_temp)
                local = local_temp
            target = unique_destination(root_path() / name)
            size = await asyncio.to_thread(fs.copy_streaming, local, target)
            if expected_size is not None and size != expected_size:
                target.unlink(missing_ok=True)
                raise OSError(f"عدم تطابق الحجم المتوقع ({expected_size}) مع المحفوظ ({size})")
            caption = message.caption or ""
            description, note = caption, ""
            for line in caption.splitlines():
                if line.strip().startswith("الوصف:"):
                    description = line.partition(":")[2].strip()
                elif line.strip().startswith("ملاحظة:"):
                    note = line.partition(":")[2].strip()
            try:
                await asyncio.to_thread(
                    CsvIndex(target.parent / settings.index_filename).append,
                    target,
                    group_id=getattr(media, "media_group_id", None) or file_unique_id,
                    description=description,
                    note=note,
                )
            except OSError:
                await message.answer(f"تم حفظ {target.name} في Drive، لكن فشل تحديث CSV.")
                return
            await message.answer(f"تم حفظ {target.name} في Drive ({size:,} بايت) وتحديث CSV.")
        except Exception as exc:
            log.exception("Media transfer failed")
            await message.answer(
                f"فشل استقبال أو حفظ الملف: {type(exc).__name__}. تحقق من الخادم المحلي ومساحة Drive ثم أعد المحاولة."
            )
        finally:
            if local_temp:
                local_temp.unlink(missing_ok=True)

    dp.include_router(router)
    return dp


async def run(settings: Settings) -> None:
    bot = build_bot(settings)
    dp = make_dispatcher(settings)
    try:
        await bot.get_me()
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="بدء البوت"),
                BotCommand(command="help", description="المساعدة والأوامر"),
                BotCommand(command="menu", description="عرض القائمة الرئيسية"),
                BotCommand(command="upload", description="بدء رفع مجموعة ملفات"),
                BotCommand(command="upload_done", description="إنهاء اختيار الملفات"),
                BotCommand(command="skip", description="تجاوز الوصف أو الملاحظة"),
                BotCommand(command="upload_confirm", description="تأكيد رفع الدفعة"),
                BotCommand(command="cancel_upload", description="إلغاء جلسة الرفع"),
                BotCommand(command="browse", description="استعراض Drive"),
                BotCommand(command="search", description="البحث بالاسم"),
                BotCommand(command="download", description="تنزيل ملف من Drive"),
                BotCommand(command="download_done", description="إرسال الملفات المحددة"),
                BotCommand(command="cancel_download", description="إلغاء اختيار التنزيل"),
                BotCommand(command="mv", description="إعادة تسمية عنصر في Drive"),
                BotCommand(command="move", description="نقل عنصر في Drive"),
                BotCommand(command="cp", description="نسخ ملف داخل Drive"),
                BotCommand(command="mkdir", description="إنشاء مجلد"),
                BotCommand(command="rm", description="حذف بعد التأكيد"),
                BotCommand(command="index", description="عرض فهرس CSV"),
                BotCommand(command="index_sync", description="مزامنة ملفات المجلد إلى CSV"),
                BotCommand(command="find", description="البحث في فهرس CSV"),
                BotCommand(command="info", description="تفاصيل ملف وSHA-256"),
                BotCommand(command="join", description="إعادة تجميع أجزاء ملف"),
                BotCommand(command="jobs", description="عرض عملياتك"),
                BotCommand(command="cancel", description="إلغاء عملية"),
                BotCommand(command="status", description="حالة Drive والبوت"),
            ]
        )
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
