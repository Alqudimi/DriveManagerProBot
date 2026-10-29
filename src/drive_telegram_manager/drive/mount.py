from __future__ import annotations

from pathlib import Path


def mount_drive(mount_point: Path = Path("/content/drive"), force_remount: bool = False) -> Path:
    try:
        from google.colab import drive
    except ImportError as exc:
        raise RuntimeError("mount_drive متاح داخل Google Colab فقط؛ اربط Drive يدويًا في البيئات الأخرى.") from exc
    drive.mount(str(mount_point), force_remount=force_remount)
    return mount_point


def authenticate_drive_api():
    """Return an authenticated Drive v3 API client using interactive Colab auth; no key files."""
    try:
        from google.auth import default
        from google.colab import auth
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("تثبيت google-api-python-client مطلوب لوظائف المشاركة. نفّذ مصادقة Colab التفاعلية.") from exc
    auth.authenticate_user()
    credentials, _ = default()
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_permissions(service, file_id: str) -> list[dict]:
    return (
        service.permissions()
        .list(fileId=file_id, fields="permissions(id,type,role,emailAddress,displayName,deleted,permissionDetails)")
        .execute()
        .get("permissions", [])
    )


def share(service, file_id: str, email: str, role: str) -> dict:
    if role not in {"reader", "commenter", "writer"}:
        raise ValueError("نوع الصلاحية غير مدعوم")
    permission = (
        service.permissions()
        .create(
            fileId=file_id,
            body={"type": "user", "role": role, "emailAddress": email},
            sendNotificationEmail=True,
            fields="id,type,role,emailAddress",
        )
        .execute()
    )
    return permission
