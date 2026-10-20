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
  - 自动解析目标用户的头像图片（支持 OneBot / NapCat、QQ 官方机器人 WebSocket / Webhook 及多平台会话头像）。
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

- 🕐 **每日活跃用户头像维护（默认关闭）**
  - 每天北京时间 01:00 检查昨日发言且已建立 Giftia 共享画像的 QQ 用户。
  - 使用完整图片的 SHA-256 判断变化，未变化且已同步的头像跳过视觉模型。
  - 自动补全缺失描述、更新已变更头像；同一 QQ 号跨群、跨 Bot 复用识图结果。

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
| ├─ `auto_sync_to_giftia` | `bool` | `true` | 是否自动将分析得到的头像描述同步写入 Giftia 用户画像库；关闭时暂停每日检查 |
| └─ `daily_avatar_check_enabled` | `bool` | `false` | 每天北京时间 01:00 检查昨日活跃且已有画像的 QQ 用户，与头像查看工具开关独立 |

### 每日头像检查

开启 `avatar_config.daily_avatar_check_enabled`，并保持 `auto_sync_to_giftia` 开启。检查任务复用 `caption_provider_ids`，未配置时使用 AstrBot 默认聊天模型，所选模型需要支持图片输入。

任务以北京时间的自然日为范围，检查昨日 00:00 至今日 00:00 的用户发言；系统通知、操作日志、机器人自身消息和没有画像的用户不参与检查。新版 Giftia 按 `(bot_name, user_id)` 共享画像，同一 Bot 内任意群聊或私聊的发言均可触发该用户的维护，每个共享画像只更新一次。

第一版支持绑定 OneBot / NapCat（`aiocqhttp`）适配器的 Giftia Bot。聊天记录没有独立的平台字段，因此混合绑定 QQ 与其他平台的 Bot 暂不参与，避免把其他平台的数字 ID 当作 QQ 号。检查时通过 OneBot 查询登录账号以排除机器人自身；依赖未就绪时稍后重试。

头像固定使用 QQ 的 640 尺寸地址，不在下载失败时切换尺寸，以避免不同尺寸造成误判。旧缓存没有图片哈希时，会重新识图一次建立基线；后续图片未变时直接复用描述。识图缓存按 QQ 号共享，各 Bot 单独记录已同步指纹，因此某个 Bot 写回失败或稍后才活跃时，可以直接重用结果。

下载或识图失败会保留旧记录，单个用户失败不会阻塞后续用户；失败的画像在下次被选中检查时重试。任务通过 Giftia 画像接口仅更新头像字段并刷新缓存，巡检不会为未建档用户创建画像。

每天完成一轮后保存执行日期，重启不会重复执行已经完成的当天任务；若当天 01:00 后启动且尚未执行，会补跑一次。补跑只处理当前所对应的昨日，不追溯停机期间的其他日期。插件卸载时先取消检查任务，再关闭缓存数据库。检查结果记录在日志中。

---

## 🛠️ LLM 工具详情

### `inspect_user_avatar`

| 参数名 | 类型 | 必填 | 默认值 | 说明 |
| :--- | :---: | :---: | :---: | :--- |
| `user_id` | `string` | 否 | `""` | 目标用户的数字ID / QQ号 / 唯一标识。若为空则缺省使用发送者 ID |
| `force_refresh` | `bool` | 否 | `false` | 是否强制重新下载头像并调用大模型转述（为 `false` 且已有缓存时直接返回缓存） |

QQ 官方机器人（`qq_official` / `qq_official_webhook`）使用当前适配器的 `appid` 与目标用户的 OpenID 构造头像地址：`https://q.qlogo.cn/qqapp/{appid}/{user_id}/0`。`user_id` 应传入当前机器人收到的用户 ID，无需填写真实 QQ 号，也无需额外配置 `appid`。

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
│   ├── avatar_monitor.py   # 昨日活跃用户筛选与每日头像维护
│   ├── database.py         # 基于 aiosqlite 的本地缓存持久化
│   ├── giftia_sync.py      # Giftia 插件实例检测与画像写入联动
│   └── prompts.py          # 头像特征分析专用 Prompt 模板
├── tests/
│   ├── test_avatar_fetcher.py # QQ 官方机器人头像下载兼容测试
│   ├── test_avatar_tool.py # 头像工具测试
│   └── test_avatar_monitor.py # 日期边界、变更检测与画像同步测试
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
