# L2 管线 benchmark 报告

生成: 2026-10-21 02:22:58  engine@c4c1517  model=deepseek-chat

### p1_gamer_student

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户玩任何游戏都只玩辅助位，从不玩输出位 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户每天坚持完成原神的日常任务，一天不落 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户打游戏不打排位，只玩休闲匹配 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=7,age=3)] 用户的猫送回老家给爸妈养了，现在养了一只柴犬叫旺财 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [preference] 设备 iPhone |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户作息特别规律，每天六点早起 | [medium(c=3,age=2)] 用户开学后变成熬夜党，经常凌晨两点才睡 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对花生过敏，且过敏症状严重 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户心脏不太好，医生嘱咐不要通宵 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的弟弟患有哮喘，家里不能长时间养带毛的宠物 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户昨天和室友大吵一架后和好，感觉反而更了解彼此 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户暑假和好朋友去看了人生第一场演唱会，觉得特别难忘 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户第一次深渊满星，感到非常激动 |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=8,age=2)] 用户已经搬出学校宿舍，现在在校外租房住 |
| contradiction | ct2 | single_value | ❌ CONTRADICTED | [short] 用户不喝咖啡，嫌苦 | [medium(c=3,age=2)] 用户现在每天喝两杯美式咖啡，已经离不开咖啡了 |
| contradiction | ct3 | single_value | ✅ EVOLVED | [preference] 专业 计算机 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 8 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 3 偏好 3
开销: 24 批次，71314 in / 2286 out（cached 51584），26.2s，API错误 0

### p2_working_mom

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ❌ MISSING |  |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户不喝奶茶，觉得太甜受不了 | [preference] 饮品偏好 不喝奶茶（觉得太甜） |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户全家周末固定去爬山，两年没断过 |
| evolving_fact | ef1 | evolved | ❌ CONTRADICTED | [short] 用户的女儿在上幼儿园，曾参加幼儿园演出 | [medium(c=6,age=3)] 用户的女儿今年九月升小学了，适应得还不错 |
| evolving_fact | ef2 | evolved | ❌ CONTRADICTED | [medium(c=3,age=14)] 用户每天开车通勤，单程四十分钟 | [short] 用户换工作了，现在改坐地铁通勤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=5,age=2)] 用户上个月转行做保险了，此前从事财务工作十年 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对青霉素过敏 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户怀二胎时得过妊娠糖尿病，现在很注意控糖 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的小儿子有先天性心脏病，需要定期复查 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户的女儿在上幼儿园，曾参加幼儿园演出 |
| salient_episode | se2 | canary | 🐤 CANARY-FAIL |  |
| salient_episode | se3 | canary | 🐤 CANARY-FAIL |  |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=7,age=2)] 用户不是独生女，有一个姐姐 |
| contradiction | ct2 | single_value | ✅ MERGED | [medium(c=6,age=2)] 用户家原本养的两条狗上个月已送人，现在家里没有宠物 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户住在杭州 | [medium(c=3,age=1)] 用户全家去年已搬到南京居住 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 8 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 1 偏好 1
开销: 24 批次，70881 in / 2056 out（cached 52864），23.8s，API错误 0

### p3_fitness_coach

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户吃饭必须高蛋白，以鸡胸肉为主食 | [preference] 饮食 必须高蛋白，鸡胸肉为主食 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户习惯早上五点起来锻炼，认为晚上锻炼影响睡眠 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户滴酒不沾，尤其在备赛期不喝酒 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=7,age=3)] 用户已结束减脂期，现在开始增肌，采用热量盈余饮食 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户膝盖已康复，深蹲重量重回150公斤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=6,age=2)] 用户已辞去连锁健身房私人教练的工作，自己开了一家私教工作室 |
| one_shot_junk | j1 | absent | ❌ LEAKED | [medium(c=3,age=8)] 用户今天腿酸得走路费劲 |
| one_shot_junk | j2 | absent | ❌ LEAKED | [medium(c=4,age=7)] 用户喝蛋白粉，最近蛋白粉喝完了准备再买 |
| one_shot_junk | j3 | absent | ❌ LEAKED | [medium(c=5,age=6)] 用户所在健身房的跑步机又坏了，物业维修慢 |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户海鲜过敏，吃虾蟹会起疹子 | [preference] 过敏 海鲜过敏（虾蟹），会起疹子 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户年轻时做过腰椎手术，做硬拉必须戴护具 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的妻子是素食主义者，家里做饭要分两套 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户练了八年，昨天第一次深蹲起两倍体重 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户是私人教练，带过一个学员三年，该学员昨天跑完人生第一个全马 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户上个月在省赛拿了季军，差一点进国家队选拔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=7,age=2)] 用户现在负重30公斤做引体向上 |
| contradiction | ct2 | single_value | ✅ MERGED | [medium(c=6,age=2)] 用户已停止拳击训练，现在专攻力量举 |
| contradiction | ct3 | single_value | ✅ EVOLVED | [medium(c=5,age=1)] 用户今年三十五岁 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 6 / MEDIUM 12 / LONG 0（时钟 24），画像槽位: 兴趣 1 偏好 2
开销: 24 批次，73566 in / 2854 out（cached 52352），25.1s，API错误 0

