# 自定义计划

AstrBot 的自然语言计划插件。每个计划有一个主数据集，**默认使用通用表格**，也可切换为打卡面板、待办列表或日历；提供统计和随笔作为可选扩展区块。

## 安装

需要 Python 3.12+、AstrBot 4.27+。将插件目录放入 AstrBot 的 `data/plugins/astrbot_plugin_custom_plan`，或在插件管理中上传本地 ZIP 包。

在 **AstrBot 实际使用的 Python 环境**安装依赖与 Chromium：

```sh
python -m pip install -r data/plugins/astrbot_plugin_custom_plan/requirements.txt
python -m playwright install chromium
```

Linux / Docker 通常需要先安装浏览器系统依赖：

```sh
python -m playwright install --with-deps chromium
```

安装中文字体（例如 Noto Sans CJK），并在插件配置中填写本地 `font_path`。没有配置时使用系统字体；缺少中文字体的系统可能显示方框。可由管理员通过 `browser_executable` 指定已有 Chromium 或 Edge。插件不会在启动时自动下载浏览器，也不使用远程截图服务。

启用插件后，确认 AstrBot 已开启模型工具调用。模型通过五个共用工具完成所有类型的计划操作。

## 使用

可以直接对话：

* “创建一个阅读打卡计划，每天读满 20 页，从今天生效。”
* “今天读了 30 页；补记昨天的 25 页。”
* “创建一个旅行待办计划，加上订酒店、买车票。”
* “酒店订好了；买车票改到周五前完成。”
* “把这个计划切成日历，显示九月。”
* “加一张统计卡片，显示全部记录的合计，再加一个随笔区块。”
* “把这个计划以后默认显示为桌面版。”

相对日期按计划时区解释，模型查询可获得该时区的当前日期。对象不明确时应先澄清。所有预设创建后均使用表格视图，需要时再切换。

| 命令 | 用途 |
| --- | --- |
| `/plan` | 查看帮助 |
| `/plan list [页码]` | 列出当前会话可查看的计划 |
| `/plan create 名称 [预设]` | 创建计划，预设为 `generic/checkin/goal/todo`，默认 `generic` |
| `/plan show 计划ID [页码]` | 发送本地生成的图片 |
| `/plan exec 操作 JSON参数` | 无需 LLM 的完整操作入口，适合调试和精确操作 |

图片是静态展示，修改通过对话或命令执行。截图失败会发送文字摘要，数据仍可查询和修改。

## 布局与主题

插件配置 `default_render_layout` 决定**创建计划时**的初始样式，默认 `mobile`。创建时会把选定样式写入 SQLite 中该计划文档的 `render_layout` 字段；以后发送图片直接使用该字段。调整插件配置只影响之后创建的计划，不会改变已有计划。

主题独立保存为 `render_theme`，创建时取插件配置 `default_render_theme`，当前默认且内置的主题为 `forest`（清新绿）。同一主题可用于移动版和桌面版，也适用于表格、打卡、待办、日历、统计和随笔。更改主题不改变布局、分页或记录内容，更改全局默认主题不影响已有计划。

| 样式 | 图片排版 | 表格 / 待办的默认每页记录数 |
| --- | --- | --- |
| `mobile` 移动版 | 640px 宽，正文 26～28px；表格改为纵向记录卡片，待办日期放在标题下方，日历使用月历概览与日期摘要 | 8 条 |
| `desktop` 桌面版 | 1000px 宽，横向表格与七列月历 | 20 条 |

两种样式都支持显式设置 `page_size`（1～30），每页展示最多 6 个字段，通过 `column_page` 翻页。统计与随笔跟随所选样式排版；统计范围仍独立于主视图分页。图片生成后不会随查看设备自动切换样式。

创建时可显式传入 `render_layout` 和 `render_theme`；已有计划通过 `custom_plan_manage` 的 `update` 修改，也可以直接对话要求切换。`custom_plan_query` 的计划列表查询同时返回可用 `themes`、全局创建默认值，以及各计划保存的布局和主题。精确命令示例（ID 和版本号须替换为查询结果）：

```text
/plan exec update {"plan_id":"p_实际ID","revision":3,"render_layout":"desktop","render_theme":"forest"}
```

样式修改遵循原有权限、版本检查和撤销机制。`render_layout` 只接受 `mobile` / `desktop`；`render_theme` 和 `default_render_theme` 只接受已注册的主题 ID。字段缺失、非法主题或主题文件缺失会明确报错，不会在发送时静默套用全局配置或其他主题。

