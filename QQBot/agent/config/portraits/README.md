# 内置人格的出厂立绘

放这里的是**随代码发布**的默认形象，文件名 = 人格 key：

```
rubi.png            # /人格切换 rubi
roxy_character.png  # /人格切换 roxy_character
```

- 支持 `png / jpg / jpeg / webp`，按这个顺序查找第一个命中的。
- 建议长边 ≤1536、单声道 RGB、体积 ≤4MB（和 `/设形象` 归一化后的规格一致）。
- **运营覆盖**不写在这里，而是写到数据盘：
  `CHARACTERS_DIR/_builtin/<key>/assets/reference.png`（管理员 `/设形象 <内置名>` 自动写入）。
  覆盖层优先于本目录；`/清除形象` 会删掉覆盖层，回落到这里的出厂图。
- 立绘要和角色卡里的 `## Appearance Anchor` 文案一致（同一套发型/瞳色/服饰），
  否则文生图的文本锚和图片锚会打架。
