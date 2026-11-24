"""用固定短对话比较千问 Responses API 在五组参数下的跨轮推理计数。"""

import sys
from pathlib import Path

from openai import APIError, OpenAI

# 直接运行本脚本时从项目根目录读取 API 配置。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Config


def main() -> None:
    """各参数分别发送 A/B 请求并比较输入用量，单次失败后继续其余对照。"""
    config = Config.load(ROOT / "config.yaml")
    # 取自会话 01a20c6b 的 turn_id=2（ordinal 20–26）和下一轮用户追问（29）。
    # 仅嵌入对话与工具项，去掉存储 metadata；B 组再移除两条原始 reasoning。
    a = [
        {'type': 'message', 'role': 'user', 'content': '重新试下呢，我改了下'},
        {'id': 'rs_632ea23e-53ee-41a3-9daf-ceec7fbb38f0',
         'summary': [{'text': '用户要求我重新尝试执行 PowerShell 命令来检查 E 盘下的文件夹数量。之前失败是因为找不到 '
                              'codex.exe，现在用户说已经改了一下，所以我应该再次尝试运行相同的命令。\n'
                              '\n'
                              '让我使用 powershell 工具来执行 Get-ChildItem 命令来获取 E 盘目录的数量。',
                      'type': 'summary_text'}],
         'type': 'reasoning',
         'status': 'completed'},
        {'arguments': '{"command": "(Get-ChildItem -Path E:\\\\ -Directory).Count", "workdir": '
                      '"D:\\\\projects\\\\others\\\\miniAgent"}',
         'call_id': 'call_5f2c100606354b009b873b2d',
         'name': 'powershell',
         'type': 'function_call',
         'id': 'fc_6ca6ad2c-9e39-4722-9d2f-d23f33b474c6',
         'status': 'completed'},
        {'type': 'function_call_output',
         'call_id': 'call_5f2c100606354b009b873b2d',
         'output': '工具执行失败，错误信息：{"exit_code": 0, "stdout": "0\\n", "stderr": "Get-ChildItem : Access '
                   'is denied\\n所在位置 行:1 字符: 60\\n+ ... stem.Text.Encoding]::UTF8; (Get-ChildItem '
                   '-Path E:\\\\ -Directory).Count\\n+                                 '
                   '~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\\n    + CategoryInfo          : '
                   'PermissionDenied: (E:\\\\:String) [Get-ChildItem], '
                   'UnauthorizedAccessException\\n    + FullyQualifiedErrorId : '
                   'ItemExistsUnauthorizedAccessError,Microsoft.PowerShell.Commands.GetChildItemCommand\\n '
                   '\\nGet-ChildItem : 找不到路径“E:\\\\”，因为该路径不存在。\\n所在位置 行:1 字符: 60\\n+ ... '
                   'stem.Text.Encoding]::UTF8; (Get-ChildItem -Path E:\\\\ '
                   '-Directory).Count\\n+                                 '
                   '~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\\n    + CategoryInfo          : '
                   'ObjectNotFound: (E:\\\\:String) [Get-ChildItem], ItemNotFoundException\\n    + '
                   'FullyQualifiedErrorId : '
                   'PathNotFound,Microsoft.PowerShell.Commands.GetChildItemCommand\\n \\n", "error": '
                   '"沙箱不允许该操作，权限不足。请向用户说明操作未能完成；若 stderr 指明了被拒绝的路径，请一并告知。"}'},
        {'id': 'rs_6e863d6d-fd89-4f19-b9db-831f7e0455c1',
         'summary': [{'text': '从错误信息来看，有两个问题：\n'
                              '1. 权限被拒绝（Access is denied）\n'
                              '2. 路径 E:\\ 不存在（找不到路径"E:\\"）\n'
                              '\n'
                              '这说明：\n'
                              '1. 当前环境可能没有足够的权限访问 E 盘\n'
                              '2. 或者系统上根本没有 E 盘挂载\n'
                              '\n'
                              '我需要向用户说明这个情况。可能是：\n'
                              '- E 盘确实不存在\n'
                              '- 存在但没有读取权限\n'
                              '- 沙箱环境限制了对某些驱动器的访问\n'
                              '\n'
                              '我应该告诉用户实际情况。',
                      'type': 'summary_text'}],
         'type': 'reasoning',
         'status': 'completed'},
        {'id': 'msg_5d7981f9-788c-48a4-b16e-2a67e6354c5c',
         'content': [{'annotations': [],
                      'text': '抱歉，这次尝试还是失败了。根据错误信息显示：\n'
                              '\n'
                              '**无法访问 E 盘的原因有两个：**\n'
                              '1. **权限不足** - 当前运行的权限无法读取该目录\n'
                              '2. **路径不存在** - 系统提示找不到 "E:\\" 路径\n'
                              '\n'
                              '这可能是因为：\n'
                              '- E 盘目前没有挂载到系统中\n'
                              '- E 盘需要特定权限才能访问\n'
                              '- 当前运行环境的沙箱限制了对外部磁盘的访问\n'
                              '\n'
                              '您可以检查一下电脑中是否真的有 E 盘？如果有的话，可能需要提升权限或者换个方式查看。',
                      'type': 'output_text',
                      'logprobs': []}],
         'role': 'assistant',
         'status': 'completed',
         'type': 'message'},
        {'type': 'message', 'role': 'user', 'content': '黄仁勋是谁'},
    ]
    b = [item for item in a if item["type"] != "reasoning"]
    # 十笔请求都使用相同的简短指令和空工具列表。
    with OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0) as client:
        for options in (
            {},
            {"reasoning": {"context": "current_turn"}},
            {"reasoning": {"context": "all_turns"}},
            {"extra_body": {"preserve_thinking": False}},
            {"extra_body": {"preserve_thinking": True}},
        ):
            print(f"\n参数 {options}", flush=True)
            counts = {}
            for label, messages in (("A", a), ("B", b)):
                print(f"{label} 组发送 {len(messages)} 条消息", flush=True)
                try:
                    response = client.responses.create(
                        model="qwen3.7-flash", instructions="请简短回答。", input=messages, tools=[], **options
                    )
                except APIError as error:
                    print(f"{label} 请求失败：{error}", flush=True)
                    continue
                if response.usage is None:
                    print(f"{label} 服务端未返回 usage", flush=True)
                    continue
                counts[label] = response.usage.input_tokens
                print(f"{label} usage={response.usage.model_dump_json(exclude_none=True)}", flush=True)
            if len(counts) == 2:
                print(f"input_tokens 差值 A - B = {counts['A'] - counts['B']}", flush=True)


if __name__ == "__main__":
    main()