数据库当前版本为 3。版本 1 升级时，为缺少 `render_layout` 的已有计划（含已删除计划）及撤销快照补写 `mobile`；版本 1、2 升级时，为缺少 `render_theme` 的同类文档补写 `forest`，保留现有绿色外观。已有合法值保持不变，非法值报错；版本 2 中缺失布局字段视为损坏，不自动补齐。迁移在同一事务中完成，与当前插件默认值无关，不改变记录、计划版本号或更新时间，失败会回滚。之后启动不重复补写。迁移测试覆盖历史撤销、失败回滚及重复启动；版本 1、2 升级入口保留至明确停止支持对应版本数据库升级时，届时删除相应迁移分支并对旧版本明确报错，运行时不保留旧文档格式分支。

## 开发新主题

样式分为三层，渲染时组合加载：

| 位置 | 职责 |
| --- | --- |
| `assets/base.css` | 公共组件结构，使用 CSS 变量引用布局尺寸和主题外观 |
| `assets/layouts/mobile.css`、`desktop.css` | 字号、间距、排列方式等布局设置 |
| `assets/themes/forest.css` | 完整的配色、状态色、圆角和阴影变量，可按布局覆盖外观变量 |
| `appearance.py` | 可信布局和主题注册表；布局统一定义画布宽度、默认分页、CSS 文件及模板前缀 |

新增外观主题只需添加 CSS 文件并注册：

1. 复制 `assets/themes/forest.css` 为新主题文件，例如 `assets/themes/midnight.css`。保留完整变量定义，修改背景、正文、卡片、达标/未达标状态、边框等颜色和圆角、阴影。主题文件只定义外观变量，尺寸与排列在布局层维护。
2. 在 `appearance.py` 的 `THEMES` 中加入 `"midnight": Theme("深色", "themes/midnight.css")`。渲染器、字段校验、主题查询和预览脚本均从此注册表读取，无需修改业务逻辑或复制视图模板。
3. 运行测试，并生成两种布局的四种视图预览，检查文字对比度、状态辨识和溢出。注册并重载插件后，配置 `default_render_theme` 或计划字段 `render_theme` 即可使用新主题 ID。

主题文件通过注册表中固定的本地路径加载，用户只选择 ID，不能指定任意 CSS 路径。默认主题配置使用文本输入并由注册表校验，因此新增主题无需同步维护配置中的选项列表。更换主题只替换外观变量；时间轴等内容结构变化应通过视图模板实现。

## 数据与四种主视图

| 视图 | 配置要求 | 展示方式 |
| --- | --- | --- |
| `table` 通用表格（默认） | 任意字段，也允许空数据集 | 每页最多 30 条记录、6 个字段，移动版纵向卡片、桌面版横向表格，支持记录与字段分别翻页 |
| `checkin` 打卡面板 | 打卡预设及其日期字段 | 近 28 天、当天数量、达标状态、连续天数 |
| `todo` 待办列表 | 标题、状态字段及完成状态值 | 未完成优先，可按字段排序，支持分页 |
| `calendar` 日历 | 日期字段，可选标题或数值字段 | 月历、每天两条摘要及其余记录数量，移动版将摘要放在月历下方，提示无日期记录 |

主视图可配置 `filters`、排序字段和可见性。切换、隐藏视图不复制或删除记录。扩展区块直接读取主数据，统计默认使用全部记录，可单独配置筛选范围，不受主视图筛选和分页影响。

* `generic`：空字段与空记录，由用户逐步配置。
* `checkin`：日期、数量、备注；默认每日每人一条、数量 1 即达标，允许补签。数量非负，不允许未来打卡。可调整每日阈值和生效日期；阈值不能追溯修改过去日期，历史记录按当时规则计算。是否允许补签和多次记录是写入规则；已有重复日期记录时不能直接关闭多次记录。
* `goal`：日期、数量、备注，加一张绑定目标值的统计卡片；数量非负，更正通过修改原记录完成。
* `todo`：任务、状态、可选截止日期、备注；完成与重新打开时由程序维护 `completed_at`，调整截止日期不改完成时间。

打卡记录带不可由模型伪造的作者。个人与受保护群计划的打卡面板显示创建者，公开群计划显示当前请求用户；统计区块仍按自己的范围计算。

先创建的通用计划也可以通过 `rules` 绑定已有字段：打卡使用 `kind/date_field/amount_field`，累计目标再提供 `target`，待办使用 `kind/title_field/status_field/done_value/pending_value`。绑定会校验全部记录，沿用原记录 ID；初始规则用于解释已有数据，需要用户确认数据含义，已有已完成任务的完成时间记为绑定时刻。首版不直接互换已绑定的业务类型，避免隐式数据迁移。

## 共用工具

| 工具 | 能力 |
| --- | --- |
| `custom_plan_query` | 列出计划、查询字段、规则和分页记录 |
| `custom_plan_manage` | 创建、修改元数据、软删除和撤销 |
| `custom_plan_records` | 批量新增、修改、删除记录 |
| `custom_plan_configure` | 字段、四种视图、业务规则和区块配置 |
| `custom_plan_render` | 按页码或月份生成并发送看板 |

