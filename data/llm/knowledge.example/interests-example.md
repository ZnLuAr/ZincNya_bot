---
category: interests
title: 这里写条目标题（比如：编程语言偏好）
tags: [示例, 占位, 替换我]
tags_expanded: []
priority: 1
---

这里是兴趣类条目的占位——

在这里写一些话题相关的小知识。运行时咱会按用户消息里的关键词把对应条目召回，作为 `<TRUSTED_KNOWLEDGE>` 块塞进上下文里。

写法小提示：

- 一个 `.md` 文件 = 一个独立话题。文件名建议 `category-topic.md`，比如 `interests-programming.md`。
- `tags` 是人工核心关键词（4-6 个），写用户实际可能说出口的词。
- `tags_expanded` 不用手填——交给 `scripts/expand_knowledge_tags.py` 这个离线脚本去扩展同义词就可以了（
- `priority` 默认 0 就行，某条特别需要被优先召回时再调高。
- 正文 50-300 字最佳。太长了会挤 token，太短不如直接塞进 prompt 里面

写完记得跑一下 `/llm knowledge reindex` 让咱重新建索引——

---

Written by ZincNya~
