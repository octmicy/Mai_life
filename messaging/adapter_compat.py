"""SnowLuma 与 NapCat 入站消息的轻量归一化。"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any


SUPPORTED_ADAPTERS = ("snowluma", "napcat")


def _additional_config(message: Mapping[str, Any]) -> Mapping[str, Any]:
    info = message.get("message_info")
    if not isinstance(info, Mapping):
        return {}
    additional = info.get("additional_config")
    return additional if isinstance(additional, Mapping) else {}


def adapter_name(message: Mapping[str, Any]) -> str:
    """从适配器保留的技术字段识别来源，不依赖展示名称。"""
    additional = _additional_config(message)
    if any(str(key).startswith("napcat_") for key in additional):
        return "napcat"
    if any(str(key).startswith("snowluma_") for key in additional):
        return "snowluma"
    return "unknown"


def component_kind(component: Mapping[str, Any]) -> str:
    """展开 SnowLuma 用 ``dict`` 包装的 file/video 等媒介类型。"""
    kind = str(component.get("type") or "").strip().lower()
    data = component.get("data")
    if kind == "dict" and isinstance(data, Mapping):
        nested = str(data.get("type") or "").strip().lower()
        return nested or kind
    return kind


def component_text(component: Mapping[str, Any]) -> str:
    if component_kind(component) != "text":
        return ""
    data = component.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, Mapping):
        return str(data.get("text") or data.get("content") or "")
    return ""


def _forward_nodes(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    for key in ("messages", "content"):
        nested = value.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, Mapping)]
    nested_data = value.get("data")
    return _forward_nodes(nested_data) if nested_data is not value else []


def _node_content(node: Mapping[str, Any]) -> Any:
    for key in ("content", "message"):
        value = node.get(key)
        if isinstance(value, list):
            return value
    data = node.get("data")
    if isinstance(data, Mapping):
        for key in ("content", "message"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def walk_components(components: Any, *, _depth: int = 0) -> Iterator[dict[str, Any]]:
    """遍历标准消息段和两种适配器的合并转发节点。

    深度限制用于防止异常转发载荷构造循环或极深嵌套，避免阻塞消息 Hook。
    """
    if not isinstance(components, list) or _depth >= 8:
        return
    for item in components:
        if not isinstance(item, dict):
            continue
        yield item
        if component_kind(item) != "forward":
            continue
        for node in _forward_nodes(item.get("data")):
            yield from walk_components(_node_content(node), _depth=_depth + 1)


def reply_target_ids(message: Mapping[str, Any]) -> list[str]:
    targets: list[str] = []
    direct = str(message.get("reply_to") or "").strip()
    if direct:
        targets.append(direct)
    for item in walk_components(message.get("raw_message")):
        if component_kind(item) != "reply":
            continue
        data = item.get("data")
        if not isinstance(data, Mapping):
            continue
        target = str(data.get("target_message_id") or data.get("id") or "").strip()
        if target and target not in targets:
            targets.append(target)
    return targets
