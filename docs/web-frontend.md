# Where Papers Go Web 前端

本项目提供一个无需 Node.js、无需额外前端框架的同源 Web 工作台。浏览器只负责交互和展示，所有主题检索都由 `where_paper_go.recommender` 执行，因此前端不会绕过强制的 LightRAG、向量、LLM 或 Search API。界面采用 Material You（Material Design 3）视觉系统，前端换肤不会改变检索算法或 API 请求结构。

## 启动

```bash
python3 -m where_paper_go.web_app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>。如果需要局域网访问，可以将 `--host` 改为 `0.0.0.0`，但建议在反向代理、访问控制和 HTTPS 后使用。

## 页面结构

- 主题输入区：支持题目、摘要、关键词和中文口语化描述，提供常用主题示例。
- 投稿边界筛选：多选 CCF/TH-CPL/CAS/JCR 等级，选择会议/期刊、基础分类、结果数量和已审核范围。
- 悬浮四阶段进度坞：开始检索后固定在视口底部，滚动到任意位置仍能看到 LLM 意图理解、bge-m3 语义召回、LightRAG `mix`、Search API + LLM 重排及最终状态。
- 吸顶状态胶囊：在页面任意滚动位置显示当前阶段、四段进度及系统健康状态。
- 中英双语切换：吸顶栏的 `EN / 中` 按钮会同步切换静态文案、检索进度、结果卡片、详情抽屉和错误提示，并在浏览器中记住选择。
- 结果卡片：显示综合得分、等级、类型、匹配判断、召回信号和可解释的匹配概念。
- 详情抽屉：展示收稿范围、覆盖主题、明确不匹配方向、各路分数和外部/官网证据链接。
- 系统状态：展示图谱、向量、LightRAG、LLM/Search 配置和索引规模。

## API

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/health` | 检查图谱、向量、LightRAG 清单和 API 配置 |
| GET | `/api/options` | 返回等级、分类、类型和数据规模筛选项 |
| POST | `/api/search` | 调用现有强制检索 CLI 并返回 JSON 结果 |

`POST /api/search` 请求示例：

```json
{
  "query": "面向大模型训练的分布式 GPU 通信与资源调度",
  "targets": ["CCF-A", "JCR-Q1"],
  "record_type": "all",
  "areas": [],
  "scopes": ["机器学习系统"],
  "reviewed_scope_only": false,
  "match_official_scope": true,
  "limit": 10,
  "locale": "zh-CN"
}
```

`locale` 由前端根据当前界面语言发送，现有检索命令会忽略不认识的附加字段，因此不改变 LightRAG、向量、LLM 与 Search API 的强制检索链路。投稿名称、官网范围和网页证据等来源内容保留原文，避免界面翻译改变证据含义。

检索失败时 API 返回 4xx/5xx 和 `detail`，前端会显示失败原因；尤其是 Search API 无法访问或没有网页证据时，不会展示降级推荐。

## 代码组织

```text
where_paper_go/web_app.py       # 标准库 HTTP API 和静态文件服务
where_paper_go/static/index.html # 页面结构与交互区域
where_paper_go/static/styles.css # Material You 令牌、组件状态和响应式布局
where_paper_go/static/app.js     # 筛选、请求、进度、卡片和详情抽屉
```

## 视觉与无障碍约定

- 全局颜色、圆角、阴影、字体和动效时长都集中在 `where_paper_go/static/styles.css` 的 `:root` 中；调整品牌种子色时应优先修改这些令牌。
- 页面使用暖白背景、紫色主色、薰衣草色调容器和 24–48px 的层级圆角，不以纯白卡片或重边框划分层次。
- 交互控件包含可见键盘焦点，移动端触控目标不小于 44px，并尊重系统的“减少动态效果”设置。
- 详情抽屉使用对话框语义、焦点约束和关闭后的焦点回收；动态检索进度、结果与系统状态会向辅助技术播报。
- 语言切换会更新页面 `lang`、标题、可访问名称和数字格式；用户已输入的主题、筛选状态和当前结果不会因切换而丢失。
- Roboto 通过 Google Fonts 加载；网络不可用时会自动回退到 Noto Sans、苹方或微软雅黑，不影响使用。

前端不保存 API key，也不直接读取 CSV。生产部署时建议让 `where_paper_go.web_app` 只监听回环地址，由 Nginx/Caddy 负责 HTTPS、登录和访问日志；同时为每个用户增加请求限流和审计记录。
