"""Mai_life 命令菜单的结构化目录与纯文本降级内容。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True,slots=True)
class CommandItem:
    command:str
    description:str


@dataclass(frozen=True,slots=True)
class CommandSection:
    title:str
    items:tuple[CommandItem,...]


COMMAND_SECTIONS:tuple[CommandSection,...]=(
    CommandSection("日常状态",(
        CommandItem("/mai_status","生活状态、模型与后台任务健康"),
        CommandItem("/mai_schedule","今日日程与当前细化场景"),
        CommandItem("/mai_relation","关系角色、温度与主动额度"),
        CommandItem("/mai_config","当前插件配置摘要"),
        CommandItem("/mai_recalled","本人最近撤回摘要"),
    )),
    CommandSection("生活记录",(
        CommandItem("/mai_diary","最近生活日记（主人或管理员）"),
        CommandItem("/mai_dates","重要日期与待确认项"),
        CommandItem("/mai_date_add 日期 名称","添加自己的重要日期"),
        CommandItem("/mai_date_remove ID","删除自己的重要日期"),
        CommandItem("/mai_date_confirm ID [日期]","确认日期候选"),
    )),
    CommandSection("见闻与书柜",(
        CommandItem("/mai_news","近期新闻见闻（主人或管理员）"),
        CommandItem("/mai_explore","主动搜索笔记（主人或管理员）"),
        CommandItem("/mai_bookshelf","当前关系可见的书柜"),
        CommandItem("/mai_read 文本ID","阅读有权限访问的文本"),
        CommandItem("/mai_relay 群QQ号 内容","向白名单群发起简单转述"),
    )),
    CommandSection("管理与诊断",(
        CommandItem("/mai_tokens","Token 与搜索 API 统计（管理员）"),
        CommandItem("/mai_admin [范围]","查看脱敏管理摘要（管理员）"),
        CommandItem("/mai_create_now","立即执行创作判断（管理员）"),
        CommandItem("/mai_regenerate_schedule","重新生成今日日程（管理员）"),
        CommandItem("/mai_rest_test","休息闸门诊断（管理员）"),
    )),
)


def build_command_usage_text(notice:str="")->str:
    """构造不依赖 Markdown 的命令菜单文本，用于图片不可用时降级。"""
    lines=["麦麦生活 · 指令中心","输入 /mai 或 /mai_help 可再次查看菜单。"]
    clean_notice=" ".join(str(notice or "").replace("\x00","").split())[:120]
    if clean_notice:lines.extend(("",clean_notice))
    for section in COMMAND_SECTIONS:
        lines.extend(("",section.title))
        lines.extend(f"{item.command}  {item.description}" for item in section.items)
    lines.extend(("","所有命令仅对已配置的私聊用户开放。"))
    return "\n".join(lines)


__all__=["COMMAND_SECTIONS","CommandItem","CommandSection","build_command_usage_text"]
