# 明斯基时刻预警监控

每天自动拉取 22 项金融脆弱性指标，按阈值评估风险。有指标状态升级或回落时自动推送预警，同时生成 HTML 看板。
只依赖 Python 3 标准库，不需要安装任何依赖包。

## 文件结构
| 路径 | 说明 |
|---|---|
| `monitor.py` | 主程序 |
| `config.json` | 指标、阈值、推送渠道配置 |
| `install_schedule.sh` | 安装或卸载 macOS 定时任务（launchd） |
| `reports/latest.html` | 可视化看板（每次运行覆盖） |
| `reports/latest.json` | 最新评估结果（机器可读） |
| `data/state.json` | 上次各指标的状态，用来判断是否有变化 |
| `data/history.csv` | 每次运行的综合分与各指标状态历史 |
| `logs/` | 定时任务日志 |

## 常用命令
```bash
cd /Users/xiangfangwei/.gemini/antigravity/scratch/minsky-monitor
python3 monitor.py --open         # 立即运行并打开看板
python3 monitor.py --dry-run      # 试运行：不推送、不改状态
python3 monitor.py --force-notify # 强制推送当前摘要
python3 monitor.py --test-notify  # 测试推送渠道
./install_schedule.sh             # 安装定时任务（每天 08:30、21:30）
./install_schedule.sh uninstall   # 卸载定时任务
launchctl kickstart gui/$(id -u)/com.minsky.monitor   # 立即触发一次定时任务
```

## 预警逻辑
- 每个指标可以配置多条规则，取最严重的一条作为该指标的状态：
  - `level`：绝对水平
  - `change`：N 期变化，单位可以是绝对值或百分比
  - `drawdown`：相对 N 日高点的回撤
- 状态分三级：0 正常、1 关注、2 警报。综合风险分 = Σ(权重×状态) ÷ Σ(权重×2) × 100。
- **只在状态变化时推送**，避免每天被重复打扰：
  - 某个指标升级（比如正常 → 关注）；
  - 综合风险等级升级；
  - 指标回落（可以关闭）。
- 也可以开启 `daily_summary`，每天固定推送一次摘要。
- 如果有大量数据获取失败，也会推送提醒。

## 推送渠道（在 `config.json` 的 `notify.channels` 中开启）
| 渠道 | 适用场景 | 需要的配置 |
|---|---|---|
| `macos` | 本机桌面通知（默认开启） | 无 |
| `bark` | iPhone 推送（App Store 搜索 Bark） | `device_key` |
| `serverchan` | 推送到微信（sct.ftqq.com） | `sendkey` |
| `telegram` | Telegram Bot | `bot_token`, `chat_id` |
| `email` | 邮件（QQ 邮箱、Gmail 等，需要 SMTP 授权码） | SMTP 信息 |
| `webhook` | Slack、飞书、钉钉或自建服务 | `url` |

密钥可以直接写进 config.json，也可以写成 `${BARK_KEY}` 这种形式从环境变量读取。
走定时任务时，环境变量要加到 `install_schedule.sh` 生成的 plist 的 `EnvironmentVariables` 里。

## 无法自动获取的信号
资本开支指引、GPU 租赁价格、CDS 报价等没有免费 API，列在看板底部的“需要人工跟踪的信号”里，需要自己定期查看。

> 仅供研究参考，不构成投资建议。阈值是经验设定，请结合自己的判断调整。
