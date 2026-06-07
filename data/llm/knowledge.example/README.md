# 知识库模板（knowledge.example）

> 这里是 `data/llm/knowledge/` 的占位模板目录——
> 真正的知识库条目放在 `data/llm/knowledge/` 下，含人设细节，已经被 `.gitignore` 排除掉了。
> 想看完整设计的话，去翻 [docs/llm-knowledge.md](../../../docs/llm-knowledge.md) 喵。

---

## 这是什么东西

知识库（Knowledge Base）是咱 LLM 模块里的「按话题召回」层，跟 system prompt 是互补关系：

- **system prompt** —— 固定人格、固定规则，每次对话全文注入。
- **knowledge base** —— 兴趣爱好、风格示例这类可以慢慢扩展的内容。运行时根据用户消息分词 + BM25 评分召回 top-k 条，作为 `<TRUSTED_KNOWLEDGE>` 块塞进上下文里。

简单说：prompt 是「锌酱永远是这样的」，knowledge 是「锌酱在聊到 XX 的时候会想起 YY」喵。

## 文件长啥样

每个 `.md` 文件 = 一个知识条目，用 YAML frontmatter + Markdown 正文：

```markdown
---
category: interests              # 分类：interests / style_examples / 你想自己起的名字
title: 编程语言偏好               # 条目标题（必填）
tags: [Python, Rust, 编程]        # 人工核心 tags（4-6 个）
tags_expanded: []                # LLM 扩展 tags（离线脚本生成，不要手动填）
priority: 1                      # 优先级，越大越优先（默认 0）
---

正文写在这里——
支持多段、列表、引用块这些 Markdown 语法
```

frontmatter 字段说明：

| 字段 | 必填 | 说明 |
|------|------|------|
| `category` | 是 | 分类标识，用来给 `/llm knowledge list -category` 过滤用 |
| `title` | 是 | 条目标题，会被分词参与评分（权重 1.5） |
| `tags` | 是 | 人工核心 tags，评分时权重最高（2.0） |
| `tags_expanded` | 否 | 由 `scripts/expand_knowledge_tags.py` 自动维护，与 `tags` 合并入库 |
| `priority` | 否 | 排序加成，相同分数时优先；默认 0 就好 |

---

## 怎么开张一个新知识库

```bash
# 1. 把模板复制到正式目录
cp -r data/llm/knowledge.example/*.md data/llm/knowledge/

# 2. 改个名 + 改内容
#    建议命名：category-topic.md（像 interests-programming.md 这样）

# 3.（可选）让 LLM 帮忙扩展 tags_expanded
python scripts/expand_knowledge_tags.py

# 4. 触发重建索引
#    方法一：重启 bot（启动时会自动 reindex）
#    方法二：在 /send -c 控制台里执行
/llm knowledge reindex
```

## 写条目的小贴士

- **一个文件一个话题** —— 不要在一个文件里塞好几个不相干的主题，分词会被污染的
- **正文 50-300 字最舒服** —— 太长挤 token，太短不如就直接写进 prompt。
- **tags 选词要贴近用户实际会说的词** —— 同义词全集就交给 `expand_knowledge_tags.py` 离线扩展，自己扩展太累了
- **priority 别乱调** —— 默认 0 就行，只有「这条必须优先被召回」的时候再调高。
- **风格类条目** —— 在正文里放 1-2 个示范回复（用引用块），再加一段触发条件说明会更好用。

---

## 跟隐私的关系喵

- `data/llm/knowledge/` 跟 `data/llm/prompts.json` 同密级，都含人设细节，都已经 gitignore 了
- 这个目录（`knowledge.example/`）只放结构占位模板，可以随仓库公开，给参考格式用
- 公开 PR / Issue 里如果要引用知识库内容，应该用本目录的占位文本……如果不想的话，记得 **不要** 把 `data/llm/knowledge/` 下的真条目贴出去哦——

---

Written by ZincNya~