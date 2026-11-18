"""Application errors that map cleanly onto HTTP responses."""

from __future__ import annotations


class DirectoryError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = list(details or [])

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class InputError(DirectoryError):
    status_code = 400
    code = "invalid_input"


class NotFoundError(DirectoryError):
    status_code = 404
    code = "not_found"


class ConflictError(DirectoryError):
    status_code = 409
    code = "conflict"


class RestoreInProgress(ConflictError):
    status_code = 409
    code = "restore_in_progress"

    def __init__(self, message: str = "备份恢复进行中，新的修改已被拒绝，请稍后重试") -> None:
        super().__init__(message)


class StaleEditError(ConflictError):
    """The edit token a form carried is no longer the service's current token.

    The token is opaque and is rotated on every change to the service (and on
    a successful restore), so an old form conflicts even if the address has
    since been changed back to the value the form was built from.
    """

    status_code = 409
    code = "stale_edit"

    def __init__(self, service: str, current: dict, message: str | None = None) -> None:
        self.service = service
        self.current = dict(current)
        version = self.current.get("version")
        if message is None:
            if self.current.get("exists"):
                message = (
                    f"冲突：服务 {service} 已被其他操作修改，当前地址为 "
                    f"{self.current.get('address')}（目录版本 {version}）。"
                    "你的表单已过期，未保存任何更改。"
                )
            else:
                message = (
                    f"冲突：服务 {service} 已被删除（目录版本 {version}）。"
                    "你的表单已过期，未做任何更改。"
                )
        if self.current.get("exists"):
            details = [
                f"当前地址：{self.current.get('address')}",
                f"目录版本：{version}",
            ]
        else:
            details = ["该服务当前不存在", f"目录版本：{version}"]
        super().__init__(message, details=details)

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["current"] = self.current
        return payload


class BackupNameTaken(ConflictError):
    status_code = 409
    code = "backup_name_taken"


class BackupValidationError(DirectoryError):
    status_code = 422
    code = "invalid_backup"

    def __init__(self, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message, details=details)


class DatabaseBusy(DirectoryError):
    status_code = 503
    code = "database_busy"

    def __init__(self) -> None:
        super().__init__("数据库繁忙，请稍后重试")


class DatabaseError(DirectoryError):
    status_code = 500
    code = "database_error"

    def __init__(self, message: str = "数据库操作失败") -> None:
        super().__init__(message)
