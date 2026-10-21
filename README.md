# Enterprise RAG KB · 企业级权限感知知识库

<div align="center">

**带部门权限体系的企业 RAG 知识库：钉钉组织同步 · 四级密级 · 检索时强制鉴权 · L4 双人审批 · OCR 扫描件解析 · 招投标打包受控交付**

Apache-2.0 | FastAPI + Qdrant + OpenAI-compatible LLM

</div>

---

## 这是什么 / What is this

一个面向真实企业场景的检索增强生成（RAG）知识库系统。与普通 RAG demo 的区别在于：**权限不是检索完之后做遮挡，而是在向量检索的那一刻由数据库过滤强制生效**——无权限的文档，连标题都不会出现在结果里。

A production-oriented RAG knowledge base for enterprises. Unlike typical RAG demos, access control is enforced **at retrieval time** inside the vector store (Qdrant payload filtering), not post-hoc. Documents a user cannot see never appear in results — not even titles.

## 核心特性 / Features

| | 特性 | 说明 |
|---|------|------|
| 🔐 | **四级密级 L1–L4** | L1 公开 / L2 部门 / L3 全员 / L4 指定人。上传即定级，chunk 携带 ACL payload |
| 🚪 | **检索时强制鉴权** | 全系统唯一出口 `build_acl_filter()`，问答/列表/预览/打包所有召回路径禁止绕开，Qdrant filter 层生效 |
| 👥 | **钉钉组织镜像** | 通讯录部门/成员/负责人全量同步 + Stream 事件实时增量（调岗/离职秒级生效），扫码登录发短期 JWT |
| ✅ | **L4 双人审批** | 敏感文档须部门负责人在钉钉收到通知并审批，通过才入库；状态机持久化，重启不丢 |
| 📄 | **13 种格式解析** | pdf/docx/xlsx/csv/pptx/图片… 扫描版走多模态 OCR；Excel 双形态切片（数据表型每 20 行重复表头 / 文档型句子化） |
| 🔍 | **OCR 质量门禁** | 低置信扫描件自动进人工校对队列，宁可人工过目不误杀 |
| 📦 | **受控打包交付** | AI 按需求组文档清单 → 按归属部门拆单审批 → 生成仅发起人可开、7 天有效、带姓名水印的受控链接，每次访问记账 |
| 📊 | **审计与命中统计** | 谁查过什么、看过哪篇、导出过什么全量落库；文档命中热度、零命中识别 |
| 🕸 | **关系驾驶舱** | 公司→部门→人三层组织图（钉钉只读镜像），部门负责人高亮、灰度状态一览 |

## 架构 / Architecture

```
┌─────────────┐   JWT    ┌──────────────────────────────┐
│ 钉钉扫码登录 │ ───────> │  FastAPI (API + 内置前端)      │
└─────────────┘          │  user / admin 双界面          │
                         ├──────────────────────────────┤
  钉钉开放平台             │  接入层: 格式解析 / OCR / 门禁  │
  ├─ 通讯录同步 ─────────>│  业务库: SQLite users/depts/  │
  ├─ Stream 事件 ────────>│   approvals/audit/hits       │
  └─ 工作通知(审批/交付) <─├──────────────────────────────┤
                         │  检索层: Embedding API         │
   OpenAI 兼容 LLM <─────│  → Qdrant(ACL filter)         │
                         │  → Rerank API → Prompt 组装   │
                         └──────────────────────────────┘
```

检索流水线：`query → dense 召回(TOP_K_DENSE) ∧ ACL filter → rerank(TOP_K_RERANK) → LLM 生成(带引用)`。ACL filter 在 Qdrant 查询条件层拼装，无权限点在向量库层面就不返回。

## 快速开始 / Quick Start

```bash
# 1. 起 Qdrant
docker compose up -d

# 2. 配置
cp .env.example .env   # 填入 LLM_API_KEY / DASHSCOPE_API_KEY 等

# 3. 安装依赖
python -m venv venv && ./venv/bin/pip install -r requirements.txt

# 4. 启动
./venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 9012

# 5. 验证
curl http://127.0.0.1:9012/health
```

浏览器打开 `http://<server>:9012/` 即可问答；`/admin` 为管理控制台。

**不接钉钉也能跑**：钉钉为可选项（组织同步/扫码登录/审批通知）。未配置 `DINGTALK_*` 时，系统以本地 `users/depts` 表为准，可手工维护或由管理员角色直接工作。

## 项目结构 / Layout

```
app/
├── api/           # FastAPI 路由: chat/upload/admin/auth/directory/packages
├── acl/           # 权限核心: identity(身份解析) filter(ACL 唯一出口) gray(灰度)
├── dingtalk/      # 钉钉: client(API) store(SQLite 组织库) sync(全量) events(Stream)
├── services/      # 解析(router 格式分流) ocr_parser embedder reranker retriever
│                  # llm gate(质量门禁) ocr_quality tabular_parser(Excel 双形态)
│                  # l4_approvals l4_notify hit_stats packages packages_link
│                  # package_proposer(AI 组清单) graph(驾驶舱) intent_router
├── core/          # session(JWT)
├── static/        # 内置前端: index(员工问答) admin(控制台) package(受控交付页)
├── data/          # 运行数据(uploads/history/SQLite)——.gitignore 已排除
docs/              # 架构与调研文档
tests/             # 124 项: ACL 隔离/审批状态机/校对队列/打包/驾驶舱...
```

## 权限模型速览 / ACL Model

```python
# 唯一出口, 所有召回路径强制经过 (app/acl/filter.py)
def build_acl_filter(user):
    # L1 全员 | L3 全员 | L2 ∩ 我的部门 | L4 ∩ 指定名单 | 我 own 的文档
    return Filter(must=[...level/dept_id/owner/extra_acl 组合...])
```

- 文档入库时把 `level / dept_id / owner_userid / extra_acl` 写进每个 chunk 的 payload。
- 用户调岗 → 钉钉事件更新 `users.dept_ids` → **下一次检索自然生效**，无需逐个文档改权限。
- JWT 里只放 userid，部门每次查询实时取——避免 token 过期窗口内的权限漂移。

## 文档 / Docs

- [`docs/00-整体架构与使用指南.md`](docs/00) — API 全表 + 部署运维
- [`docs/07-知识库RAG-V2搭建方案.md`](docs/07) — 权限体系设计原理（为什么在检索时鉴权）
- [`docs/11-文件格式扩展需求方案.md`](docs/11) — 13 种格式与 Excel 双形态切片规则
- [`docs/08-RAG热门开源项目调研.md`](docs/08) — 2026 RAG 开源生态对比

## 测试 / Testing

```bash
./venv/bin/python -m pytest tests/ -v
```

覆盖：ACL 越权攻击面（跨部门不可见/伪造 token/历史隔离/删除越权）、L4 审批状态机、校对队列、批量导入、打包拆单、驾驶舱聚合。全部进程内 mock 存储，无需外部服务。

## License

Apache-2.0
