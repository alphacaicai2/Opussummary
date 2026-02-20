# OpusBrief 项目交接文档

> 创建时间: 2026-02-20
> 创建原因: 工作交接，上下文转移

---

## 项目概述

**OpusBrief** 是一个 AI 驱动的 RSS 智能简报系统，集成了：
- Miniflux RSS 阅读器
- LLM（大语言模型）简报生成
- Discord Webhook 推送
- Web UI 管理界面
- 定时任务调度

---

## 技术架构

```
OpusSummary/
├── main.py                 # CLI 入口
├── database.py             # 数据库操作
├── miniflux_client.py      # Miniflux API 客户端
├── web/
│   ├── server.py           # FastAPI 服务端
│   └── static/
│       └── index.html      # 单页面前端
├── llm/
│   └── client.py           # LLM 统一客户端
├── briefing/
│   ├── templates.py        # 4种简报模板定义
│   ├── generator.py        # 简报生成器
│   └── scheduler.py        # 定时任务调度
├── sender/
│   └── discord.py          # Discord Webhook 推送
└── data/
    └── briefings.db        # SQLite 数据库
```

---

## 最近完成的工作

### 1. 任务创建 UI 重设计

**文件**: `web/static/index.html`

**改动内容**:
- 移除了重复的"启用任务"复选框
- 简化"调度方式"为两个选项：手动触发 / 定时任务
- 定时任务配置嵌套显示在下方
- 两列布局：左侧放文章时间范围，右侧放 LLM 配置和 Webhooks

**关键代码位置**: 任务模态框（Task Modal）

### 2. 模板管理功能

**新增内容**:
- "模板" Tab 用于查看和编辑模板
- 4 个内置模板可以通过 UI 查看和编辑
- 模板弹窗支持编辑系统提示词和用户提示词模板

**API 端点** (在 `web/server.py`):
- `GET /api/templates` - 获取所有模板
- `POST /api/templates` - 创建自定义模板
- `PUT /api/templates/{id}` - 更新模板
- `DELETE /api/templates/{id}` - 删除自定义模板
- `POST /api/templates/{id}/reset` - 重置内置模板到默认

**内置模板**:
1. `general` - FA 通用简报
2. `investment` - AI 投融资简报
3. `ai_product` - AI 产品简报
4. `wechat_mp` - 公众号选题简报

### 3. 模板弹窗高度修复

**问题**: 弹窗太高，无法完整显示

**解决方案**:
- 减少 textarea rows 从 10 到 6
- 添加 `max-h-[90vh]` 和 `overflow-y-auto`
- 使用 flex 布局，内容区可滚动，按钮固定底部

---

## 已知问题

### 1. 服务器需要手动重启

**问题**: 修改 `web/server.py` 后，需要手动重启服务器才能生效

**原因**: 开发模式没有启用 `--reload`

**解决方法**:
```bash
# 找到并杀死旧进程
netstat -ano | findstr :8090
taskkill /PID <pid> /F

# 重新启动
python main.py
```

### 2. /api/templates 返回 404

**问题**: 旧服务器没有模板 API

**原因**: 服务器运行的是旧代码

**解决方案**: 已通过重启服务器解决

### 3. 数据库约束错误（历史问题）

**问题**: `UNIQUE constraint failed: webhooks.is_default`

**位置**: `web/server.py` 第 1520 行

**说明**: 这是之前的错误，可能已在后续修改中修复

---

## 前端状态管理

**文件**: `web/static/index.html`

**全局状态对象**:
```javascript
const state = {
    settings: {},
    llmConfigs: [],
    webhooks: [],
    tasks: [],
    templates: [],  // 新增
    briefings: [],
    categories: [],
    currentTab: 'tasks'
};
```

**关键函数**:
- `loadTemplates()` - 加载模板列表
- `showTemplateModal(template)` - 显示编辑弹窗
- `hideTemplateModal()` - 隐藏弹窗
- `saveTemplate()` - 保存模板
- `resetTemplate()` - 重置内置模板

---

## 后端关键代码

### 数据库表结构

**custom_templates 表**:
```sql
CREATE TABLE IF NOT EXISTS custom_templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    system_prompt TEXT NOT NULL,
    user_prompt_template TEXT NOT NULL,
    required_sections TEXT,
    is_builtin INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
```

### Pydantic 模型

```python
class TemplateOut(BaseModel):
    id: str
    name: str
    description: str
    system_prompt: str
    user_prompt_template: str
    required_sections: list[str]
    is_builtin: bool = True
    is_modified: bool = False
```

---

## 启动和测试

```bash
# 启动 Web UI
python main.py

# 或指定端口
python main.py --port 8090

# 测试 API
curl http://127.0.0.1:8090/api/templates
curl http://127.0.0.1:8090/api/tasks
```

---

## 用户反馈记录

### 2026-02-20 反馈

1. **UI 逻辑混淆**: 原始设计让用户容易混淆"启用任务"复选框和"调度方式"下拉框的功能重叠
2. **模板弹窗高度**: 弹窗太高无法完整显示
3. **内置模板不可见**: 4 个内置模板没有显式展示在 UI 中

### 解决方案实施

- [x] 重新设计任务创建 UI
- [x] 添加模板 Tab
- [x] 修复弹窗高度
- [x] 重启服务器加载新 API

---

## 遗留问题

1. **LLM API 调用失败**: 日志显示 `401 Unauthorized`，可能是测试用的 API Key 无效
2. **markdown 库缺失**: 日志警告 `markdown library not installed`
3. **定时调度器未激活**: 调度器代码已实现但未在服务启动时激活

---

## 文件修改记录

| 文件 | 修改内容 |
|------|----------|
| `web/static/index.html` | 任务 UI 重设计、模板 Tab、模板弹窗高度修复 |
| `web/server.py` | 添加模板 API 端点、添加 custom_templates 表 |

---

## 下一步建议

1. 修复 LLM API Key 配置问题
2. 安装 markdown 库: `pip install markdown`
3. 激活定时调度器
4. 添加更多模板自定义功能
5. 考虑添加模板导入/导出功能

---

## 联系和参考

- 项目根目录: `c:\Users\TonyS\Desktop\OpusSummary`
- Web UI: http://127.0.0.1:8090
- 详细计划文档: `PLAN.md`