写操作除创建外，必须提供查询得到的 `plan_id` 和 `revision`。记录值以 `field_id` 为键，修改记录使用稳定的 `record_id`。版本冲突返回错误，重新查询后才能决定是否重试；同一条平台消息中的相同操作会去重，不会重复累计。

字段配置完整替换字段定义；遗漏的字段会删除该列数据，被规则、视图或区块引用的字段不能直接删除。更改字段类型会检查全部已有记录。删除或隐藏区块不删除主数据。

统计支持 `count/sum/average/min/max`。空值不参与数值统计；没有有效数值时，平均、最小、最大显示“—”。`target` 可设置独立目标，累计目标预设的卡片使用 `target_from_goal: true` 跟随计划目标。随笔支持纯文本，不执行 HTML、模板或脚本。首版内置区块为统计和随笔；里程碑等仅是后续可注册的扩展类型。

## 精确操作示例

先创建并查询：

```text
/plan create 旅行准备 todo
/plan exec query {"plan_id":"p_实际ID"}
```

新增任务，以下版本号需要替换成实际查询结果：

```text
/plan exec add_records {"plan_id":"p_实际ID","revision":1,"records":[{"values":{"title":"订酒店","due":"2026-10-01"}}]}
/plan exec view {"plan_id":"p_实际ID","revision":2,"type":"todo"}
```

切换日历与隐藏主视图：

```text
/plan exec view {"plan_id":"p_实际ID","revision":3,"type":"calendar","date_field":"due"}
/plan exec view {"plan_id":"p_实际ID","revision":4,"visible":false}
```

添加独立随笔：

```text
/plan exec block_add {"plan_id":"p_实际ID","revision":5,"type":"notes","title":"出发前提醒","config":{"text":"带好证件，提前确认入住时间。"}}
```

## 权限与恢复

| 归属 / 模式 | 权限 |
| --- | --- |
| `person/private` | 本人在私聊中读写 |
| `person/shared` | 本人在私聊及群聊中读写；发到群里的图片对群内成员可见 |
| `group/protected` | 所属群内创建者读写，群员只读 |
| `group/public` | 所属群内所有群员读写，包括修改字段、权限、删除和撤销 |

身份来自 AstrBot 消息事件，以平台实例隔离。不同平台或不同适配器实例不会自动共享计划。群计划只能在所属群访问，私聊中不推断群成员身份。私聊创建默认个人私有，群聊创建默认群组受保护；群内创建个人计划需显式选择 `shared`。

SQLite 保存于 `data/plugin_data/astrbot_plugin_custom_plan/plans.sqlite3`，插件更新不会覆盖数据。备份运行中的数据库时使用 SQLite 在线备份；或停止 AstrBot 后复制数据库，避免遗漏 WAL 中的数据。

每次修改在事务中保存版本和操作者，保留最近 20 次变更快照。首版只允许撤销当前最近一次非创建、非撤销操作，不支持连续撤销。计划删除为软删除，可通过 `query` 的 `include_deleted:true` 找回 ID 和版本后撤销；撤销仍检查权限。

## 资源边界

首版每个用户最多 100 个计划（含软删除），每个计划最多 2,000 条记录、24 个字段、12 个区块和 4 MB 数据；每批写入最多 50 条记录。截图串行执行，默认最多等待 4 个任务，总超时 40 秒，像素倍率为 1，最多 800 万像素或 8,000 像素高。

页面复用同一浏览器进程，任务使用独立上下文。模板与 CSS 随插件提供，禁用页面脚本和外部网络请求。生成前和发送前分别鉴权，临时图片在发送完成或失败后清理。禁用插件时关闭浏览器。

## 开发验证

```sh
python -m pip install -r requirements.txt pytest pytest-asyncio ruff pillow
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

设置 `CUSTOM_PLAN_BROWSER` 为本地 Chromium 可执行文件路径，可启用真实截图测试；其余测试不启动浏览器。有相邻 AstrBot 源码及其依赖时，还会执行实际插件工具注册与消息发送集成测试。所有测试使用隔离的数据目录。

使用 `python scripts/preview.py --theme forest --layout mobile --browser "浏览器可执行文件路径"` 可生成四种视图的示例图片，保存到 `dist/previews/forest/mobile/`，并输出冷启动与复用浏览器后的耗时；`--layout desktop` 生成桌面版并保存到 `dist/previews/forest/desktop/`。`--theme` 接受已注册的主题 ID，省略时使用 `forest`。已安装 Playwright Chromium 时可省略 `--browser`。使用 `python scripts/package.py` 生成可导入的 ZIP，排除虚拟环境、测试数据和示例图片。
