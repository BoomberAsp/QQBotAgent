"""找图反查一期：纯 HTTP 引擎 + VLM 识别 + 同名共识。

设计见 docs/找图反查-方案.md。二期加 Google Lens（Chrome CDP）/Yandex。
按需从子模块导入（service 依赖 PicImageSearch，net/render 等不依赖）。
"""
