# 截标时间核对工具

面向采购专员的网页工具：从公开招标公告站抓取公告，按项目串起公告链，
自动应用更正与撤销，给出每个项目的**最终截标时间 / 停办状态**及**依据链接**；
并提供**公告正文检索**，帮助招标资料员快速定位“联合体投标”等条款并点开原文核对。

仓库包含两个服务：

| 服务 | 目录 | 说明 |
| --- | --- | --- |
| 核对工具 | `app/` | FastAPI 网页：输入列表网址 → 抓取同源分页与详情 → 分析展示 + 正文检索，SQLite（含 FTS5）保存采集轮次 |
| 示例公告站 | `sample_site/` | FastAPI 模拟公告站：3 页列表分页 + 11 条公告详情（含更正、撤销、联合体投标条款及条款变更），支持故障注入与公告撤下 |

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 核对工具：http://localhost:8000/ （打开后点击 **开始抓取**，默认已填入示例站列表网址）
- 示例公告站：http://localhost:8080/list.html?page=1

## 功能说明

1. **抓取**：输入与示例站同结构的列表网址，点击抓取。工具只跟随与起始网址
   **同源**的分页链接（`nav.pagination`）与详情链接（`ul.notice-list`），
   逐个读取详情页的：项目编号、公告编号、公告类型、发布时间、截标时间、
   更正/撤销所指公告编号，并提取公告正文区的可见文字。
2. **分析**：同一项目编号的公告按发布时间串链——
   - 招标公告确立截标时间；
   - 更正公告更新截标时间（链上记录 `旧值 → 新值`）；
   - 撤销公告将项目标记为 **停办**。
   页面展示每个项目的最终状态、当前截标时间，以及每条公告的来源链接（依据）。
3. **正文检索**：在“公告正文检索”框输入**连续至少三个汉字**的短语（如
   `联合体投标`），结果表格展示**项目编号、公告编号、命中原文片段（高亮）与
   来源链接**，点击“查看原文”即可核对公告页面。
   - 只检索采集时提取的**公告正文区可见文字**：顶部导航、侧栏、`<script>`/
     `<style>` 以及 `hidden` / `display:none` / `visibility:hidden` 的隐藏内容
     均不参与；列表页只用于发现链接，不建索引；
   - 输入中不足三个汉字的字串（单字、两字词）与英文、数字、标点一律忽略，
     没有任何合格短语时页面给出提示；多个合格短语为“且”关系，需同时命中；
   - 结果**只来自最近一次成功采集**，底层为 SQLite **FTS5（trigram 分词器）**索引，
     支持汉字短语按子串命中。
4. **轮次持久化（SQLite）**：每次抓取是一个轮次，公告快照、正文与 FTS 索引挂在
   轮次下，保存于 SQLite（容器卷 `app-data`，`/data/app.db`）。
   - **任一页面失败**：轮次标记失败并记录出错网址与原因，页面红色横幅提示，
     项目结果与正文检索仍使用上一轮成功数据；
   - **重抓不叠加**：公告记录、正文、FTS 索引与轮次成功标记在**同一事务**中提交，
     失败轮次不留半截数据（含半截索引）；页面始终只展示最近一次成功轮次的结果，
     成功重抓后被撤下的公告不会再出现在检索结果中。

## 演示失败处理与公告撤下

```bash
# 开启故障注入：详情页 NT-2026-0004 返回 500
curl "http://localhost:8080/admin/fail?on=1"
# 在 http://localhost:8000/ 点击“开始抓取” → 第 N 轮失败，报出该网址，结果仍是上一轮，检索也仍是上一轮
# 关闭故障后重抓 → 新一轮成功，数据整体替换，不叠加
curl "http://localhost:8080/admin/fail?on=0"

# 撤下一条公告（列表不再展示、详情返回 404），再到核对工具重抓
curl "http://localhost:8080/admin/remove?notice_no=NT-2026-0005&on=1"
# 重抓成功后检索“联合体投标”，NT-2026-0005 不再出现；恢复撤下用 on=0
curl "http://localhost:8080/admin/remove?notice_no=NT-2026-0005&on=0"
```

### 条款变更场景（联合体投标）

- `NT-2026-0001` 招标公告：**接受联合体投标**（成员不超过两家）；
- `NT-2026-0002` 更正公告：将条款**变更为不接受联合体投标**，同时更正截标时间；
- `NT-2026-0003` 招标公告：明确**不接受联合体投标**；
- `NT-2026-0005` 招标公告：接受联合体投标（可配合 `/admin/remove` 演示撤下后检索消失）。

检索 `联合体投标` 可同时命中上述公告并对比条款原文；示例站详情页的顶部导航、
侧栏与隐藏元素中也放置了含相同字样的干扰文字，用于验证它们不参与检索。

## 页面结构约定（解析契约）

输入网址须与示例站同结构：

- 列表页 `list.html?page=N`：公告链接在 `ul.notice-list a`，分页链接在 `nav.pagination a`；
- 详情页 `detail/<公告编号>.html`：容器 `div.notice[data-notice-type]`
  （`tender`/`correction`/`cancellation`），字段元素
  `#project-no`、`#notice-no`、`#publish-time`、`#deadline`（如有）、
  `#target-notice`（更正/撤销必填）；正文文字放在容器内的 `.notice-body`
  （没有该容器时退化为整个 `div.notice`，并自动剔除导航、侧栏等框架元素）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/app.db`（容器） | SQLite 文件路径 |
| `DEFAULT_START_URL` | `http://sample-site:8000/list.html?page=1` | 抓取表单预填的列表网址 |
| `URL_REWRITE_FROM` / `URL_REWRITE_TO` | 见 compose | 把结果页中的内部网址改写为浏览器可访问地址 |

## 本地开发与测试

```bash
pip install -r app/requirements.txt -r sample_site/requirements.txt pytest
python -m pytest tests/            # 公告链分析、详情页解析、正文提取、FTS 检索、事务原子性与端到端流程
# 手动联调：
cd sample_site && uvicorn main:app --port 8080 &
cd app && DB_PATH=/tmp/app.db DEFAULT_START_URL=http://localhost:8080/list.html?page=1 \
  uvicorn main:app --port 8000
# 浏览器打开 http://localhost:8000/ ，先“开始抓取”，再在“公告正文检索”输入：联合体投标
```

测试文件：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_analysis.py` | 公告链分析（招标 / 更正 / 撤销） |
| `tests/test_scraper_parse.py` | 详情页字段解析与同源判断 |
| `tests/test_search.py` | 正文可见文字提取、三字汉字查询规则、FTS5 trigram 命中、片段高亮、轮次隔离与单事务原子性 |
| `tests/test_end_to_end.py` | 真实 uvicorn 端口跑示例站：抓取 → 检索 → 故障注入保留上一轮 → 撤下公告重抓后不再命中、页面路由 |
