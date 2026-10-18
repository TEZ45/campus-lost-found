# URS 需求覆盖矩阵 — Campus Lost and Found Matching System

> 依据：URS 全文快照（urs_content_v3.md）对照仓库 `campus-lost-found`（2026-10-18 clone）。
> 状态说明：✅ 已实现 ｜ 🔶 部分实现 ｜ ❌ 未实现 ｜ ➕ 本次新增

## 1. 功能需求（REQ）

| 需求 | 优先级 | 现状 | 缺口与实现方案 |
|---|---|---|---|
| REQ-1.1 Access and roles | M | 🔶 仅 Django 登录，无角色/权限分离 | ➕ 注册/登录/登出；角色判断（user/staff/admin）；staff 专属视图装饰器 |
| REQ-1.2 Submit reports | M | 🔶 表单齐全，缺 category | ➕ Report 增加 category 字段；表单校验（CAP-3.1 附件限制） |
| REQ-1.3 Moderate reports | M | 🔶 仅 admin 手工改状态，无拒绝理由 | ➕ 员工审核页（批准/拒绝+理由/要求修正），拒绝理由必填 |
| REQ-1.4 Manage own reports | S | ❌ | ➕ 所有者编辑自己的报告（更改公开内容回 pending 重审）；申请关闭 |
| REQ-1.5 Search | M | ❌ 仅 type 过滤 | ➕ 关键词 + 类型/类别/地点/日期/状态组合过滤 |
| REQ-1.6 Suggested matches | C | ❌ Match 模型无视图 | ➕ 基础匹配建议（同类别+关键词重叠），带理由，不自动批准 |
| REQ-1.7 Submit claims | M | 🔶 可提交，无引用号/状态页 | ➕ 登录用户对 approved+open 的 found 报告提交；返回 claim 引用与状态 |
| REQ-1.8 Review and evidence | M | ❌ 无审核动作 | ➕ 员工审核队列：批准/拒绝（理由）/请求证据；claimant 重新提交（Awaiting Evidence→Pending） |
| REQ-1.9 Exclusive reservation | M | ❌ | ➕ 保留规则：同一报告同时最多一个保留 claim（Awaiting Handover/Dispute/In Progress 共享） |
| REQ-1.10 Dispute review | M | ❌ | ➕ 员工将 Awaiting Handover 移入 Under Dispute Review；暂停发放、保留 reservation；解决后恢复/拒绝 |
| REQ-1.11 Physical handover | M | ❌ | ➕ 发放开始（记录前确认）→ Handover in Progress → 完成/安全中断（恢复 Awaiting Handover 需员工确认） |
| REQ-1.12 Withdraw and invalidate | M | ❌ | ➕ 仅限 Before Handover 状态撤销；完成时失效其他 Before Handover claim；进行中发放阻断撤销/关闭 |
| REQ-1.13 Progress and notices | M | ❌ | ➕ Notice 模型 + 通知页；证据请求/决定/发放结果生成站内通知 |
| REQ-1.14 Work queues and history | M | ❌ | ➕ 员工队列页；ActionHistory 记录 actor/time/reason/result |

## 2. 领域规则（DOM）

| 规则 | 现状 | 实现方案 |
|---|---|---|
| DOM-1 新 claim 须引用 approved+open 的 found 报告 | 🔶 仅限定 report_type="found" | ➕ 增加 status="approved" 校验；关闭报告拒绝新 claim；lost/匹配不证明所有权 |
| DOM-2 员工用私有证据验证 + 确认物品保管 | ❌ | ➕ 审核流程含证据查看；状态机内保证 |
| DOM-3 最多一个保留 claim；其他用户仍可提交 | ❌ | ➕ 保留唯一性检查（模型层 + 视图层） |
| DOM-4 发放前记录开始；完成仅确认移交后；不确定结果保留 reservation | ❌ | ➕ 发放状态机实现 |
| DOM-5 完成关闭报告，仅失效其他 Before Handover claim；终态不可变 | ❌ | ➕ 完成事务逻辑 |

## 3. 安全（SEC）

| 需求 | 现状 | 实现方案 |
|---|---|---|
| SEC-1.1 HTTPS/密码 | 🔶 Django 默认哈希密码 | 生产配置 HTTPS 说明；密码不存明文（Django 默认满足） |
| SEC-1.2 证据仅 claimant+授权员工可读 | ❌ detail 页直接显示全部 claim！ | ➕ 证据访问控制：claimant 本人或 staff 可见；直接附件链接鉴权 |
| SEC-1.3 公开页隐藏联系方式；管理员不自动授权证据；用户不可改他人报告 | ❌ | ➕ 公开页过滤 contact；证据权限独立于 superuser；编辑权限归属校验 |
| SEC-1.4 记录 actor/time/reason/result；用户不可改历史 | ❌ | ➕ ActionHistory 模型 + 只读展示 |

## 4. 验收检查（A1-A6）

| 检查 | 实现方案 |
|---|---|
| A1 并发批准最多一个保留 | 事务 + 唯一性校验；测试覆盖 |
| A2 证据重提交/争议不变量 + 证据访问拒绝 | 状态机 + 权限测试 |
| A3 发放与撤销竞争仅一个成功 | 事务锁；测试覆盖 |
| A4 中断/不确定结果处理 | 状态机 + 测试 |
| A5 完成失效范围正确 | 事务逻辑 + 测试 |
| A6 基本表单/搜索/权限 | 表单校验 + 测试 |

## 5. 容量约束（CAP-3.1，代码层可实现项）

- 报告：1 张图；claim 每次最多 3 张、累计 5 张；单图 ≤5 MB；仅 JPG/PNG → 表单/模型验证

## 6. 实施顺序（对应 URS Review gate）

1. 模型与迁移（category、claim 状态机、Notice、ActionHistory、附件约束）
2. 账户与角色（注册/登录/登出、staff 装饰器）
3. 报告：搜索 + 编辑/关闭 + 员工审核
4. 声明：提交/审核/证据/保留/争议/发放/撤销/完成
5. 通知 + 历史
6. 匹配建议（基础版）
7. 测试（A1-A6）与需求回核
