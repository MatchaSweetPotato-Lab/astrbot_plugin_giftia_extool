<div align="center">

# AstrBot Plugin: Giftia ExTool

**为 AstrBot 与 Giftia 提供更丰富强大的 LLM 扩展工具（Function Calling / Tools）**

[![AstrBot](https://img.shields.io/badge/AstrBot-v3.4+-blue.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL%20v3-green.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Author](https://img.shields.io/badge/Author-e.e.-orange.svg)](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia_extool)

</div>

---

## 📖 简介

`astrbot_plugin_giftia_extool` 是专为 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 和 [Giftia (astrbot_plugin_giftia)](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia) 打造的增强型工具扩展插件。

通过注册标准的 LLM Tools（大模型函数调用），使大语言模型和拟人化 AI 伴侣具备主动探索与感知外部特征的能力。目前已内置**用户头像视觉感知与长期画像记忆同步**工具。

---

## ✨ 核心特性

- 🖼️ **用户头像特征智能分析 (`inspect_user_avatar`)**
  - 支持大模型在对话中根据意图自主触发工具调用。
  - 自动解析目标用户的头像图片（支持 QQ 高清/标清及多平台会话头像）。
  - 调用视觉多模态大模型进行精炼的视觉特征转述（精准识别二次元角色、真人自拍、萌宠、风景、表情包、画风与神态等）。

- ⚡ **本地 SQLite 异步高命中缓存**
  - 内置基于 `aiosqlite` 的独立头像特征缓存数据库（`avatar_cache.db`）。
  - 首次分析后自动持久化，后续同一用户提问直接秒级复用本地缓存。
  - 极大减少多模态视觉 API 调用频次与 Token 开销，显著降低响应延迟。
  - 支持 `force_refresh` 强制刷新机制。

- 🔄 **视觉模型多提供商容灾降级（Provider Fallback）**
  - 支持在配置中指定多个视觉大模型提供商（按优先级排列）。
  - 当前提供商调用异常或返回为空时，平滑自动降级重试下一个模型。
  - 未配置时自适应回退使用 AstrBot 当前默认视觉模型。

- 🧠 **与 Giftia 长期画像库无缝联动**
  - 自动检测并联动运行中的 `astrbot_plugin_giftia` 插件。
  - 获取或命中头像特征后，自动将 `avatar_description` 写入 Giftia 用户画像数据库。
  - 让 Giftia 角色在后续日常对话中自然“记住”用户的外貌与头像特征。

---

## 📦 安装方法

### 方式一：管理面板安装（推荐）
1. 打开 AstrBot Web 管理面板。
2. 进入 **插件管理** -> **插件市场**。
3. 搜索 `Giftia ExTool` 或 `giftia_extool`，点击安装并重启生效。

### 方式二：Git 仓库安装
进入 AstrBot 的 `data/plugins/` 目录，克隆本仓库：
```bash
cd data/plugins
git clone https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia_extool.git
```

---

## ⚙️ 配置说明

在 AstrBot Web 管理面板中进入插件配置页即可可视化调整：

| 配置组 / 配置项 | 类型 | 默认值 | 说明 |
| :--- | :---: | :---: | :--- |
| **avatar_config** | `object` | - | 头像查看工具配置组 |
| ├─ `inspect_user_avatar_enabled` | `bool` | `true` | 是否启用头像查看工具（关闭后大模型将无法调用该工具） |
| ├─ `caption_provider_ids` | `list` | `[]` | 视觉大模型提供商列表（按优先级排序）。若留空则自动使用当前默认视觉模型 |
| └─ `auto_sync_to_giftia` | `bool` | `true` | 是否自动将分析得到的头像描述同步写入 Giftia 用户画像库 |

---

## 🛠️ LLM 工具详情

### `inspect_user_avatar`

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
| :--- | :---: | :---: | :---: | :--- |
| `user_id` | `string` | 否 | `""` | 目标用户的数字ID / QQ号 / 唯一标识。若为空则缺省使用发送者 ID |
| `force_refresh` | `bool` | 否 | `false` | 是否强制重新下载头像并调用大模型转述（为 `false` 且已有缓存时直接返回缓存） |

#### 🗣️ 典型交互示例
- **用户**：“你看看我的头像是什么呀？”
  - **Bot**：*(自动调用 `inspect_user_avatar(user_id="<sender_id>")`)* -> “你的头像是一位有着银白色长发、红瞳的二次元动漫少女，看起来非常灵动可爱呢~”
- **用户**：“帮我看看群友 12345678 的头像换成什么了，重新看一下。”
  - **Bot**：*(自动调用 `inspect_user_avatar(user_id="12345678", force_refresh=true)`)* -> “这位群友换成了一张趴在草地上晒太阳的金毛犬头像~”

---

## 📂 项目结构

```text
astrbot_plugin_giftia_extool/
├── core/
│   ├── avatar_fetcher.py   # 头像网络下载与视觉模型转述（含降级机制）
│   ├── database.py         # 基于 aiosqlite 的本地缓存持久化
│   ├── giftia_sync.py      # Giftia 插件实例检测与画像写入联动
│   └── prompts.py          # 头像特征分析专用 Prompt 模板
├── tests/
│   └── test_avatar_tool.py # 单元测试
├── _conf_schema.json       # 配置模式定义文件
├── main.py                 # 插件入口与 LLM Tool 注册
├── metadata.yaml           # 插件元数据
└── README.md               # 项目说明文档
```

---

## 📄 开源许可证

本项目采用 [GNU Affero General Public License v3.0 (AGPL-3.0)](LICENSE) 协议开源。

## 💡 鸣谢与支持

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) - 优秀的对话 AI 机器人框架
- [Giftia](https://github.com/MatchaSweetPotato-Lab/astrbot_plugin_giftia) - 赋予 AI 鲜活性格与长期记忆的拟人化插件
