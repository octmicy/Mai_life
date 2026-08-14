"""Mai_life 指令菜单的结构化目录与纯文本降级内容。"""
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
        CommandItem("/麦麦状态","生活状态、模型与后台任务健康"),
        CommandItem("/麦麦日程","今日日程与当前细化场景"),
        CommandItem("/麦麦关系","关系角色、温度与主动额度（需用户档案）"),
        CommandItem("/麦麦配置","当前插件配置摘要"),
        CommandItem("/麦麦撤回","本人最近撤回摘要（需用户档案）"),
    )),
    CommandSection("生活记录",(
        CommandItem("/麦麦日记","最近生活日记（主人或管理员）"),
        CommandItem("/麦麦日期","重要日期与待确认项（需用户档案）"),
        CommandItem("/麦麦添加日期 日期 名称","添加自己的重要日期（需用户档案）"),
        CommandItem("/麦麦删除日期 编号","删除自己的重要日期（需用户档案）"),
        CommandItem("/麦麦确认日期 编号 [日期]","确认日期候选（需用户档案）"),
    )),
    CommandSection("见闻与书柜",(
        CommandItem("/麦麦新闻","近期新闻见闻（主人或管理员）"),
        CommandItem("/麦麦探索","主动搜索笔记（主人或管理员）"),
        CommandItem("/麦麦书柜","当前关系可见的书柜"),
        CommandItem("/麦麦阅读 文本编号","阅读有权限访问的文本"),
        CommandItem("/麦麦转述 群QQ号 内容","向白名单群发起简单转述"),
    )),
    CommandSection("管理与诊断",(
        CommandItem("/麦麦统计","Token 与搜索 API 统计（管理员）"),
        CommandItem("/麦麦管理 [概览/用户/群聊/日期/来源/书柜/统计/主动]","查看脱敏管理摘要（管理员）"),
        CommandItem("/麦麦立即创作","立即执行创作判断（管理员）"),
        CommandItem("/麦麦重生日程","重新生成今日日程（管理员）"),
        CommandItem("/麦麦休息测试","休息闸门诊断（管理员）"),
    )),
)


def build_command_usage_text(notice:str="")->str:
    """构造不依赖 Markdown 的命令菜单文本，用于图片不可用时降级。"""
    lines=["麦麦生活 · 指令中心","输入 /麦麦 或 /麦麦帮助 可再次查看菜单。"]
    clean_notice=" ".join(str(notice or "").replace("\x00","").split())[:120]
    if clean_notice:lines.extend(("",clean_notice))
    for section in COMMAND_SECTIONS:
        lines.extend(("",section.title))
        lines.extend(f"{item.command}  {item.description}" for item in section.items)
    lines.extend(("","私聊管理员无需重复创建用户档案；关系、撤回摘要和重要日期仍需启用用户档案。"))
    return "\n".join(lines)


__all__=["COMMAND_SECTIONS","CommandItem","CommandSection","build_command_usage_text"]
