---
name: interview-post-extractor
description: Retrieve and summarize interview-experience posts through the interview-crawl MCP server. Use when the user provides a supported interview-post URL, including Nowcoder and Xiaohongshu, and wants the interview questions.
---

# 面经抓取整理

## WorkFlow

1. 先调用 `interview-post-crawl` MCP server 提供的 `fetch_interview_post` 工具抓取帖子。返回内容包含帖子发布时间、抓取时间、面经保存目录、正文文字和图片 OCR 结果。保存目录是绝对目录路径，不是完整文件名，文件名看下面的要求你自行提取生成；日期已由 MCP 按抓取时的北京时间确定，直接使用。
  

2. 如果分享链接来自小红书，且抓取失败原因是登录态缺失或过期，提示用户“小红书登录信息已失效，请重新登录”；

3. 如果帖子包含多次面试的面经，比如同时包含腾讯一面，阿里二面内容，那就分别生成2个md文件；

4. 生成 Markdown 面经笔记时，AI 简要回答部分要简洁精炼，无需长篇大论。

5. 调用系统工具写入相应markdown文件，要求：
   - 在 MCP 返回的保存目录下，以 Markdown 一级标题作为文件名，添加 `.md` 后缀；最终相对路径形如 `面经/年/月/日/<标题>.md`。
   - 标题名看下面的示例说明；
   - 同名文件已经存在时追加序号，如已有name.md则用name-2.md，避免覆盖。
   - 多份面经分别命名，保存到同一个返回目录

## Markdown 格式

使用下面的结构。没有内容的可选部分可以省略。

```markdown
# 标题（公司名+部门+一面/二面..., 若没有这些信息，则用帖子的默认标题名(只保留正常文字如中文数字英文，表情包等忽略) ）
面试时间: (指的是面试时间，如果有说的话)
来源: 牛客/小红书 
url: ...
发布时间: ...（指的是帖子发布时间，没有就忽略）
抓取时间: ...
生成模型：...（使用系统工具获取，没有就凭你自己的印象填自己是什么模型）

题目如下：
## 项目相关
1. 问题1；
原文回答: xxx（如果有发帖人自己的回答的话，这部分不要压缩精炼，保留原文）
AI简要回答：xxx
2. 问题2：...

## 八股(比较宽泛，场景题等都可以包含在内)
1. 问题1；
原文回答: xxx（如果有发帖人自己的回答的话，这部分不要压缩精炼，保留原文）
AI简要回答：xxx
2. 问题2: ...

## 手撕
1. 两数之和；
AI简要思路：xxx

## (可选，其他分类)
...

## 面试者的感言(如果有的话，就写，原封不动就行，没有就可以省略该行)
```
