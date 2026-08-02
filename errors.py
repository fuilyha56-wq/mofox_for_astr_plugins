"""AstrBot 兼容桥的可识别错误类型。

错误类型是桥的公共失败边界：没有真实宿主映射的能力必须抛出这些错误，
不能以空对象、空列表或固定成功值掩盖问题。
"""

from __future__ import annotations


class AstrCompatError(RuntimeError):
    """AstrBot 兼容桥的基础错误。"""

    code = "ASTR_COMPAT_ERROR"


class AstrUnitAdmissionError(AstrCompatError):
    """外部 AstrBot 单元未通过预备期准入检查。"""

    code = "ASTR_UNIT_ADMISSION_REJECTED"


class UnitUnavailableError(AstrCompatError):
    """单元未激活、已隔离或正在卸载，无法接受新调用。"""

    code = "ASTR_UNIT_UNAVAILABLE"


class LateComponentRegistrationError(AstrCompatError):
    """外部插件在宿主组件快照后试图新增宿主可见组件。"""

    code = "ASTR_LATE_COMPONENT_REGISTRATION"


class UnsupportedAstrCapabilityError(AstrCompatError):
    """请求的 AstrBot 能力不存在可验证的 Neo-MoFox 映射。"""

    code = "ASTR_CAPABILITY_UNSUPPORTED"


class UnsupportedMessageSegmentError(UnsupportedAstrCapabilityError):
    """消息链含有当前 OneBot 发送链路无法无损表达的段。"""

    code = "ASTR_MESSAGE_SEGMENT_UNSUPPORTED"


class OneBotProfileError(UnsupportedAstrCapabilityError):
    """OneBot 专属 API 被用于非 QQ/OneBot 消息。"""

    code = "ASTR_ONEBOT_PROFILE_REQUIRED"


class OneBotOperationError(AstrCompatError):
    """OneBot Service 返回失败或不符合已验证的响应契约。"""

    code = "ASTR_ONEBOT_OPERATION_FAILED"


class UnknownProviderIdError(AstrCompatError):
    """调用方请求的 AstrBot Provider ID 没有显式模型任务映射。"""

    code = "ASTR_UNKNOWN_PROVIDER_ID"


class AgentLoopUnsupportedError(UnsupportedAstrCapabilityError):
    """请求完整 Agent 工具循环，但宿主不存在等价执行器。"""

    code = "ASTR_AGENT_LOOP_UNSUPPORTED"


class ConfigPersistenceUnsupportedError(UnsupportedAstrCapabilityError):
    """外部配置尝试持久化，但宿主尚未提供公开的原子配置扩展接口。"""

    code = "ASTR_CONFIG_PERSISTENCE_UNSUPPORTED"