# Contributing

感谢你改进 Where Papers Go。

1. 不要提交 `llmapi.json`、API 密钥、查询缓存、向量索引或论文 PDF。
2. 核心检索代码放在 `where_paper_go/`，维护命令放在 `scripts/`，设计说明放在 `docs/`。
3. 保持 LightRAG、精确向量召回、LLM 和 Search API 的强制检索约束。
4. PR 的固定验收入口是 `fixed-pr-gates`。本地提交前运行：

   ```bash
   WPG_VALIDATOR="$PWD/scripts/validate_pr_gates.py"
   WPG_MANIFEST="$PWD/.github/pr-gate-manifest.json"
   /usr/bin/python3.12 -I -S "$WPG_VALIDATOR" \
     static --base main --manifest "$WPG_MANIFEST"
   "$PWD/.venv/bin/python" -I -S "$WPG_VALIDATOR" \
     tests --suite full --manifest "$WPG_MANIFEST"
   "$PWD/.venv/bin/python" -I -S "$WPG_VALIDATOR" \
     retrieval --manifest "$WPG_MANIFEST"
   "$PWD/benchmark_artifacts/m3_model_runtime_20260828/venv/bin/python" \
     -I -S "$WPG_VALIDATOR" tests --suite model-focused \
     --manifest "$WPG_MANIFEST"
   ```

   `full` 必须固定为 489 项、485 通过和 4 个精确 allowlist skip；
   `model-focused` 必须 6/6 且无 skip；retrieval 必须在离线 socket
   guard 下从 tracked 数据重建临时图并达到 7/7、micro Recall@K 1.0。
   CI 用固定 uv/action/Python 版本、tracked `uv.lock` 和独立 PEP 751
   model lock 建立各自环境；执行测试时再进入只有 loopback 的 Linux network
   namespace，不改动 runner 宿主网络，同时保留 Python socket audit。
   因此 Search、LLM
   或 embedding provider 无法被调用。本地需要复制该强隔离时使用：

   ```bash
   WPG_VALIDATOR="$PWD/scripts/validate_pr_gates.py"
   WPG_MANIFEST="$PWD/.github/pr-gate-manifest.json"
   WPG_OFFLINE_PYTHON="$HOME/.wpg-pr-gate-venv/bin/python"
   bash scripts/run_linux_offline_gate.sh "$WPG_OFFLINE_PYTHON" \
     -I -S "$WPG_VALIDATOR" retrieval --manifest "$WPG_MANIFEST" \
     --require-os-isolation
   ```

   先在 checkout 与 `/tmp` 之外准备上述锁定依赖环境。wrapper 的首个参数
   必须是绝对可执行文件路径，且不能位于 noexec checkout 或会被遮蔽的
   `/run`、`/tmp`、`/dev/shm`；在 GitHub Actions 中还必须保留 runner 提供的
   `RUNNER_TEMP` 和 `RUNNER_TOOL_CACHE`。该 Linux 强隔离入口要求非 root
   调用者具备无交互 `sudo`。

5. [PR gate manifest](.github/pr-gate-manifest.json) 固定关键 tracked 文件、
   retrieval case、credential 合成夹具和 `docs/Where-Papers-Go.png` 的 Git
   blob/SHA-256。修改被固定的文件时，先运行
   `/usr/bin/python3.12 -I -S "$PWD/scripts/validate_pr_gates.py" manifest-template`，
   逐项审查输出后再显式
   更新 manifest；不得为了让 CI 通过而放宽凭据或 logo 规则。
6. 提交前确认 `git status` 中没有凭据、大型生成物或本地缓存。
7. 稳定 job 名本身不是独立信任根：PR 可以同时修改 workflow、validator、
   manifest 和测试。GitHub 上必须另外用受保护 ruleset 要求
   `fixed-pr-gates`、对 `.github/`、`scripts/validate_pr_gates.py`、
   `scripts/run_closeout_tests.py` 和 manifest 要求 Code Owner 审核，最好使用
   GitHub 的 required workflow 作为 base-side policy。不得用
   `pull_request_target` 直接执行未信任 PR 代码。仓库内
   `.github/CODEOWNERS` 使用 `@rudykon` 声明审查责任，但只有 base 分支已
   包含该文件且 GitHub ruleset 启用 required code-owner review 时才会强制
   执行；该文件与 PR 内 workflow 都不是独立信任根。这些仓库外设置不能由
   PR 自行证明或启用。
8. 模型专项同时固定并验证 `torch`/`transformers`/`safetensors`/
   `tokenizers` 四个直接版本及完整 25 包传递闭包；CI 只按 PEP 751 lock 中
   的 HTTPS wheel URL/SHA-256 使用 `--require-hashes --no-deps --no-index`
   安装。wheel 构建工具另有独立的四包 PEP 751 hash lock。
