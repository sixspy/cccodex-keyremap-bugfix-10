# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10935 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放、撤映留痕

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 撤映与孤儿（本题）

撤映口径锁定 **甲路径**，详见 `ops/蒸发修复说明.md`（2026-11-20 锁定，当日不改口）。
甲乙互斥，本仓不混用乙（正式保留打 orphan 标记）。

**甲：撤映同一事务内先迁入冲突区、再删除正式区；迁入或删除任一失败，两者一起回滚。**

撤映成功后五处必须同态：

| # | 位置 | 表 / 接口 |
|---|------|-----------|
| 1 | 正式区 | `formal_entities` / `GET /api/formal` |
| 2 | 冲突区 | `conflicts` / `GET /api/conflicts`（`reason=撤映`,`status=待处置`） |
| 3 | 处置审计 | `disposition_audit` / `GET /api/audit` |
| 4 | 互证卡 | `reconcile_cards` / `GET /api/reconcile-cards` |
| 5 | 映射册 | `mapping_register` / `GET /api/register` |

- 两路同时撤映同一实体占锁：后到者返回 409 且 `contended: true`，记 `outcome=争用` 审计，不重复迁入。
- 孤儿只读查询：`GET /api/orphans`，在册但正式区与冲突区都找不到的登记行；正常应恒为 0。
- 互证卡 `inconsistent` 应恒为 0；五处任一处不同态即失败。
- 严禁只改孤儿角标。

## 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/login` | 登录（admin / 123456） |
| GET | `/api/map-versions` | 映射版本 |
| GET | `/api/mappings` | 键映射 |
| POST | `/api/remap` | 入站重映，进正式区 |
| GET | `/api/formal` | 正式区 |
| GET | `/api/conflicts` | 冲突队列 |
| POST | `/api/revoke` | 撤映（甲路径），实体迁冲突待处置 |
| GET | `/api/register` | 映射册（撤映后仍在册） |
| GET | `/api/reconcile-cards` | 互证卡 |
| GET | `/api/audit` | 处置审计 |
| GET | `/api/orphans` | 孤儿只读查询 |

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10935/  
健康检查：GET http://127.0.0.1:10935/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`
