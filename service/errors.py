"""领域错误类型。

所有业务规则违反都抛出：class:`DomainError` 的子类，HTTP 层依据
``status_code`` 与 ``code`` 生成稳定的错误响应，便于银行复核系统对接。
"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""

    status_code = 422
    code = "domain_error"

    def __init__(self, message: str, **extra: object) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, object]:
        return {"error": self.code, "message": self.message, **self.extra}


class ValidationError(DomainError):
    """请求数据不满足结构或取值要求。"""

    status_code = 400
    code = "validation_error"


class NotFoundError(DomainError):
    """引用的实体不存在。"""

    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    """请求与当前状态冲突（重复创建、状态机不允许的迁移）。"""

    status_code = 409
    code = "conflict"


class AuthorizationError(DomainError):
    """操作人缺少所需角色或不满足独立性要求。"""

    status_code = 403
    code = "forbidden"


class PublishBlockedError(DomainError):
    """正式结论发布闸门未通过。"""

    status_code = 422
    code = "publish_blocked"


class QualificationExpiredError(PublishBlockedError):
    """评估资格已过期，禁止发布。"""

    code = "qualification_expired"


class ReviewRequiredError(PublishBlockedError):
    """争议版本尚未通过双人复核。"""

    code = "review_required"


class ReviewRejectedError(PublishBlockedError):
    """双人复核给出否决意见，只能形成新版本。"""

    code = "review_rejected"


class NotCalculatedError(PublishBlockedError):
    """版本尚未完成价值计算，不能发布。"""

    code = "not_calculated"


class CalculationInterruptedError(DomainError):
    """计算过程中断；版本保持草稿态，可安全重试。"""

    status_code = 500
    code = "calculation_interrupted"
