# 从体庄(tizhuang)题库导入真题扩充题库

`scripts/tizhuang_import.py` 用体庄题库服务(weishao2/tizhuang-agent-skills 的后端,
`https://tizhuang.qcscience.cc/api`)的 K12 真题扩充 `data/questions.json`。
真题来自各地试卷/教辅(学科网系),弥补本库"AI 生成、未经教师审核"的短板。

## 凭证与配额

- 凭证:环境变量 `TIZHUANG_API_KEY`(或 `QUESTION_SERVICE_LICENSE`),以 `X-API-Key` 头发送,不会打印/落盘。
- 基址:`QUESTION_SERVICE_URL` 可覆盖,默认 `https://tizhuang.qcscience.cc/api`。
- 配额:`GET /v1/quota` 免费查。免费注册档 **每天 200 题**,元数据 `/v1/meta/*` 免费,
  `GET /v1/questions` 每次消耗返回的题目数。每天 0 点(服务器日期)重置。
  全库 870 个数学知识点若每点补到 10 题约需 5000+ 题,按每天 200 约需 4 周跑完。

## 用法

```bash
export TIZHUANG_API_KEY=...   # 或写进 .env 后 source
python3 scripts/tizhuang_import.py map       # 知识点名 -> 体庄 grade_id/knowledge_id(免费)
python3 scripts/tizhuang_import.py fetch --per-topic 2 --target 10   # 每天跑一次
python3 scripts/tizhuang_import.py fetch --grades 10,11,12,13,14 --per-topic 3  # 只跑高中
python3 scripts/tizhuang_import.py status
```

- 年级映射:小学 1-6 -> 110..160(+学期),初中 7-9 -> 200/300/400(+学期),
  高一/高二 -> 500/600,选必一/二/三 -> 30100/30200/30300。
- 每个知识点优先用 `knowledge_id` 精确拉,未匹配的 197 个点走 `keyword=知识点名` 回退。
- 拉取时固定带 `has_images=false&auto_gradable=true`,并跳过:综合题(含子题)、
  题干/选项带图、题干写了"如图/图中"的无图题、LaTeX array 等无法转纯文本的题、
  多选题、选项不在 3-4 个或重复、答案不是单字母、答案超长(>60 字)或无有效解析的题。
- 选择题保留题源选项顺序和答案字母(不改写不重排);题干里内嵌 `A.x B.x` 形式的选项会拆出来。
- HTML/LaTeX 转纯文本(NFKC + 常见命令替换);`difficulty` 0-5 映射为 basic/standard/challenge。
- 去重靠 `content_hash`,记录在 `data/_tizhuang_import/log.json`(含 tz 题号、来源试卷、年份),
  原始返回在 `data/_tizhuang_import/raw/`。`questions.json` 字段白名单不允许带来源,所以溯源看 log。
- 追加到 `data/questions.json` 后跑 `python3 scripts/validate.py` 与 `verify_questions.py` 必过。

## 已知限制

- 服务端 `knowledge_tree_id(s)` 目前返回 500,只能用 `knowledge_id`(精确点)或 `keyword`。
- 公式是图片的题(has_images=true)无法表达为本库的纯文本 prompt,只能放弃(几何图形题基本都在内)。
- `offset` 分页参数被服务端忽略,靠 `random_order=true` 每天抽到新题。
- 每天最多 200 题,脚本会读 quota 自动刹车(`--budget` 可再限),次天继续;按年级轮询保证各学段均衡。
