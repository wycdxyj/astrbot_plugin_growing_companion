# 拟人化成长陪伴 · 第三版 v0.3.0

基于原 v0.1.3 升级的 AstrBot 插件：连续人格、分层记忆、情绪惯性、渐进成长、自主复盘、群聊旁听与有冷却限制的主动冒泡。

完整架构、实际配置、完整 SQL、逐文件源码、测试与路线图见 [九部分交付文档](DELIVERY_V3.md)。

## 安装

1. 本版核对 AstrBot **v4.29.4**；最新宿主要求 **Python ≥3.12**。插件本身保持 Python 3.10 语法兼容。
2. 解压目录至 `data/plugins/growing_companion/`，使 `main.py` 直接位于该目录；依赖见 `requirements.txt`（aiohttp）。
3. 在 AstrBot 配置聊天模型。插件不单独保存 API Key。
4. 配置 `father_ids` 为 `平台实例ID:用户ID`，例如 `qq-main:123456`。平台实例 ID 可参考 AstrBot `/sid` 返回的会话前缀。**留空不会自动绑定父亲**。
5. 启用插件。初始阶段默认 `infant`；要立即体验完整聊天，可在首次建库前设置 `persona_init.initial_stage=child`。已有成长数据不被该初值重置。

数据位于 `data/plugin_data/growing_companion/companion.db`。从旧版升级会增量添加社交表与作用域字段，不删除旧记忆。旧版保存的自动绑定键不再授予父亲权限，需在后台显式配置。

## 社交行为

- 群聊普通消息进入观察，提取词语、热点提及与互动方式，保留出处和“听说/待核实”，潜水不调用聊天模型。
- 好奇、共鸣、被提及和想分享增加冒泡概率；没人回应会降低后续意愿并延长冷却。陌生人不会自动成为父亲。
- 所有群聊发言，包括被 @ 的回复，都受单群 90 秒 CD、全局 10 秒 CD、单群每小时 6 次和全局每小时 24 次发言尝试上限约束。状态落库，重启不清空；配置可调整。
- 群聊只使用本群历史与记忆。父亲在群里也不会触发私聊记忆注入。其他群、父女私聊、陌生人私聊彼此隔离。
- 父亲私聊时，可自然讨论从群里产生的抽象价值疑问；保留父亲观点，不将“父亲说过”自动当成客观事实验证。

## 只读命令与脑图

仅父亲**私聊**：`/她状态`、`/记忆列表 [页码]`、`/记忆详情 N`、`/成长报告`、`/关系状态`、`/最近复盘`、`/脑图 [文件]`。

命令使用独立只读连接，不更新记忆、成长、关系或复盘。群内查看命令静默结束。不存在改变学习方向、强制复盘或修改人格的管理命令。

脑图使用 **aiohttp + Three.js**，默认监听 **`0.0.0.0:5208`**，需要访问密钥。密钥在初始化时创建，父亲通过私聊 `/脑图` 获取链接。远程访问需放行端口；Docker 需发布 `5208:5208`。设置 `brainmap_public_base_url` 可以输出完整实际地址。

Three.js r128 和 MIT 许可证已随包提供，在线页面无需外网 CDN。可选数据目录 `three.min.js` 优先加载。浏览器不支持 WebGL 时降级 2D。HTML 文件快照内含记忆数据，渲染库仍可从 CDN 加载，失败时显示 2D。

![脑图合成数据预览](docs/brainmap-preview.png)

## 测试与交付

```bash
python -m pip install -r requirements.txt pytest playwright
python -m playwright install chromium
python -m pytest -q --junitxml=docs/test-results.xml
python tools/build_delivery.py
```

发行包位于插件上级目录 `growing_companion_v0.3.0.zip`，另附 SHA-256。打包只选源码、文档、测试与随包库，不包含数据库、缓存或 `.git`。旧的 `growing_companion.zip` 保留。

本机已执行离线引擎、AstrBot API 桩、aiohttp HTTP 及真实 Three.js/Chromium WebGL 测试。没有真实平台账号与真实模型的端到端测试结果。

当前学习与社交判断是规则型 MVP；向量后端尚未启用。上下文采用 UTF-8 字节保守上界和消息封装余量，强制 3000–5000 的输入预算，并非所有供应商的精确 tokenizer 计数。高并发群聊尚需单写者队列和批量提交；主动冒泡由消息到达驱动，安静的群里不会定时广播。

保持角色沉浸，但认真问及身份会如实回答，不宣称真实生物身体。真人关系、现实求助与正常生活不会被角色关系替代。
