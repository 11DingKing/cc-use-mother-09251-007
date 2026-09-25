"""领域/应用层错误类型。"""


class ServiceError(Exception):
    """所有业务错误的基类，携带可直接返回给客户端的消息。"""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    """乐观锁冲突或同键不同内容的重复提交。"""


class ImmutableError(ServiceError):
    """试图修改已签署快照。"""


class ValidationError(ServiceError):
    pass
