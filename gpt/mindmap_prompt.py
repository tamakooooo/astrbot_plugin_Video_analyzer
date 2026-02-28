MINDMAP_PROMPT_TEMPLATE = """
你是一个专业的信息架构师。请基于下面的“视频总结 Markdown”，生成一个 Mermaid 思维导图代码。

要求：
1. 仅输出 Mermaid 代码本体，不要加 ``` 包裹。
2. 使用 mindmap 语法，第一行必须是：mindmap
3. 节点层级清晰，最多 4 层，避免过深。
4. 内容简洁，优先提炼“主题-要点-细节”。
5. 语言使用中文。

视频总结 Markdown：
---
{note_text}
---
"""
