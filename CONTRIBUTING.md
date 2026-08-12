# Contributing

感谢你改进 Where Papers Go。

1. 不要提交 `llmapi.json`、API 密钥、查询缓存、向量索引或论文 PDF。
2. 核心检索代码放在 `where_paper_go/`，维护命令放在 `scripts/`，设计说明放在 `docs/`。
3. 保持 LightRAG、精确向量召回、LLM 和 Search API 的强制检索约束。
4. 修改检索逻辑后运行：

   ```bash
   python -m unittest discover -s tests -p 'test_*.py'
   python -m scripts.benchmark_retrieval --format json
   ```

5. 提交前确认 `git status` 中没有凭据、大型生成物或本地缓存。