### p4_indie_dev

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户做游戏只使用 Godot 引擎，不使用其他引擎 | [interest] Godot 引擎 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户效率最高的时段是深夜，代码都在凌晨编写 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户的游戏全部采用像素风，这是其个人标志性风格 | [interest] 像素风 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户的第二款游戏已于上个月上架，销量一般 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=3,age=3)] 用户上个月招了一名全职美术，不再独自包揽美术工作 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户在老家远程办公，生活在小城市，生活成本较低 | [medium(c=3,age=2)] 用户为了谈游戏发行已从生活三年的老家搬回深圳 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户患有腱鞘炎，医生建议其控制打字时长 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户曾被合伙人坑害，背负违约金，目前正在打官司 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的第一款游戏亏损三万块，约占其全部积蓄的三分之一 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户收到玩家第一条长评好评，看哭了，觉得值了 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户去年参加 game jam，48 小时做完一个游戏并获得名次 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户三年前辞职做独立开发，把工牌留在工位直接走了，一点不后悔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=7,age=2)] 用户改变了不做外包的做法，音乐现已外包，美术以前也外包过 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=6,age=2)] 用户计划改变，目前正在洽谈将游戏移植到 Switch |
| contradiction | ct3 | single_value | ✅ MERGED | [medium(c=5,age=1)] 用户做游戏已改用 Linux 系统进行开发，两年多前就从 Windows 换过来了 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 7 / MEDIUM 10 / LONG 0（时钟 24），画像槽位: 兴趣 3 偏好 2
开销: 24 批次，72786 in / 2439 out（cached 54272），25.6s，API错误 0

### p5_acg_fan

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户玩 cosplay 已有五年，每个月至少出一套 | [interest] cosplay |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户收藏手办只买正版，不碰盗版 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户追番从不看解说剪辑，只看原片 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=7,age=3)] 用户的本命从初音未来换成了崩坏：星穹铁道的流萤，初音仍留在心里但已不是第一本命 |
| evolving_fact | ef2 | evolved | ✅ MERGED | [medium(c=5,age=3)] 用户加入了 cos 社团，现在逛漫展都跟社团一起，不再一个人去 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户正在学板绘，自认为画得很烂 | [medium(c=3,age=2)] 用户的板绘水平已经练出来，现在开始接约稿赚外快 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户眼角膜受过伤，不能戴隐形眼镜，cos 只能戴框架改 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户对乳胶过敏，购买道具手套时需注意材质 |
| critical_lowfreq | cl3 | in_store | ❌ MISSING |  |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户上周第一次上台 cos 走秀，虽然紧张腿抖但下台时感到特别爽 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户和认识四年的网友第一次面基，聊到展馆关门也不觉得尴尬 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户养猫，最贵的一只手办被猫推下去摔碎了，心痛到失眠三天 |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=7,age=2)] 用户最近开始追国漫，追了好几部，打破之前只看日漫的习惯 |
| contradiction | ct2 | single_value | ✅ MERGED | [medium(c=6,age=2)] 用户为了崩坏：星穹铁道的流萤氪了好几个648，已不再是完全不氪金的白嫖党 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户在上海读书 | [short] 用户已从上海毕业，回到成都生活，上海是上学时的事 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 8 / LONG 0（时钟 24），画像槽位: 兴趣 7 偏好 2
开销: 24 批次，73325 in / 2429 out（cached 53760），29.4s，API错误 0

### p6_retired_teacher

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户退休后每天写一小时毛笔字，主练颜体 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户听了一辈子京剧，最爱程派 | [interest] 京剧（程派） |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=6,age=2)] 用户一点辣都不能吃，肠胃不行 | [preference] 饮食 完全不能吃辣（肠胃不好） |
| evolving_fact | ef1 | evolved | ❌ LOST |  |
| evolving_fact | ef2 | evolved | ✅ MERGED | [medium(c=4,age=3)] 用户上个月从老家县城搬到省城儿子家住，帮忙带孙女 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户血压有点偏高，正在观察中 | [medium(c=3,age=2)] 用户确诊高血压，开始每天吃降压药 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对磺胺类药物过敏，年轻时曾因过敏休克过一次 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户与老伴曾庆祝金婚纪念，他给老伴写了一幅长卷，老伴去世前看到了 | [short] 用户的老伴五年前去世，目前独自生活 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户左耳戴助听器，希望对话尽量用文字 | [preference] 交流方式 偏好文字交流（左耳戴助听器） |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户教了三十年书，上月第一批学生（已五十多岁）回来看望他 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户的颜体书法在县里书法比赛获得一等奖 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户与老伴曾庆祝金婚纪念，他给老伴写了一幅长卷，老伴去世前看到了 |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=7,age=2)] 用户是退休语文老师，教了四十年书（不是数学老师） |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=6,age=2)] 用户现在也用微信了，还在老年大学群里天天发早安 |
| contradiction | ct3 | single_value | ✅ EVOLVED | [medium(c=5,age=1)] 用户的孙女正在上小学三年级（不是初中） |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 7 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 2
开销: 24 批次，72056 in / 2113 out（cached 53760），24.8s，API错误 0

## 汇总

| persona | 计分通过 | 通过率 | canary | 泄漏(junk/rd/植入) | 批次 | in+out token |
|---|---|---|---|---|---|---|
| p1_gamer_student | 19/21 | 90% | 3/3 | 0 | 24 | 73600 |
| p2_working_mom | 17/21 | 81% | 1/3 | 0 | 24 | 72937 |
| p3_fitness_coach | 18/21 | 86% | 3/3 | 3 | 24 | 76420 |
| p4_indie_dev | 20/21 | 95% | 3/3 | 0 | 24 | 75225 |
| p5_acg_fan | 18/21 | 86% | 3/3 | 0 | 24 | 75754 |
| p6_retired_teacher | 19/21 | 90% | 3/3 | 0 | 24 | 74169 |
| **合计** | **111/126** | **88%** | **16/18** | **3** | | |