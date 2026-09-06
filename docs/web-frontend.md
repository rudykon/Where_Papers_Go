# Where Papers Go Web 前端

本项目提供一个无需 Node.js、无需额外前端框架的同源 Web 工作台。浏览器只负责交互和展示，所有主题检索都由 `where_paper_go.recommender` 执行，因此前端不会绕过强制的 LightRAG、向量、LLM 或 Search API。界面采用 Material You（Material Design 3）视觉系统，前端换肤不会改变检索算法或 API 请求结构。

## 启动

仅限本机开发时可直接运行：

```bash
python3 -m where_paper_go.web_app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/>。生产主拓扑不直接暴露这个 Python 服务：

```text
局域网/外部客户端 -> Nginx :443 (HTTPS + Basic Auth)
                     -> 127.0.0.1:8001 (Where Papers Go)
```

此时生产 `render-systemd` 会把 `127.0.0.1:8001`、仅 loopback 的
直连/受信代理 CIDR、强制 Bearer 认证及 passwd-home 下固定的
`.config/where-papers-go/backend.token` 直接写入审阅后的 unit；unit 不读取
可在重启前被修改的 `EnvironmentFile`。Nginx 必须覆盖客户端传入的转发头，
并把外部 Basic Auth 头替换为同一个私有后端 Bearer；应用在信任
转发身份前必须验证它，未持有密钥的本机用户也无法绕过前置认证。

旧的直连 LAN 前任版本可能仍为 `0.0.0.0:8765`；它没有 TLS 或前置
Basic Auth，不属于新版 closeout 可接受的生产状态。Nginx、受信证书、
htpasswd 和防火墙未就绪时保留前任版本，不要通过修改环境文件把新版
unit 降级为 wildcard listener。

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
| GET | `/api/health/live` | 只报告 Web 进程是否存活，不代表检索已就绪 |
| GET | `/api/health/ready` | 最小 readiness；响应严格只含 `status`、`ready`，未就绪返回 503 |
| GET | `/api/health` | 详细 readiness；检查图谱、向量、LightRAG、不可变 Python/system ABI、精确 worker 进程与 API 配置 |
| GET | `/api/options` | 返回等级、分类、类型和数据规模筛选项 |
| POST | `/api/search` | 仅接受 `application/json`，调用现有强制检索 CLI 并返回 JSON 结果 |

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

前端不保存 API key，也不直接读取 CSV。生产部署时应让
`where_paper_go.web_app` 只监听 `127.0.0.1:8001`，由 Nginx 负责 HTTPS、登录、
按客户端限流和访问日志。仓库的 Nginx 模板对包括两个 health 路径在内的
全部 HTTPS 路径继承全局 Basic Auth；最小 readiness 用来减少响应暴露，
不是绕过认证的公开接口。详细 `/api/health` 只应经已认证代理或本机回环访问。

详细健康证明中的 `checks.python_runtime_identity` 和
`checks.worker_process_identity` 也必须为 true；worker 证明只输出
PID、start ticks、哈希、版本/ABI 和验证布尔值，不泄露源码或 runtime 路径。

仓库现已提供可渲染的 user-systemd 单元、Nginx TLS/鉴权/限流配置、结构化审计、严格 readiness 和可恢复原子替换工具。Nginx 集成测试只在显式设置 `WPG_NGINX_BIN` 时执行；未设置时是前置条件缺口导致的 skip，不是通过。完整启停、升级、回滚、受信证书验证、LAN 边界和管理员待执行步骤见 [生产部署手册](production-deployment.md)。
