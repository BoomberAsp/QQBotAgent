# Lucima QQ 机器人

机器人支持私聊 `/璐茜玛`，也可使用 `/璐西玛`、`/露西玛`、`/lucima`。游戏命令在智能体处理之前执行。`/璐茜玛 图鉴 夏妮` 读取 Lucima 仓库内置图鉴，无需登录。`/璐茜玛 兑换 <兑换码>` 一次申请兑换一个码。现有 `/兑换码` 继续查询公开兑换码。

## 部署

1. 使用两个仓库的 `dev` 版本。以下示例假设机器人、Nginx 和登录服务都在同一台 Linux 主机的同一个网络命名空间。桥接服务只监听 `127.0.0.1:8092`，容器部署需要让机器人和反代能访问这个私有监听地址。
2. 在机器人现有虚拟环境更新 `QQBot/requirements.txt`。在 Lucima 的独立虚拟环境安装 `httpx==0.28.1` 和 `websockets==13.1`。桥接服务可用机器人虚拟环境，外部 Lucima 执行进程使用配置指定的解释器。
3. 用 `python -c 'import secrets; print(secrets.token_urlsafe(32))'` 生成私有接口密钥。用机器人虚拟环境执行 `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` 生成凭证密钥，单独保存。两个值都应通过权限为 `0600` 的配置加载，凭证密钥应长期稳定。
4. 在 `QQBot/.env` 设置 `LUCIMA_INTERNAL_TOKEN`，并重启机器人。桥接服务通过环境变量读取下列配置，由服务管理器加载。

   ```dotenv
   LUCIMA_INTERNAL_TOKEN=<机器人和桥接服务共用的接口密钥>
   LUCIMA_VAULT_KEY=<Fernet 凭证密钥>
   LUCIMA_PUBLIC_ORIGIN=https://lucima.oneweblog.cn
   LUCIMA_ROOT=/srv/lucima-tools
   LUCIMA_PYTHON=/srv/lucima-venv/bin/python
   LUCIMA_BRIDGE_DATA=/srv/data/lucima-bridge
   ```

5. 创建 `LUCIMA_BRIDGE_DATA`，让服务账号拥有该目录并设置为 `0700`。从 `QQBotAgent/QQBot` 启动 `/path/to/bot-venv/bin/python -m lucima_bridge`。同一个数据目录只允许一个桥接实例。`LUCIMA_INTERNAL_URL` 默认 `http://127.0.0.1:8092`，机器人可配置自己的私有地址。
6. 配置 `lucima.oneweblog.cn` 的 DNS 和 HTTPS。将下列配置放入该域名的 HTTPS `server` 块，再检查并重新加载 Nginx。登录服务使用独立域名和 Cookie，公开站点只转发此处列出的页面及登录接口。

   ```nginx
   access_log off;
   client_max_body_size 8k;
   client_body_buffer_size 16k;
   client_body_timeout 10s;

   location ~ ^/(?:$|login\.(?:js|css)$|api/(?:claim|session|login)$) {
       proxy_pass http://127.0.0.1:8092;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_request_buffering off;
       proxy_buffering off;
       proxy_read_timeout 120s;
   }

   location / {
       return 404;
   }
   ```

## 试运行检查

- 在 QQ 私聊发 `/璐茜玛 登录`。用二维码或链接打开网页。先在原私聊发送网页上的核对命令，再用本人测试账号授权。分别检查两种 QQ 渠道的图片和链接送达情况。
- 转发二维码后，换另一个 QQ 身份尝试核对，应失败。重复打开同一张二维码，应提示重新申请。群聊命令只引导到私聊。
- 分别勾选和取消勾选「记住登录」进行授权。临时授权保留 15 分钟或到服务重启为止。选择记住登录时，服务最多保留续期令牌 30 天，游戏上游可提前让令牌失效。`/璐茜玛 解绑` 可清除绑定与本服务内凭证。绑定归属在临时令牌过期后仍保留，直到显式解绑。
- 用 `/璐茜玛 状态` 查看任务结果。全站同时只执行一个登录或游戏任务，队列最多五条，每个身份最多一个待处理操作。`/璐茜玛 取消` 可移除排队中的任务。签到、邮件、基地领取和兑换有实际游戏副作用，先用可处置的授权测试账号确认行为。基地入口包括动力、成长药剂和升星水晶。任务可能导致玩家当前客户端下线。
- 公网访问 `/internal/command` 应返回 404。验证两人同时提交只出现一个正在执行的进程，并检查超时后下一个任务可以启动。首版限时约为登录 110 秒、游戏任务 100 秒。

离线测试从 `QQBot` 目录运行 `python -m unittest test_lucima_bridge`。测试使用虚拟凭证和受控子进程。

## 进阶任务与回执

- 在官方 QQ 私聊发送 `/璐茜玛` 打开按钮菜单，或用 `/璐茜玛 进阶` 查看派遣奖励、成就、佣兵团、免费招募、关卡查询和装备筛选。按钮沿用现有的官方 Markdown 与键盘开关。OneBot 以及按钮不可用时会显示文字命令。
- `领派遣` 领取已完成奖励。`领成就` 领取每日及每周已达成的阶段奖励。`团签到` 核对当天签到。`团周奖` 领取已达成的个人周任务和全团积分奖励。`捐赠金币` 或 `捐赠证明` 会先显示一次捐赠的消耗并请求确认。`免费招募` 需要确认，执行时重新检查账号快照中的免费次数。
- `讨伐关卡` 和 `元素关卡` 可查已解锁关卡。`筛装备 [套装名]` 按副属性分数列出前六件匹配装备。扫荡执行需要正式支持的扫荡入口，派遣续派及相关消耗另行接入。
- 私聊发起的任务结束后会返回结果文字和任务回执 PNG，回执记录 Lucima 可确认的结果、完成时间和编号。用 `/璐茜玛 回执` 可取回一小时内的最近回执。回执由任务服务生成，属于执行记录。
- 更新时同时部署 Lucima 执行端与 QQBot 桥接端，然后重启桥接和机器人。桥接重启会清空临时会话、尚未执行的队列和回执。

## 自然语言操作

在原私聊直接说「帮我领星陨邮件」「查一下我的装备」或「我要捐赠一次金币」，机器人会先发待确认卡。QQ 官方渠道可点击「确认执行」，也可发送卡片上带编号的确认命令。查询、扫码入口和账号操作同样经过确认。每张卡五分钟内有效，绑定发起人的 QQ 渠道、游戏账号和具体参数。需要改参数时重新发起。

通过确认的任务进入原任务队列，完成后回传结果和 PNG 凭证。网页账号密码与网页核对码继续由系统登录流程处理。
